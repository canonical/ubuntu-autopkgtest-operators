"""Logic for verifying and submitting test requests.

Author: Martin Pitt <martin.pitt@ubuntu.com>
"""

import base64
import json
import logging
import os
import re
import urllib.parse
import urllib.request
import uuid
from datetime import UTC, datetime, timedelta
from time import time
from urllib.error import HTTPError

import pika
from helpers.cache import KeyValueCache
from helpers.exceptions import (
    BadRequest,
    ForbiddenRequest,
    InvalidArgs,
    NotFound,
    RequestInQueue,
    RequestRunning,
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

# not teams
ALLOWED_USERS_PERPACKAGE = {"snapcraft": ["snappy-m-o"]}

ALLOWED_USER_CACHE_TIME = timedelta(hours=3)


class Submit:
    def __init__(self):
        self.config = get_autopkgtest_cloud_conf()

        self.release_arches = get_release_arches()
        logging.debug(f"Valid arches per release: {self.release_arches}")

        self.allowed_user_cache = KeyValueCache("/dev/shm/autopkgtest_users.json")

    def clear_cache(self):
        self.allowed_user_cache.clear()

    def migration_reference_all_proposed_check(self, triggers, kwargs):
        if (
            "migration-reference/0" in triggers
            and "all-proposed" in kwargs.keys()
            and int(kwargs["all-proposed"]) == 1
        ):
            raise BadRequest(
                "migration-reference/0 and all-proposed=1 are not compatible arguments."
            )

    def validate_distro_request(
        self, release, arch, package, triggers, requester, ppas=[], **kwargs
    ):
        """Validate package and triggers for a distro test request.

        'package' is a single source package name. 'triggers' has the format
        ['sourcepackage/version', ...].

        'ppas' is a list of 'team/ppaname' strings.

        Raise ValueError with error message if the request is invalid,
        otherwise return.
        """
        self.is_request_queued_or_running(release, arch, package, triggers)

        self.migration_reference_all_proposed_check(triggers, kwargs)

        can_upload_any_trigger = False

        try:
            if kwargs["delete"] != "1":
                raise ValueError("Invalid delete value")
            del kwargs["delete"]
        except KeyError:
            pass
        try:
            if kwargs["all-proposed"] != "1":
                raise ValueError("Invalid all-proposed value")
            del kwargs["all-proposed"]
        except KeyError:
            pass
        try:
            if not kwargs["readable-by"]:
                raise ValueError("Invalid readable-by value")
            del kwargs["readable-by"]
        except KeyError:
            pass
        # no other kwargs supported
        if kwargs:
            raise ValueError(f"Invalid argument {list(kwargs)[0]}")

        if release not in self.release_arches:
            raise NotFound("release", release)
        if arch not in self.release_arches[release]:
            raise NotFound("arch", arch)
        for ppa in ppas:
            if not self.is_valid_ppa(ppa):
                raise NotFound("ppa", ppa)
        if not self.in_allowed_team(requester):
            if not ppas and not self.is_valid_package_with_results(
                release, arch, package
            ):
                raise NotFound("package", package, "does not have any test results")

        if "migration-reference/0" in triggers:
            if len(triggers) != 1:
                raise BadRequest(
                    "Cannot use additional triggers with migration-reference/0"
                )
            if ppas:
                raise BadRequest("Cannot use PPAs with migration-reference/0")
            if "all-proposed" in kwargs:
                raise BadRequest('Cannot use "all-proposed" with migration-reference/0')
        for trigger in triggers:
            try:
                trigsrc, trigver = trigger.split("/")
            except ValueError as e:
                raise BadRequest("Malformed trigger, must be srcpackage/version") from e
            # Debian Policy 5.6.1 and 5.6.12
            if not NAME.match(trigsrc) or not VERSION.match(trigver):
                raise BadRequest(f"Malformed trigger: {trigsrc}\nversion: {trigver}")

            # The raspi kernel can't be tested with autopkgtest. It doesn't
            # support EFI and won't boot in OpenStack.
            if trigsrc.startswith("linux-meta-raspi"):
                raise BadRequest("The raspi kernel can't be tested with autopkgtest.")

            # Special snowflake
            if trigger in ("qemu-efi-noacpi/0", "migration-reference/0"):
                continue

            if ppas:
                if not self.is_valid_package_version(
                    release, trigsrc, trigver, ppas and ppas[-1] or None
                ):
                    raise BadRequest(
                        f"{trigger} is not published in PPA {ppas[-1]} {release}"
                    )
                # PPAs don't have components, so we need to determine it from the
                # Ubuntu archive
                trigsrc_component = (
                    self.is_valid_package_version(release, trigsrc, None) or "main"
                )
            else:
                trigsrc_component = self.is_valid_package_version(
                    release, trigsrc, trigver
                )
                if not trigsrc_component:
                    raise BadRequest(f"{trigger} is not published in {release}")

            can_upload_any_trigger = can_upload_any_trigger or self.can_upload(
                requester, release, trigsrc_component, trigsrc
            )

        if ppas:
            package_component = (
                self.is_valid_package_version(release, package, None) or "main"
            )
        else:
            package_component = self.is_valid_package_version(release, package, None)
            if not package_component:
                raise BadRequest(f"{package} is not published in {release}")

        # verify that requester can upload package or trigsrc
        if (
            not self.can_upload(requester, release, package_component, package)
            and not can_upload_any_trigger
            and requester not in ALLOWED_USERS_PERPACKAGE.get(package, [])
            and not self.in_allowed_team(requester)
        ):
            raise ForbiddenRequest(package, ",".join(triggers))

    def validate_git_request(self, release, arch, package, ppas=[], env=[], **kwargs):
        """Validate parameters for an upstream git test request.

        Supported kwargs:
        - 'build-git' is the URL of the branch to test
        - 'ppas' is a list of 'team/ppaname' strings.
        - 'env' is a list of 'key=value' strings.
        - 'testname' is a string.

        Raise ValueError with error message if the request is invalid,
        otherwise return.
        """
        triggers = []
        for env_var in env:
            if "trigger" in env_var:
                if "," in env_var:
                    for trig in env_var.split("=")[1].split(","):
                        triggers.append(trig)
                else:
                    triggers.append(env_var.split("=")[1])

        self.is_request_queued_or_running(
            release, arch, package, triggers, kwargs, git=True
        )

        self.migration_reference_all_proposed_check(triggers, kwargs)

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
        for ppa in ppas:
            if not self.is_valid_ppa(ppa):
                raise NotFound("ppa", ppa)
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

    def unsend_amqp_request(self, release, arch, package, context=None, **params):
        """Remove an autopkgtest AMQP request."""
        if context:
            queue = f"debci-{context}-{release}-{arch}"
        else:
            queue = f"debci-{release}-{arch}"

        count = 0

        with amqp_connect() as amqp_con:
            with amqp_con.channel() as ch:
                while True:
                    method, properties, body = ch.basic_get(queue)
                    if body is None:
                        break
                    body = body.decode()
                    this_package, this_params = body.split(None, 1)
                    this_params = json.loads(this_params)
                    del this_params["submit-time"]

                    if this_package == package and this_params == params:
                        ch.basic_ack(method.delivery_tag)
                        count += 1
        return count

    def send_amqp_request(self, release, arch, package, context=None, **params):
        """Send autopkgtest AMQP request."""
        if context:
            queue = f"debci-{context}-{release}-{arch}"
        else:
            queue = f"debci-{release}-{arch}"

        params["submit-time"] = datetime.strftime(
            datetime.now().astimezone(UTC), "%Y-%m-%d %H:%M:%S%z"
        )
        params["uuid"] = str(uuid.uuid4())
        body = f"{package}\n{json.dumps(params, sort_keys=True)}"
        with amqp_connect() as amqp_con:
            with amqp_con.channel() as ch:
                ch.basic_publish(
                    exchange="",
                    routing_key=queue,
                    body=body,
                    properties=pika.BasicProperties(
                        delivery_mode=pika.spec.PERSISTENT_DELIVERY_MODE,
                    ),
                )
        return params["uuid"]

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
            return None
        # https://launchpad.net/+apidoc/1.0.html#person-getPPAByName
        (code, response) = self.lp_request(
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
        if code < 200 or code >= 300:
            return None
        if response.get("name") == name:
            return True

    def is_valid_package_with_results(self, release, arch, package):
        """Check if package exists and has any results on that release+arch.

        Use this for validating *tested* packages (not triggers, as they don't
        necessarily have tests themselves).
        """
        c = self.db_con.cursor()
        if release:
            c.execute(
                "SELECT count(arch) FROM test WHERE package=? AND arch=? AND release=?",
                (package, arch, release),
            )
        else:
            c.execute(
                "SELECT count(arch) FROM test WHERE package=? AND arch=?",
                (package, arch),
            )
        return c.fetchone()[0] > 0

    def is_valid_package_version(self, release, package, version, ppa=None):
        """Check if package/version exists in the given release.

        Use this for validating trigger packages. This queries the Launchpad
        REST API. Check given ppa (team/name), or the main Ubuntu archive if
        this is None.

        Return the component name if package/version exists, otherwise None.
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
        }
        if version is not None:
            req["version"] = json.dumps(version)
        (code, response) = self.lp_request(obj, req)
        if code < 200 or code >= 300:
            return None
        logging.debug(
            "is_valid_package_version(%s, %s, %s): code %u, response %s",
            release,
            package,
            version,
            code,
            repr(response),
        )
        if response.get("total_size", 0) > 0:
            return response["entries"][0]["component_name"]
        else:
            return None

    def can_upload(self, person, release, component, package):
        """Check if person can upload package into Ubuntu release."""
        # https://launchpad.net/+apidoc/1.0.html#archive-checkUpload
        (code, response) = self.lp_request(
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
        return code >= 200 and code < 300

    def in_allowed_team(self, person):
        """Check if person is allowed to queue tests."""
        cached_entry = self.allowed_user_cache.get(person)
        if cached_entry is not None:
            cached_entry = datetime.fromtimestamp(float(cached_entry))
            cache_age = datetime.now() - cached_entry
            if cache_age <= ALLOWED_USER_CACHE_TIME:
                return True
            else:
                self.allowed_user_cache.delete(person)

        # In the case someone is in more than 300 teams, and the first
        # 300 teams are alphabetically before "autopkgtest-requestors",
        # the following will fail.
        _, response = self.lp_request(f"~{person}/super_teams?ws.size=300", {})
        entries = response.get("entries")
        for e in entries:
            for team in self.config["web"]["allowed_requestors"].split(","):
                if team == e["name"]:
                    self.allowed_user_cache.set(person, time())
                    return True
        return False

    @classmethod
    def lp_request(cls, obj, query):
        """Do a Launchpad REST request.

        Request https://api.launchpad.net/1.0/<obj>?<query>.

        Return (code, json), where json is defined for successful codes
        (200 <= code < 300) and None otherwise.
        """
        url = LP + obj + "?" + urllib.parse.urlencode(query)
        try:
            with urllib.request.urlopen(url, timeout=60) as req:
                code = req.getcode()
                if code >= 300:
                    logging.error("URL %s failed with code %u", req.geturl(), code)
                    return (code, None)
                response = req.read()
        except HTTPError as e:
            logging.error("%s failed with %u: %s\n%s", url, e.code, e.reason, e.headers)
            return (e.code, None)

        try:
            response = json.loads(response.decode("UTF-8"))
        except (UnicodeDecodeError, ValueError) as e:
            logging.error(
                "URL %s gave invalid response %s: %s",
                req.geturl(),
                response,
                str(e),
            )
            return (500, None)
        logging.debug("lp_request %s succeeded: %s", url, response)
        return (code, response)

    def is_test_running(
        self,
        req_series,
        req_arch,
        req_package,
        req_triggers,
        kwargs,
        ppas,
        git,
    ):
        if not os.path.isfile(self.config["web"]["running_cache"]):
            return False
        data = {}
        with open(self.config["web"]["running_cache"]) as f:
            data = json.load(f)
        if data == {}:
            return False
        for pkg in data:
            if pkg != req_package:
                continue
            for submitted in data[pkg]:
                releases = data[pkg][submitted].keys()
                for release in data[pkg][submitted]:
                    architectures = data[pkg][submitted][release].keys()
                    for arch in architectures:
                        triggers = data[pkg][submitted][release][arch][0].get(
                            "triggers", []
                        )
                        running_all_proposed = "all-proposed_1" in submitted
                        req_all_proposed = "all-proposed" in kwargs.keys()
                        git_same = False
                        if git and "build-git" in submitted:
                            build_git_url = data[pkg][submitted][release][arch][0].get(
                                "build-git", []
                            )
                            ppas_running = data[pkg][submitted][release][arch][0].get(
                                "ppas", []
                            )
                            env = data[pkg][submitted][release][arch][0].get("env", [])
                            if (
                                kwargs.get("build-git", "") == build_git_url
                                and ppas_running == ppas
                                and (set(kwargs.get("env", [])) == set(env))
                            ):
                                git_same = True
                        if (
                            req_arch in architectures
                            and req_series in releases
                            and req_package == pkg
                            and sorted(triggers) == sorted(req_triggers)
                            and (running_all_proposed == req_all_proposed)
                            and (not git or git_same)
                        ):
                            return True
        return False

    def is_test_in_queue(
        self,
        req_series,
        req_arch,
        req_package,
        req_triggers,
        kwargs,
        ppas,
        git,
    ):
        if not os.path.isfile(self.config["web"]["amqp_queue_cache"]):
            return False
        data = {}
        with open(self.config["web"]["amqp_queue_cache"]) as f:
            data = json.load(f)
        data = data["queues"]
        this_test = {
            "release": req_series,
            "arch": req_arch,
            "package": req_package,
            "triggers": sorted(req_triggers),
        }
        for test_type in data:
            # Because the huge queue is huge it is possible some tests won't
            # run for quite some time. To shortcut the wait developers should
            # have the ability to request the same items run in the "normal"
            # queue which will execute sooner.
            if test_type == "huge":
                continue
            for release in data[test_type]:
                for arch in data[test_type][release]:
                    packages = data[test_type][release][arch]
                    if req_package not in packages:
                        continue
                    if packages["size"] != 0:
                        for req in packages["requests"]:
                            pkg = req[: req.find("{")].rstrip()
                            try:
                                details = json.loads(req[req.find("{") :])
                            except json.decoder.JSONDecodeError:
                                return False
                            triggers = details.get("triggers", [])
                            running_all_proposed = (
                                "all-proposed"
                                in data[test_type][release][arch]["requests"]
                            )
                            req_all_proposed = "all-proposed" in kwargs.keys()
                            test = {
                                "release": release,
                                "arch": arch,
                                "package": pkg,
                                "triggers": sorted(triggers),
                            }
                            git_same = False
                            if git and "build-git" in details:
                                build_git_url = details.get("build-git", [])
                                ppas_queued = details.get("ppas", [])
                                env = details.get("env", [])
                                if (
                                    kwargs.get("build-git", "") == build_git_url
                                    and ppas_queued == ppas
                                    and (set(kwargs.get("env", [])) == set(env))
                                ):
                                    git_same = True
                            if (
                                test == this_test
                                and running_all_proposed != req_all_proposed
                                and (not git or git_same)
                            ):
                                return True
        return False

    def is_request_queued_or_running(
        self,
        req_series,
        req_arch,
        req_package,
        req_triggers=[],
        kwargs={},
        ppas=[],
        git=False,
    ):
        if self.is_test_running(
            req_series, req_arch, req_package, req_triggers, kwargs, ppas, git
        ):
            raise RequestRunning(req_series, req_package, req_arch, req_triggers)

        if self.is_test_in_queue(
            req_series, req_arch, req_package, req_triggers, kwargs, ppas, git
        ):
            raise RequestInQueue(req_series, req_package, req_arch, req_triggers)

    def validate_args(self, parameters):
        base = ["arch", "release", "package", "triggers"]
        if not set(base).issubset(set(parameters.keys())):
            raise InvalidArgs(parameters)
