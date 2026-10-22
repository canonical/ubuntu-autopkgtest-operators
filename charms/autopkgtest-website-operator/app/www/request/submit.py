"""Logic for verifying and submitting test requests.

Author: Martin Pitt <martin.pitt@ubuntu.com>
"""

import base64
import json
import logging
import re
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from http.client import HTTPException
from time import time
from urllib.error import HTTPError, URLError

import pika
from helpers.cache import KeyValueCache
from helpers.exceptions import (
    BadRequest,
    ForbiddenRequest,
    InvalidArgs,
    LaunchpadUnavailable,
    NotFound,
)
from helpers.utils import amqp_connect, get_autopkgtest_cloud_conf, get_release_arches

# Launchpad REST API base
LP = "https://api.launchpad.net/1.0/"
NAME = re.compile("^[a-z0-9][a-z0-9.+-]+$")
VERSION = re.compile("^[a-zA-Z0-9.+:~-]+$")
# allowed values are rather conservative, expand if/when needed
ENV = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]+=[a-zA-Z0-9.:~/ -=]*$")
# URL and optional branch name
GIT = re.compile(r"^https?://[a-zA-Z0-9._/~+-]+(#[a-zA-Z0-9._/-]+)?$")

ALLOWED_USER_CACHE_TIME = 6 * 60 * 60

LP_TIMEOUT = 60


class Submit:
    def __init__(self):
        config = get_autopkgtest_cloud_conf()
        self.lp_cache = {}

        self.release_arches = get_release_arches()
        logging.debug(f"Valid arches per release: {self.release_arches}")

        self.allowed_user_cache = KeyValueCache("/dev/shm/autopkgtest_users.json")
        self.allowed_teams = {
            team.strip() for team in config["web"]["allowed_requestors"].split(",")
        }

    def validate_distro_request(
        self, release, arch, package, triggers, requester, ppas=(), **kwargs
    ):
        """Validate package and triggers for a distro test request.

        'package' is a single source package name. 'triggers' has the format
        ['sourcepackage/version', ...].

        'ppas' is a list of 'team/ppaname' strings.

        'arch' is an architecture or a list of architectures.

        Raise WebControlException with error message if the request is invalid,
        otherwise return.
        """
        if "migration-reference/0" in triggers and kwargs.get("all-proposed") == "1":
            raise BadRequest(
                "migration-reference/0 and all-proposed=1 are not compatible arguments."
            )
        if "all-proposed" in kwargs and kwargs["all-proposed"] != "1":
            raise BadRequest("Invalid all-proposed value")
        if "readable-by" in kwargs and not kwargs["readable-by"]:
            raise BadRequest("Invalid readable-by value")
        # no other kwargs supported
        for key in kwargs:
            if key not in {"all-proposed", "readable-by"}:
                raise BadRequest(f"Invalid argument {key}")

        if release not in self.release_arches:
            raise NotFound("release", release)
        arches = [arch] if isinstance(arch, str) else arch
        if not arches:
            raise BadRequest("Must specify at least one architecture")
        for architecture in arches:
            if architecture not in self.release_arches[release]:
                raise NotFound("arch", architecture)
        if not NAME.fullmatch(package):
            raise BadRequest("Malformed package name")

        if "migration-reference/0" in triggers:
            if len(triggers) != 1:
                raise BadRequest(
                    "Cannot use additional triggers with migration-reference/0"
                )
            if ppas:
                raise BadRequest("Cannot use PPAs with migration-reference/0")
        for trigger in triggers:
            try:
                trigsrc, trigver = trigger.split("/")
            except ValueError as e:
                raise BadRequest("Malformed trigger, must be srcpackage/version") from e
            # Debian Policy 5.6.1 and 5.6.12
            if not NAME.fullmatch(trigsrc) or not VERSION.fullmatch(trigver):
                raise BadRequest(f"Malformed trigger: {trigsrc}\nversion: {trigver}")

            # The raspi kernel can't be tested with autopkgtest. It doesn't
            # support EFI and won't boot in OpenStack.
            if trigsrc.startswith("linux-meta-raspi"):
                raise BadRequest("The raspi kernel can't be tested with autopkgtest.")

        for ppa in ppas:
            if not self.is_valid_ppa(ppa):
                raise NotFound("ppa", ppa)

        upload_targets = []
        for trigger in triggers:
            trigsrc, trigver = trigger.split("/")
            # Special snowflake
            if trigger in ("qemu-efi-noacpi/0", "migration-reference/0"):
                continue

            if ppas:
                if not self.get_published_source_component(
                    release, trigsrc, trigver, ppas[-1]
                ):
                    raise BadRequest(
                        f"{trigger} is not published in PPA {ppas[-1]} {release}"
                    )
                trigsrc_component = None
            else:
                trigsrc_component = self.get_published_source_component(
                    release, trigsrc, trigver
                )
                if not trigsrc_component:
                    raise BadRequest(f"{trigger} is not published in {release}")

            upload_targets.append((trigsrc, trigsrc_component))

        if ppas:
            component = None
        else:
            component = self.get_published_source_component(release, package, None)
            if not component:
                raise BadRequest(f"{package} is not published in {release}")
        upload_targets.append((package, component))

        if self.cached_allowed_user(requester):
            return
        for source, component in upload_targets:
            component = (
                component
                or self.get_published_source_component(release, source, None)
                or "main"
            )
            if self.can_upload(requester, release, component, source):
                return
        if self.in_allowed_team(requester):
            return
        raise ForbiddenRequest(package, ",".join(triggers))

    def validate_git_request(self, release, arch, package, ppas=(), env=(), **kwargs):
        """Validate parameters for an upstream git test request.

        Supported kwargs:
        - 'build-git' is the URL of the branch to test
        - 'ppas' is a list of 'team/ppaname' strings.
        - 'env' is a list of 'key=value' strings.
        - 'testname' is a string.

        Raise WebControlException with an error message if the request is invalid,
        otherwise return.
        """
        if release not in self.release_arches:
            raise NotFound("release", release)
        if arch not in self.release_arches[release]:
            raise NotFound("arch", arch)
        if not NAME.match(package):
            raise NotFound("package", package)
        if not ppas:
            raise BadRequest(
                "Must specify at least one PPA (to associate results with)"
            )
        for e in env:
            if not ENV.match(e):
                raise BadRequest(f'Invalid environment variable format "{e}"')
        # we should only be called in this mode
        assert "build-git" in kwargs
        if not GIT.match(kwargs["build-git"]):
            raise BadRequest("Malformed build-git")
        if "testname" in kwargs and not NAME.match(kwargs["testname"]):
            raise BadRequest("Malformed testname")

        unsupported_keys = set(kwargs.keys()) - {"build-git", "testname"}
        if unsupported_keys:
            raise BadRequest(
                "Unsupported arguments: {}".format(" ".join(unsupported_keys))
            )

        for ppa in ppas:
            if not self.is_valid_ppa(ppa):
                raise NotFound("ppa", ppa)

    def send_amqp_request(self, release, arch, package, context=None, **params):
        """Submit jobs for one or more architectures over one AMQP connection."""
        arches = [arch] if isinstance(arch, str) else arch
        queue_prefix = f"debci-{context}-{release}" if context else f"debci-{release}"

        params["submit-time"] = datetime.strftime(
            datetime.now().astimezone(UTC), "%Y-%m-%d %H:%M:%S%z"
        )
        body = f"{package}\n{json.dumps(params, sort_keys=True)}"
        with amqp_connect() as amqp_con:
            with amqp_con.channel() as ch:
                for architecture in arches:
                    ch.basic_publish(
                        exchange="",
                        routing_key=f"{queue_prefix}-{architecture}",
                        body=body,
                        properties=pika.BasicProperties(
                            delivery_mode=pika.spec.PERSISTENT_DELIVERY_MODE,
                        ),
                    )

    @classmethod
    def post_json(cls, url, data, auth_file, project):
        """Send POST request with JSON data via basic auth.

        'data' is a dictionary which will be posted to 'url' in JSON encoded
        form. 'auth_file' is the path to a file containing
        "project:user:password" lines. 'project' is used as a lookup key in
        auth_file.

        This is being used to send status updates to GitHub.

        Raises exception if auth_file does not exist or is invalid. Raises
        HTTPError if POST request fails.
        """
        # look up project in auth_file
        with open(auth_file) as f:
            contents = f.read()
        for l in contents.splitlines():
            if l.startswith(project + ":"):
                credentials = l.split(":", 1)[1].strip()
                break
        else:
            logging.error(
                "%s does not have password for project %s", auth_file, project
            )
            return

        req = urllib.request.Request(
            url=url,
            method="POST",
            data=json.dumps(data).encode("UTF-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Basic {base64.b64encode(credentials.encode()).decode()}",
            },
        )
        with urllib.request.urlopen(req) as f:
            logging.debug(
                "POST to URL %s succeeded with code %u: %s",
                f.geturl(),
                f.getcode(),
                f.read(),
            )

    #
    # helper methods
    #

    def is_valid_ppa(self, ppa):
        """Check if a ppa exists."""
        team, _, name = ppa.partition("/")
        if not NAME.match(team) or not NAME.match(name):
            return False
        # https://launchpad.net/+apidoc/1.0.html#person-getPPAByName
        (code, response) = self.cached_lp_request(
            "~" + team,
            {
                "ws.op": "getPPAByName",
                "distribution": json.dumps(LP + "ubuntu"),
                "name": json.dumps(name),
            },
        )
        logging.debug(
            "is_valid_ppa(%s): code %u, response %s", ppa, code, repr(response)
        )
        if code == 404:
            return False
        if code != 200 or response.get("name") != name:
            raise LaunchpadUnavailable()
        return True

    def get_published_source_component(self, release, package, version, ppa=None):
        """Return the component of a published source, or None if absent.

        Use this for validating trigger packages. This queries the Launchpad
        REST API. Check given ppa (team/name), or the main Ubuntu archive if
        this is None.
        """
        # https://launchpad.net/+apidoc/1.0.html#archive-getPublishedSources
        if ppa:
            team, name = ppa.split("/")
            obj = f"~{team}/+archive/ubuntu/{name}"
        else:
            obj = "ubuntu/+archive/primary"
        req = {
            "ws.op": "getPublishedSources",
            "source_name": json.dumps(package),
            "distro_series": json.dumps(LP + "ubuntu/" + release),
            "status": "Published",
            "exact_match": "true",
            "ws.size": "1",
        }
        if version is not None:
            req["version"] = json.dumps(version)
        (code, response) = self.cached_lp_request(obj, req)
        if code != 200:
            raise LaunchpadUnavailable()
        logging.debug(
            "get_published_source_component(%s, %s, %s): code %u, response %s",
            release,
            package,
            version,
            code,
            repr(response),
        )
        try:
            entries = response["entries"]
            if not isinstance(entries, list):
                raise LaunchpadUnavailable()
            if not entries:
                return None
            component = entries[0]["component_name"]
            if not isinstance(component, str) or not component:
                raise LaunchpadUnavailable()
            return component
        except (KeyError, TypeError) as e:
            raise LaunchpadUnavailable() from e

    def can_upload(self, person, release, component, package):
        """Check if person can upload package into Ubuntu release."""
        # https://launchpad.net/+apidoc/1.0.html#archive-checkUpload
        (code, response) = self.cached_lp_request(
            "ubuntu/+archive/primary",
            {
                "ws.op": "checkUpload",
                "distroseries": json.dumps(LP + "ubuntu/" + release),
                "person": json.dumps(LP + "~" + person),
                "component": component,
                "pocket": "Proposed",
                "sourcepackagename": json.dumps(package),
            },
        )
        logging.debug(
            "can_upload(%s, %s, %s, %s): (%u, %s)",
            person,
            release,
            component,
            package,
            code,
            repr(response),
        )
        if code == 403:
            return False
        if code != 200 or response is not True:
            raise LaunchpadUnavailable()
        return True

    def allowed_user_cache_key(self, person):
        return json.dumps([person, sorted(self.allowed_teams)])

    def cached_allowed_user(self, person):
        timestamp = self.allowed_user_cache.get(self.allowed_user_cache_key(person))
        try:
            return 0 <= time() - float(timestamp) <= ALLOWED_USER_CACHE_TIME
        except (TypeError, ValueError, OverflowError):
            return False

    def in_allowed_team(self, person):
        """Check if person is allowed to queue tests."""
        obj, query = f"~{person}/super_teams", {"ws.size": "300"}
        while True:
            code, response = self.cached_lp_request(obj, query)
            if code != 200:
                raise LaunchpadUnavailable()
            if not isinstance(response.get("entries"), list):
                raise LaunchpadUnavailable()
            try:
                if any(
                    entry["name"] in self.allowed_teams for entry in response["entries"]
                ):
                    self.allowed_user_cache.set(
                        self.allowed_user_cache_key(person), time()
                    )
                    return True
            except (KeyError, TypeError) as e:
                raise LaunchpadUnavailable() from e
            next_page = response.get("next_collection_link")
            if not next_page:
                return False
            obj, query = next_page, {}

    def cached_lp_request(self, obj, query):
        """Reuse identical lookups only within this submission."""
        key = (obj, tuple(sorted(query.items())))
        if key not in self.lp_cache:
            self.lp_cache[key] = self.lp_request(obj, query)
        return self.lp_cache[key]

    @classmethod
    def lp_request(cls, obj, query):
        """Do a Launchpad REST request.

        Request https://api.launchpad.net/1.0/<obj>?<query>.

        Return (code, json), where json is defined for successful codes
        (200 <= code < 300) and None for non-transient client errors. Raise
        LaunchpadUnavailable for dependency failures.
        """
        url = urllib.parse.urljoin(LP, obj)
        if not url.startswith(LP):
            raise LaunchpadUnavailable()
        if query:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(query)
        try:
            with urllib.request.urlopen(url, timeout=LP_TIMEOUT) as req:
                code = req.getcode()
                response = req.read()
        except HTTPError as e:
            logging.error("%s failed with %u: %s\n%s", url, e.code, e.reason, e.headers)
            if e.code in (408, 429) or not 400 <= e.code < 500:
                raise LaunchpadUnavailable() from e
            return (e.code, None)
        except (URLError, OSError, HTTPException) as e:
            logging.warning("Launchpad request failed: %s: %s", url, e)
            raise LaunchpadUnavailable() from e

        try:
            response = json.loads(response.decode("UTF-8"))
            if query.get("ws.op") != "checkUpload" and not isinstance(response, dict):
                raise ValueError("Unexpected Launchpad response")
        except ValueError as e:
            logging.error(
                "URL %s gave invalid response %s: %s",
                req.geturl(),
                response,
                str(e),
            )
            raise LaunchpadUnavailable() from e
        logging.debug("lp_request %s succeeded: %s", url, response)
        return (code, response)

    def validate_args(self, parameters):
        base = ["arch", "release", "package", "triggers"]
        if not set(base).issubset(set(parameters.keys())):
            raise InvalidArgs(parameters)
