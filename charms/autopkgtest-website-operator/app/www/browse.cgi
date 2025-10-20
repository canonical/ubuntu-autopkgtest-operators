#!/usr/bin/env python3

"""Browse autopkgtest results."""

import gzip
import json
import os
import re
import sqlite3
import sys
import traceback
from collections import OrderedDict
from pathlib import Path
from wsgiref.handlers import CGIHandler

import distro_info
import flask
from helpers.exceptions import NotFound, RunningJSONNotFound
from helpers.utils import (
    db_connect_readonly,
    get_autopkgtest_cloud_conf,
    get_ppa_containers_cache,
    get_release_arches,
    get_stats_cache,
    setup_key,
    srchash,
    swift_connect,
)
from werkzeug.middleware.proxy_fix import ProxyFix

# Initialize app
PATH = os.path.join(
    os.path.sep, os.getenv("XDG_RUNTIME_DIR", "/run"), "autopkgtest_webcontrol"
)
os.makedirs(PATH, exist_ok=True)
app = flask.Flask("browse")
# we don't want a long cache, as we only serve files that are regularly updated
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 60

app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1)

secret_path = os.path.join(PATH, "secret_key")
setup_key(app, secret_path)

db_con = None
CONFIG = {}

UDI = distro_info.UbuntuDistroInfo()
ALL_UBUNTU_RELEASES = UDI.all
SUPPORTED_UBUNTU_RELEASES = [
    r for r in UDI.all if r in UDI.supported() + UDI.supported_esm()
]


def init_config():
    global CONFIG

    cp = get_autopkgtest_cloud_conf()

    CONFIG["swift_container_url"] = cp["web"]["external_swift_url"] + "/autopkgtest-%s"
    CONFIG["amqp_queue_cache"] = Path(cp["web"]["amqp_queue_cache"])
    CONFIG["running_cache"] = Path(cp["web"]["running_cache"])
    CONFIG["database"] = Path(cp["web"]["database_ro"])


def get_test_id(release, arch, src):
    c = db_con.cursor()
    c.execute(
        "SELECT id FROM test WHERE release=? AND arch=? AND package=?",
        (release, arch, src),
    )
    try:
        return c.fetchone()[0]
    except TypeError:
        return None


def get_running_jobs():
    try:
        with open(CONFIG["running_cache"]) as f:
            # package -> runhash -> release -> arch -> (params, duration, logtail)
            return json.load(f)
    except FileNotFoundError as e:
        raise RunningJSONNotFound(e) from e


def render(template, code=200, **kwargs):
    # sort the values passed in, so that releases are in the right order
    try:
        release_arches = OrderedDict()
        for k in sorted(kwargs["release_arches"], key=ALL_UBUNTU_RELEASES.index):
            release_arches[k] = kwargs["release_arches"][k]
        kwargs["release_arches"] = release_arches
    except KeyError:
        pass
    try:
        kwargs["releases"] = sorted(kwargs["releases"], key=ALL_UBUNTU_RELEASES.index)
    except KeyError:
        pass
    return (
        flask.render_template(
            template,
            base_url=flask.url_for("index_root"),
            static_url=flask.url_for("static", filename=""),
            **kwargs,
        ),
        code,
    )


def human_date(run_id):
    return re.sub(
        r"(\d\d\d\d)(\d\d)(\d\d)_(\d\d)(\d\d)(\d\d).*",
        r"\1-\2-\3 \4:\5:\6 UTC",
        run_id,
    )


def human_sec(secs):
    return "%ih %02im %02is" % (secs // 3600, (secs % 3600) // 60, secs % 60)  # noqa: UP031


def human_exitcode(code):
    if code in (0, 2):
        return "pass"
    elif code in (4, 6, 12, 14):
        return "fail"
    elif code == 8:
        return "neutral"
    elif code == 99:
        return "denylisted"
    elif code == 16:
        return "tmpfail"
    elif code == 20:
        return "error"
    else:
        return "otherfail"


def get_queues_info():
    """Return information about queued tests.

    Return (releases, arches, context -> release -> arch -> (queue_size, [requests])).
    """
    with open(CONFIG["amqp_queue_cache"]) as json_file:
        queue_info_j = json.load(json_file)

        arches = queue_info_j["arches"]
        queues = queue_info_j["queues"]

        ctx = {}

        for context in queues:
            for release in queues[context]:
                for arch in queues[context][release]:
                    requests = queues[context][release][arch]["requests"]
                    size = queues[context][release][arch]["size"]
                    ctx.setdefault(context, {}).setdefault(release, {})[arch] = (
                        size,
                        requests,
                    )

        return (SUPPORTED_UBUNTU_RELEASES, arches, ctx)


def db_has_result_requester_idx(cursor: sqlite3.Cursor):
    for row in cursor.execute("PRAGMA index_list('result')"):
        if row["name"] == "result_requester_idx":
            return True
    return False


def get_results(limit: int, offset: int = 0, **kwargs) -> list:
    requested_arch = kwargs.get("arch", None)
    requested_release = kwargs.get("release", None)
    requested_user = kwargs.get("user", None)

    results = []
    # We want to use sqlite3.Row here, so we need to create a cursor
    # as to not affect the overall db_con object, which could interfere
    # with other queries
    cursor = db_con.cursor()
    cursor.row_factory = sqlite3.Row
    if db_has_result_requester_idx(cursor):
        filters = []
        if requested_arch:
            filters.append("arch=:requested_arch")
        if requested_release:
            filters.append("release=:requested_release")
        if requested_user:
            filters.append("requester=:requested_user")

        for row in cursor.execute(
            "SELECT test_id, run_id, version, triggers, "
            "duration, exitcode, requester, env, uuid, arch, package, release FROM result "
            "JOIN test on test_id = test.id "
            + ("WHERE " + (" AND ".join(filters)) + " " if filters else "")
            + "ORDER BY run_id DESC "
            "LIMIT :limit OFFSET :offset ",
            {
                "limit": limit,
                "offset": offset,
                "requested_arch": requested_arch,
                "requested_release": requested_release,
                "requested_user": requested_user,
            },
        ):
            arch = row["arch"]
            package = row["package"]
            release = row["release"]
            requester = row["requester"]
            triggers = row["triggers"]
            version = row["version"]
            additional_params = row[
                "env"
            ]  # string of comma separated env variables e.g. all-proposed=1,test-name=mytest
            code = human_exitcode(row["exitcode"])
            url = os.path.join(
                CONFIG["swift_container_url"] % release,
                release,
                arch,
                srchash(package),
                package,
                row["run_id"],
            )
            show_retry = code != "pass"
            all_proposed = (
                additional_params is not None and "all-proposed=1" in additional_params
            )
            results.append(
                dict(
                    version=version,
                    triggers=triggers,
                    additional_params=additional_params,
                    human_date=human_date(row["run_id"]),
                    human_sec=human_sec(int(row["duration"])),
                    requester=requester,
                    code=code,
                    url=url,
                    show_retry=show_retry,
                    all_proposed=all_proposed,
                    uuid=row["uuid"],
                    package=package,
                    release=release,
                    arch=arch,
                )
            )
    else:
        # If we reach this block, we need to signal to
        # the user that the issue is the db index not
        # being present
        results.append(
            dict(
                version="No results are being displayed as",
                triggers="the db index required for this page",
                additional_params="is not present, please contact an admin.",
                human_date="",
                human_sec="",
                requester=1,
                code="",
                url=False,
                show_retry=False,
                all_proposed="",
                uuid="",
                package="",
                release="",
                arch="",
            )
        )
    return results


def get_queued_for_user(user: str):
    queued_tests = []
    (_, _, queues_info) = get_queues_info()
    for _, queue in queues_info.items():
        for release, queue_by_arch in queue.items():
            for arch, queue_items in queue_by_arch.items():
                if queue_items[0] == 0:
                    continue
                requests = queue_items[1]
                for req in requests:
                    try:
                        req_info = json.loads(req.split("\n")[1])
                    except (json.JSONDecodeError, IndexError):
                        # These usually result from `private job` instances
                        continue
                    package = req.split("\n")[0]
                    if req_info.get("requester", "") == user:
                        queued_tests.append(
                            dict(
                                version="N/A",
                                triggers=req_info.get("triggers"),
                                additional_params="N/A",
                                human_date=human_date(req_info.get("submit-time")),
                                human_sec="N/A",
                                requester=user,
                                code="queued",
                                url="",
                                show_retry=False,
                                all_proposed="",
                                uuid=req_info.get("uuid", ""),
                                package=package,
                                release=release,
                                arch=arch,
                            ),
                        )
    return queued_tests


def get_running_for_user(user: str):
    running_tests = []
    for package, running_hash in get_running_jobs().items():
        for _, running in running_hash.items():
            for release, vals in running.items():
                for arch, list_of_running_items in vals.items():
                    if len(list_of_running_items) < 1:
                        continue
                    info_dict = list_of_running_items[0]
                    if info_dict.get("requester", "") == user:
                        running_tests.append(
                            dict(
                                version="N/A",
                                triggers=info_dict.get("triggers"),
                                additional_params="N/A",
                                human_date=human_date(info_dict.get("submit-time")),
                                human_sec=human_sec(int(list_of_running_items[1])),
                                requester=user,
                                code="running",
                                url="",
                                show_retry=False,
                                all_proposed="",
                                uuid=info_dict.get("uuid", "-"),
                                package=package,
                                release=release,
                                arch=arch,
                            ),
                        )
    return running_tests


@app.route("/")
def index_root():
    flask.session.permanent = True

    recent = []
    for row in db_con.execute(
        "SELECT exitcode, package, release, arch, triggers, uuid "
        "FROM result, test "
        "WHERE test.id == result.test_id "
        "ORDER BY run_id DESC "
        "LIMIT 10"
    ):
        hc = human_exitcode(row[0])
        res = hc if "code" not in hc else "fail"
        recent.append((res, row[1], row[2], row[3], row[4], row[5]))

    return render(
        "browse-home.html",
        recent_runs=recent,
    )


# backwards-compatible path with debci that specifies the source hash
@app.route("/packages/<_>/<package>")
@app.route("/packages/<package>")
def package_overview(package, _=None):
    results = {}
    arches = set()
    for row in db_con.execute(
        "SELECT MAX(run_id), exitcode, release, arch "
        "FROM test, result "
        "WHERE package = ? AND test.id = result.test_id "
        "GROUP BY release, arch",
        (package,),
    ):
        arches.add(row[3])
        results.setdefault(row[2], {})[row[3]] = human_exitcode(row[1])

    running_info = dict((k, v) for (k, v) in get_running_jobs().items() if k == package)

    try:
        (_, _, queues_info) = get_queues_info()
        for queue_name, queue in queues_info.items():
            for release in queue:
                for arch in queue[release]:
                    filtered_requests = [
                        r
                        for r in queue[release][arch][1]
                        if r.startswith(package + "\n")
                    ]
                    queues_info[queue_name][release][arch] = (
                        len(filtered_requests),  # update the size too
                        filtered_requests,
                    )
    except Exception:
        # We never want to fail in that block, even is there are issues with cache-amqp
        queues_info = {
            "error": {
                "no-release": {
                    "no-arch": [
                        1,
                        ["There are errors in cache-amqp"],
                    ]
                }
            }
        }

    return render(
        "browse-package.html",
        package=package,
        releases=[
            release
            for release in results.keys()
            if release in SUPPORTED_UBUNTU_RELEASES
        ],
        arches=sorted(arches),
        results=results,
        title_suffix=f"- {package}",
        running=running_info,
        queues_info=queues_info,
    )


def load_ppa_cache():
    ppa_containers = []
    cache_dir = get_ppa_containers_cache()
    try:
        cache_path = cache_dir / "ppa_containers.json"
        ppa_containers = json.load(cache_path.open())
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return ppa_containers


def get_container_ppa_name(user: str, container: str) -> str:
    """Given username and swift container name, get PPA name.

    Given a username and a swift container name, try to return
    the name of the ppa. Swift container names for PPA results follow
    the format autopkgtest-<series>-<username>-<ppa_name>.

    :return ``str`` or ``None``:
    """
    # strip out the first two parts of the container name
    stripped_container = "-".join(container.split("-")[2:])
    # without checking for a trailing hyphen, we could get false
    # positives like (user: user-ppa, container: autopkgtest-noble-user-ppa)
    # where the container belongs to a user named 'user' instead of 'user-ppa'
    if stripped_container.startswith(f"{user}-"):
        return container.split(f"{user}-")[1]
    return None


@app.route("/user/<user>/")
def user_overview(user):
    """Provide per-user view of autopkgtest results.

    This endpoint provides a "per-user" view for autopkgtest-cloud.
    It shows a page, much like the package/release/arch pages,
    except all of the queued, running and historical results are
    only shown if they were requested by the user provided.
    """

    def is_it_true(test_string):
        mapping_dict = {
            "true": True,
            "false": False,
        }
        return mapping_dict.get(test_string.lower(), True)

    args = flask.request.args
    try:
        limit = int(args.get("limit", 100))
        assert isinstance(limit, int)
        assert limit > 0
        assert limit <= 10000
    except (AssertionError, ValueError):
        limit = 100
    try:
        offset = int(args.get("offset", 0))
        assert offset > 0
        assert isinstance(offset, int)
    except (AssertionError, ValueError):
        offset = 0

    show_running = args.get("show-running", True, type=is_it_true)
    show_queued = args.get("show-queued", True, type=is_it_true)
    show_results = args.get("show-results", True, type=is_it_true)
    only_results = args.get("only-results", False, type=is_it_true)
    if only_results:
        show_running = False
        show_queued = False
        show_results = True  # just in case anyone tries fumbling with the args!

    # Get results for this user
    if show_results:
        previous_test_results = get_results(limit, offset, user=user)
    else:
        previous_test_results = dict()

    # add queued tests for this user
    if show_queued:
        queued_tests = get_queued_for_user(user)
    else:
        queued_tests = []

    # add running tests from this user
    if show_running:
        running_tests = get_running_for_user(user)
    else:
        running_tests = []

    return render(
        "browse-user.html",
        running_tests=running_tests,
        queued_tests=queued_tests,
        previous_test_results=previous_test_results,
        limit=limit,
        offset=offset,
        user=user,
    )


@app.route("/user/<user>/ppa")
def list_user_ppas(user):
    ppa_containers = load_ppa_cache()

    results = {}
    for container in ppa_containers:
        ppa_name = get_container_ppa_name(user, container)
        if ppa_name:
            release = container.split("-")[1]
            if ppa_name in results:
                results[ppa_name].append(release)
            else:
                results[ppa_name] = [release]

    return render(
        "browse-user-ppas.html",
        user=user,
        ppas=results,
    )


@app.route("/user/<user>/ppa/<ppa>")
def list_ppa_runs(user, ppa):
    ppa_containers = load_ppa_cache()

    target_containers = []
    # try to find all releases for this PPA
    for container in ppa_containers:
        if f"{user}-{ppa}" in container:
            target_containers.append(container)

    swift_con = swift_connect()

    test_runs = {}
    for target_container in target_containers:
        (_, container_objs) = swift_con.get_container(target_container)
        for obj in container_objs:
            name = obj["name"]
            if not name.endswith("log.gz"):
                continue
            name_parts = name.split("/")
            identifier = name_parts[4]
            release = name_parts[0]
            arch = name_parts[1]
            package = name_parts[3]
            date = human_date(identifier)

            if package not in test_runs:
                test_runs[package] = {}

            test_runs[package][identifier] = {
                "release": release,
                "arch": arch,
                "date": date,
                "fullname": "/".join(name_parts[:-1]),
                "container_name": target_container,
            }

    return render(
        "browse-ppa.html",
        ppa_name=f"{user}/{ppa}",
        test_runs=test_runs,
    )


@app.route("/recent")
@app.route("/api/experimental/recent.json")
def recent():
    """Provide recent results.

    This endpoint provides recent results where recent means that the test is
    among the last limit results (default = 100 and 0 < limit <= 10000).
    The page includes details such as version, triggers, requester, result,
    log, ...
    """
    args = flask.request.args
    try:
        limit = int(args.get("limit", 100))
        assert limit > 0
        assert limit <= 10000
    except (AssertionError, ValueError):
        limit = 100

    try:
        offset = int(args.get("offset", 0))
        assert offset > 0
        assert isinstance(offset, int)
    except (AssertionError, ValueError):
        offset = 0

    arch = args.get("arch")
    release = args.get("release")
    user = args.get("user")

    recent_test_results = get_results(
        limit, offset, arch=arch, release=release, user=user
    )

    if flask.request.path.endswith(".json"):
        return flask.jsonify(recent_test_results)

    else:
        all_arches = set()
        all_releases = []
        for r, arches in get_release_arches().items():
            all_releases.append(r)
            for a in arches:
                all_arches.add(a)

        return render(
            "browse-recent.html",
            running_tests=[],
            queued_tests=[],
            recent_test_results=recent_test_results,
            limit=limit,
            offset=offset,
            releases=all_releases,
            all_arches=all_arches,
            arch=arch or "",
            release=release or "",
        )


# backwards-compatible path with debci that specifies the source hash
@app.route("/packages/<_>/<package>/<release>/<arch>")
@app.route("/packages/<package>/<release>/<arch>")
def package_release_arch(package, release, arch, _=None):
    test_id = get_test_id(release, arch, package)
    if test_id is None:
        return render(
            "browse-results.html",
            package=package,
            release=release,
            arch=arch,
            package_results=[],
            title_suffix=f"- {package}/{release}/{arch}",
        )

    seen = set()
    results = []
    for row in db_con.execute(
        "SELECT run_id, version, triggers, duration, exitcode, requester, env, uuid FROM result "
        "WHERE test_id=? "
        "ORDER BY run_id DESC",
        (test_id,),
    ):
        requester = row[5] if row[5] else "-"
        code = human_exitcode(row[4])
        version = row[1]
        triggers = row[2]
        additional_params = row[
            6
        ]  # string of comma separated env variables e.g. all-proposed=1,test-name=mytest

        identifier = (
            version,
            triggers,
        )  # Version + triggers uniquely identifies this result
        show_retry = code != "pass" and identifier not in seen
        seen.add(identifier)
        url = os.path.join(
            CONFIG["swift_container_url"] % release,
            release,
            arch,
            srchash(package),
            package,
            row[0],
        )
        all_proposed = (
            additional_params is not None and "all-proposed=1" in additional_params
        )
        results.append(
            dict(
                version=version,
                triggers=triggers,
                additional_params=additional_params,
                human_date=human_date(row[0]),
                human_sec=human_sec(int(row[3])),
                requester=requester,
                code=code,
                url=url,
                show_retry=show_retry,
                all_proposed=all_proposed,
                uuid=row[7],
            )
        )

    # Add running jobs if any
    try:
        for _, running_jobs in get_running_jobs().get(package, {}).items():
            job = running_jobs.get(release, {}).get(arch, {})
            if job:
                results.insert(
                    0,
                    dict(
                        version="N/A",
                        triggers=job[0].get("triggers"),
                        additional_params="N/A",
                        human_date=human_date(job[0].get("submit-time")),
                        human_sec=human_sec(int(job[1])),
                        requester=job[0].get("requester", "-"),
                        code="running",
                        url="",
                        show_retry=False,
                        all_proposed="",
                        uuid=job[0].get("uuid", "-"),
                    ),
                )
    except Exception:
        # We never want to fail in that block, even is there are issues with cache-amqp
        # Let's signal the error in the page, but still display other results
        results.insert(
            0,
            dict(
                version="Unknown running list",
                triggers="There are errors in running.json",
                additional_params="",
                human_date="",
                human_sec="",
                requester="",
                code="",
                url="",
                show_retry=False,
                all_proposed="",
                uuid="",
            ),
        )

    # Add queued jobs if any
    try:
        (_, _, queues_info) = get_queues_info()
        for _, queue in queues_info.items():
            queue_items = queue.get(release, {}).get(arch, [0, []])[1]
            for item in queue_items:
                if item.startswith(package + "\n"):
                    item_info = json.loads(item.split("\n")[1])
                    results.insert(
                        0,
                        dict(
                            version="N/A",
                            triggers=item_info.get("triggers"),
                            additional_params=(
                                "all-proposed=1"
                                if "all-proposed" in item_info.keys()
                                else ""
                            ),
                            human_date=human_date(item_info.get("submit-time")),
                            human_sec="N/A",
                            requester="-",
                            code="queued",
                            url="",
                            show_retry=False,
                            all_proposed="",
                            uuid=item_info.get("uuid", ""),
                        ),
                    )
    except Exception:
        # We never want to fail in that block, even is there are issues with cache-amqp
        # Let's signal the error in the page, but still display other results
        results.insert(
            0,
            dict(
                version="Unknown queued list",
                triggers="There are errors in cache-amqp",
                additional_params="",
                human_date="",
                human_sec="",
                requester="",
                code="",
                url="",
                show_retry=False,
                all_proposed="",
                uuid="",
            ),
        )

    return render(
        "browse-results.html",
        package=package,
        release=release,
        arch=arch,
        package_results=results,
        title_suffix=f"- {package}/{release}/{arch}",
    )


@app.route("/run/<uuid>")
def get_by_uuid(uuid):
    package = ""
    release = ""
    arch = ""
    cursor = db_con.cursor()
    cursor.row_factory = sqlite3.Row
    result = cursor.execute(
        "SELECT run_id, version, triggers, duration, exitcode, requester, "
        "env, uuid, package, release, arch FROM result "
        "LEFT JOIN test ON result.test_id = test.id "
        "WHERE uuid = ?",
        (uuid,),
    ).fetchone()

    if result is None:
        raise NotFound("uuid", uuid)

    requester = result["requester"] if result["requester"] else "-"
    code = human_exitcode(result["exitcode"])
    version = result["version"]
    triggers = result["triggers"]
    additional_params = result["env"]
    package = result["package"]
    release = result["release"]
    arch = result["arch"]

    show_retry = code != "pass"
    url = os.path.join(
        CONFIG["swift_container_url"] % release,
        release,
        arch,
        srchash(package),
        package,
        result["run_id"],
    )
    all_proposed = (
        additional_params is not None and "all-proposed=1" in additional_params
    )

    test_results = {
        "version": version,
        "triggers": triggers,
        "env": additional_params,
        "run_date": human_date(result["run_id"]),
        "duration": human_sec(int(result["duration"])),
        "requester": requester,
        "result": code,
        "url": url,
        "show_retry": show_retry,
        "all_proposed": all_proposed,
        "uuid": result["uuid"],
    }
    return render(
        "browse-run.html",
        package=package,
        release=release,
        arch=arch,
        test_results=test_results,
        title_suffix=f"- {package}/{release}/{arch}",
    )


@app.route("/run/<uuid>/log")
def display_run_logs(uuid):
    cursor = db_con.cursor()
    cursor.row_factory = sqlite3.Row
    result = cursor.execute(
        "SELECT run_id, version, triggers, duration, exitcode, requester, "
        "env, uuid, package, release, arch FROM result "
        "LEFT JOIN test ON result.test_id = test.id "
        "WHERE uuid = ?",
        (uuid,),
    ).fetchone()

    if result is None:
        raise NotFound("uuid", uuid)

    arch = result["arch"]
    duration = result["duration"]
    package = result["package"]
    requester = result["requester"]
    release = result["release"]
    run_id = result["run_id"]

    conn = swift_connect()

    if package.startswith("lib") and len(package) > 3:
        prefix = package[:4]
    else:
        prefix = package[0]

    object_name = f"{release}/{arch}/{prefix}/{package}/{run_id}/log.gz"

    gz_log = conn.get_object(container=f"autopkgtest-{release}", obj=object_name)[1]
    text_log = gzip.decompress(gz_log).decode("utf-8")

    # split log into sections
    sections = []
    for line in text_log.split("\n"):
        if re.match(
            r"^\s*\d+s autopkgtest \[\d\d:\d\d:\d\d\]: starting date and time", line
        ):
            sections.append({"name": "Preparation", "subsections": []})
            sections[-1]["subsections"].append({"name": "start run", "lines": []})
        elif re.match(
            r"^\s*\d+s autopkgtest \[\d\d:\d\d:\d\d\]: @@@@@@@@@@@@@@@@@@@@ apt-source",
            line,
        ):
            sections[-1]["subsections"].append({"name": "apt-source", "lines": []})
        elif re.match(
            r"^\s*\d+s autopkgtest \[\d\d:\d\d:\d\d\]: @@@@@@@@@@@@@@@@@@@@ test bed setup",
            line,
        ):
            sections[-1]["subsections"].append({"name": "test bed setup", "lines": []})

        elif re.match(
            r"^\s*\d+s autopkgtest \[\d\d:\d\d:\d\d\]: test (.*): preparing testbed",
            line,
        ):
            match = re.match(
                r"^\s*\d+s autopkgtest \[\d\d:\d\d:\d\d\]: test (.*): preparing testbed",
                line,
            )
            sections.append(
                {"name": f"test {match.group(1)}", "subsections": [], "result": "skip"}
            )
            sections[-1]["subsections"].append(
                {"name": "preparing testbed", "lines": []}
            )
        elif re.match(
            r"^\s*\d+s autopkgtest \[\d\d:\d\d:\d\d\]: test (.*): \[-----------------------",
            line,
        ):
            sections[-1]["subsections"].append({"name": "test run", "lines": []})
        elif re.match(
            r"^\s*\d+s autopkgtest \[[0-9:]+\]: test (\S+):  - - - - - - - - - - results - - - - - - - - - -",
            line,
        ):
            sections[-1]["subsections"].append({"name": "test results", "lines": []})
        elif re.match(
            r"^\s*\d+s autopkgtest \[\d\d:\d\d:\d\d\]: @@@@@@@@@@@@@@@@@@@@ (summary)",
            line,
        ):
            sections.append({"name": "Closing", "subsections": []})
            sections[-1]["subsections"].append({"name": "summary", "lines": []})
        elif re.match(r"^\s*\d+s\s+badpkg:\s+", line):
            if "result" in sections[-1] and sections[-1]["name"] != "closing":
                sections[-1]["result"] = "fail"

        if (
            re.match(r"^\s*\d+s\s+\S+\s*(PASS|FAIL|SKIP)", line)
            and "result" in sections[-1]
        ):
            match = re.match(r"^\s*\d+s\s+\S+\s*(PASS|FAIL|SKIP)", line)
            sections[-1]["result"] = match.group(1).lower()

        if line.strip() != "":
            sections[-1]["subsections"][-1]["lines"].append(line)

    return render(
        "browse-log.html",
        arch=arch,
        duration=duration,
        package=package,
        requester=requester,
        release=release,
        sections=sections,
        uuid=uuid,
    )


@app.route("/running")
def running():
    (releases, arches, queues_info) = get_queues_info()
    queues_lengths = {}
    for c in queues_info:
        for r in releases:
            for a in arches:
                (queue_length, _) = queues_info.get(c, {}).get(r, {}).get(a, (0, []))
                queues_lengths.setdefault(c, {}).setdefault(r, {})[a] = queue_length

    running_info = get_running_jobs()
    packages = running_info.keys()
    running_count = 0
    for pkg in packages:
        running_count += len(running_info[pkg].keys())

    return render(
        "browse-running.html",
        releases=releases,
        arches=arches,
        queues_info=queues_info,
        queues_lengths=queues_lengths,
        running=running_info,
        running_count=running_count,
    )


@app.route("/statistics")
def statistics():
    release_arches = get_release_arches()

    # try to load from both system cache and user cache in order
    # in case autopkgtest-cloud is running locally
    data = dict()
    cache_dir = get_stats_cache()
    try:
        cache_path = cache_dir / "stats.json"
        data = json.load(cache_path.open())
    except (FileNotFoundError, json.JSONDecodeError):
        pass

    return render("browse-statistics.html", release_arches=release_arches, data=data)


if __name__ == "__main__":

    @app.errorhandler(Exception)
    def all_exception_handler(exception):
        # If the exception doesn't have the exit_code method, it's not an expected
        # exception defined in helpers/exceptions.py
        try:
            exit_code = exception.exit_code()
        except AttributeError:
            # werkzeug exceptions have a code, otherwise let's default to a generic 500
            try:
                exit_code = exception.code
            except AttributeError:
                exit_code = 500

        # this can be simplified to the following after we move past Python 3.10
        # traceback.print_exception(error)
        exc_type, exc_value, exc_traceback = sys.exc_info()
        traceback.print_exception(exc_type, exc_value, exc_traceback)
        return render(
            "browse-error.html",
            error=exception,
            tb=traceback.format_exception(exc_type, exc_value, exc_traceback),
            exit_code=exit_code,
            code=exit_code,
        )

    app.config["DEBUG"] = True
    init_config()
    db_con = db_connect_readonly()
    CGIHandler().run(app)
