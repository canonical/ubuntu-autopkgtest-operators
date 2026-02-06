"""utilities for autopkgtest-web webcontrol."""

import configparser
import logging
import os
import pathlib
import sqlite3
import urllib.parse
from pathlib import Path

import pika
import swiftclient

sqlite3.paramstyle = "named"


def read_config_file(filepath: str | pathlib.Path, cfg_key: str = None):
    """Read a given config file.

    Reads a given config file, whether it be a key=value env file or
    properly formatted config file.

    :param filepath:
        Path to config file. Can be a string or pathlib.Path
    :type filepath: ``type[str | pathlib.Path]``
    :param cfg_key:
        Variable only necessary when parsing a key=value env file.
        indicates the first key to be used e.g. cfg["cfg_key"]["env_file_value"]
        This is a configparser "section" name.
    :type cfg_key: ``str``
    :return config dict:
    """
    config = configparser.ConfigParser()
    if cfg_key is None:
        with open(filepath) as f:
            config.read_file(f)
        return config
    else:
        with open(filepath) as fp:
            config.read_string(
                (f"[{cfg_key}]\n") + fp.read().replace('"', "")
            )  # read_string preserves "" quotes
        return config


def get_autopkgtest_cloud_conf():
    try:
        return read_config_file(
            pathlib.Path("/etc/autopkgtest-website/autopkgtest-cloud.conf")
        )
    except (FileNotFoundError, RuntimeError, PermissionError):
        try:
            return read_config_file(
                pathlib.Path("~/autopkgtest-cloud.conf").expanduser()
            )
        except FileNotFoundError:
            try:
                return read_config_file(
                    pathlib.Path(__file__).parent.parent / "autopkgtest-cloud.conf"
                )
            except FileNotFoundError as fnfe:
                raise FileNotFoundError(
                    "No config file found. Have a look at %s"
                    % (
                        pathlib.Path(__file__).parent.parent
                        / "autopkgtest-cloud.conf.example"
                    )
                ) from fnfe


def get_stats_cache():
    """Return path of the the autopkgtest stats cache.

    :return path: ``pathlib.Path``
    """
    return Path(get_autopkgtest_cloud_conf()["web"]["stats_cache_dir"]).expanduser()


def get_ppa_containers_cache():
    """Return path of the of the ppa containers cache.

    :return path: ``pathlib.Path``
    """
    return Path(
        get_autopkgtest_cloud_conf()["web"]["ppa_containers_cache_dir"]
    ).expanduser()


def get_release_arches():
    """Determine available releases and architectures.

    :return ``dict(release -> [arch])``:
    """
    db_con = db_connect_readonly()
    cp = get_autopkgtest_cloud_conf()

    release_arches = {}
    releases = cp["web"]["releases"].split()
    for r in releases:
        for row in db_con.execute(
            "SELECT DISTINCT arch from test WHERE release=?", (r,)
        ):
            release_arches.setdefault(r, []).append(row[0])
    return release_arches


def get_source_versions(db_con, release):
    """Get latest version of packages for given release.

    :param db_con:
        sqlite3 connection for autopkgtest db
    :type db_con: ``sqlite3.Connection``
    :param release:
        release to get package versions for
    :type release: ``str``
    :return ``dict(package -> version)``:
    """
    srcs = {}
    for pkg, ver in db_con.execute(
        "SELECT package, version FROM current_version WHERE release = ?",
        (release,),
    ):
        srcs[pkg] = ver
    return srcs


def get_github_context(params: dict[str, str]) -> str:
    if "testname" in params:
        return "{}/{} {}".format(
            params["release"],
            params["arch"],
            params["testname"],
        )
    else:
        return "{}/{}".format(params["release"], params["arch"])


def srchash(src: str) -> str:
    """Get srchash of package name.

    :param src:
        package name
    :type src: ``str``
    :return ``str``:
        The first letter of package name, or if
        package name starts with 'lib' then
        'lib' + first letter
    """
    if src.startswith("lib"):
        return src[:4]
    else:
        return src[0]


def setup_key(app, path):
    """Create or load app.secret_key for cookie encryption."""
    try:
        with open(path, "rb") as f:
            app.secret_key = f.read()
    except FileNotFoundError:
        key = os.urandom(24)
        with open(path, "wb") as f:
            os.fchmod(f.fileno(), 0o600)
            f.write(key)
        app.secret_key = key


def amqp_connect():
    """Connect to AMQP server."""
    cp = get_autopkgtest_cloud_conf()
    amqp_uri = cp["amqp"]["uri"]
    parts = urllib.parse.urlsplit(amqp_uri, allow_fragments=False)
    amqp_con = pika.BlockingConnection(
        parameters=pika.ConnectionParameters(
            host=parts.hostname,
            credentials=pika.PlainCredentials(
                username=parts.username,
                password=parts.password,
            ),
        ),
    )
    logging.info("Connected to AMQP server at %s@%s", parts.username, parts.hostname)

    return amqp_con


def db_connect_readonly():
    """Get connection to autopkgtest db from config.

    :return conn: ``sqlite3.Connection``
    """
    cp = get_autopkgtest_cloud_conf()
    return sqlite3.connect(
        "file:{}?mode=ro".format(cp["web"]["database"]),
        uri=True,
    )


def swift_connect() -> swiftclient.Connection:
    """Establish connection to swift storage."""
    try:
        config = get_autopkgtest_cloud_conf()
        swift_creds = {
            "authurl": config["swift"]["os_auth_url"],
            "user": config["swift"]["os_username"],
            "key": config["swift"]["os_password"],
            "os_options": {
                "project_domain_name": config["swift"]["os_project_domain_name"],
                "project_name": config["swift"]["os_project_name"],
                "user_domain_name": config["swift"]["os_user_domain_name"],
            },
            "auth_version": "3",
        }
        swift_conn = swiftclient.Connection(**swift_creds)
        return swift_conn
    except KeyError as e:
        raise swiftclient.ClientException(repr(e))
