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
from urllib.error import HTTPError

import pika
from helpers.cache import KeyValueCache
from helpers.exceptions import (
    BadRequest,
    ForbiddenRequest,
    InvalidArgs,
    LaunchpadError,
    LaunchpadUnavailable,
    NotFound,
)
from helpers.utils import (
    HTTP_TIMEOUT,
    WEB_RUNTIME_DIR,
    amqp_connect,
    get_autopkgtest_cloud_conf,
    get_release_arches,
)

# Launchpad REST API base
LP = "https://api.launchpad.net/1.0/"
NAME = re.compile("^[a-z0-9][a-z0-9.+-]+$")
VERSION = re.compile("^[a-zA-Z0-9.+:~-]+$")
# allowed values are rather conservative, expand if/when needed
ENV = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]+=[a-zA-Z0-9.:~/ -=]*$")
# URL and optional branch name
GIT = re.compile(r"^https?://[a-zA-Z0-9._/~+-]+(#[a-zA-Z0-9._/-]+)?$")

ALLOWED_USER_CACHE_TIME = 6 * 60 * 60


class Submit:
    def __init__(self):
        config = get_autopkgtest_cloud_conf()

        self.release_arches = get_release_arches()
        logging.debug(f"Valid arches per release: {self.release_arches}")

        self.allowed_user_cache = KeyValueCache(
            WEB_RUNTIME_DIR / "autopkgtest_users.json"
        )
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

        self.validate_ppas(ppas, check_exists=False)

        if not self.in_allowed_team(requester):
            raise ForbiddenRequest(self.allowed_teams)

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
        if not NAME.fullmatch(package):
            raise NotFound("package", package)
        if not ppas:
            raise BadRequest(
                "Must specify at least one PPA (to associate results with)"
            )
        for e in env:
            if not ENV.fullmatch(e):
                raise BadRequest(f'Invalid environment variable format "{e}"')
        # we should only be called in this mode
        assert "build-git" in kwargs
        if not GIT.fullmatch(kwargs["build-git"]):
            raise BadRequest("Malformed build-git")
        if "testname" in kwargs and not NAME.fullmatch(kwargs["testname"]):
            raise BadRequest("Malformed testname")

        unsupported_keys = set(kwargs.keys()) - {"build-git", "testname"}
        if unsupported_keys:
            raise BadRequest(
                "Unsupported arguments: {}".format(" ".join(unsupported_keys))
            )

        self.validate_ppas(ppas)

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
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as f:
            logging.debug(
                "POST to URL %s succeeded with code %u: %s",
                f.geturl(),
                f.getcode(),
                f.read(),
            )

    #
    # helper methods
    #

    def validate_ppas(self, ppas, *, check_exists=True):
        for ppa in dict.fromkeys(ppas):
            team, _, name = ppa.partition("/")
            if not NAME.fullmatch(team) or not NAME.fullmatch(name):
                raise BadRequest(f"Malformed PPA name: {ppa}")
            if not check_exists:
                continue

            response = self.lp_request(
                "~" + team,
                {
                    "ws.op": "getPPAByName",
                    "distribution": json.dumps(LP + "ubuntu"),
                    "name": json.dumps(name),
                },
            )
            if response is None:
                raise NotFound("ppa", ppa)
            if response.get("name") != name:
                raise LaunchpadError("Unexpected response to a Launchpad PPA lookup.")

    def in_allowed_team(self, person):
        """Check for allowed team membership, caching positive results."""
        cache_key = json.dumps([person, sorted(self.allowed_teams)])
        timestamp = self.allowed_user_cache.get(cache_key)
        now = time()
        if (
            isinstance(timestamp, (int, float))
            and now - ALLOWED_USER_CACHE_TIME <= timestamp <= now
        ):
            return True

        response = self.lp_request(f"~{person}/super_teams", {"ws.size": "300"})
        if response is None:
            raise NotFound("Launchpad user", person)

        while True:
            try:
                if not isinstance(response.get("entries"), list):
                    raise ValueError("Expected a list of teams")
                if any(
                    entry["name"] in self.allowed_teams for entry in response["entries"]
                ):
                    self.allowed_user_cache.set(cache_key, time())
                    return True
            except (KeyError, TypeError, ValueError) as e:
                raise LaunchpadError(
                    "Unexpected response to a Launchpad team membership lookup."
                ) from e
            next_page = response.get("next_collection_link")
            if not next_page:
                return False
            response = self.lp_request(next_page, {})
            if response is None:
                raise LaunchpadError("Launchpad team membership page not found.")

    @staticmethod
    def lp_request(obj, query) -> dict | None:
        """Return a Launchpad JSON object, or None on 404."""
        url = urllib.parse.urljoin(LP, obj)
        if not url.startswith(LP):
            raise LaunchpadError("Unexpected URL during a Launchpad request.")
        if query:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(query)
        try:
            with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as req:
                code = req.getcode()
                response = req.read()
        except HTTPError as e:
            if e.code == 404:
                return None
            request_id = e.headers.get("X-Request-Id") if e.headers else None
            logging.error(
                "%s returned HTTP %d (request ID: %s)", url, e.code, request_id or "-"
            )
            if e.code >= 500 or e.code in (408, 429):
                raise LaunchpadUnavailable() from e
            raise LaunchpadError("Unexpected HTTP status from Launchpad.") from e
        except (OSError, HTTPException) as e:
            logging.warning("Launchpad request failed: %s: %s", url, e)
            raise LaunchpadUnavailable() from e

        if code != 200:
            logging.error("Unexpected HTTP status %s from %s", code, url)
            raise LaunchpadError("Unexpected HTTP status from Launchpad.")

        try:
            response = json.loads(response.decode("UTF-8"))
            if not isinstance(response, dict):
                raise ValueError("Unexpected Launchpad response")
        except ValueError as e:
            logging.error("URL %s gave an invalid response: %s", req.geturl(), e)
            raise LaunchpadError(
                "Unexpected response during a Launchpad request."
            ) from e
        return response

    def validate_args(self, parameters):
        if not {"arch", "release", "package", "triggers"}.issubset(parameters):
            raise InvalidArgs(parameters)
