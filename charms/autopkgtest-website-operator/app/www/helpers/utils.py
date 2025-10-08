"""
utilities for autopkgtest-web webcontrol
"""

import configparser
import logging
import os
import pathlib
import random
import signal
import sqlite3
import subprocess
import time
import typing
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

import distro_info
import pika
import pygit2
import swiftclient

sqlite3.paramstyle = "named"


@dataclass
class SqliteWriterConfig:
    writer_exchange_name = "sqlite-write-me.fanout"
    checkpoint_interval = 5  # minutes
    amqp_entry_fields = [
        "run_id",
        "version",
        "triggers",
        "duration",
        "exitcode",
        "requester",
        "env",
        "uuid",
        "release",
        "arch",
        "package",
    ]
    retry_time_limit = 120  # seconds


def zstd_compress(data: bytes) -> bytes:
    p = subprocess.run(
        ["zstd", "--compress"], input=data, capture_output=True, check=True
    )
    return p.stdout


def zstd_decompress(data: bytes) -> bytes:
    p = subprocess.run(
        ["zstd", "--decompress"], input=data, capture_output=True, check=True
    )
    return p.stdout


def read_config_file(filepath: typing.Union[str, pathlib.Path], cfg_key: str = None):
    """
    Reads a given config file, whether it be a key=value env file or
    properly formatted config file

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
        with open(filepath, "r") as f:
            config.read_file(f)
        return config
    else:
        with open(filepath, "r") as fp:
            config.read_string(
                ("[%s]\n" % cfg_key) + fp.read().replace('"', "")
            )  # read_string preserves "" quotes
        return config


def get_autopkgtest_cloud_conf():
    try:
        return read_config_file(pathlib.Path("/etc/autopkgtest-cloud.conf"))
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


def get_autopkgtest_db_conn():
    """Get connection to autopkgtest db from config

    :return conn: ``sqlite3.Connection``
    """
    cp = get_autopkgtest_cloud_conf()
    return sqlite3.connect(
        "file:%s?mode=ro" % cp["web"]["database_ro"],
        uri=True,
        check_same_thread=False,
    )


def get_stats_cache():
    """Return path object representing the location of the autopkgtest
    stats cache.

    :return path: ``pathlib.Path``
    """
    return Path(get_autopkgtest_cloud_conf()["web"]["stats_cache_dir"]).expanduser()


def get_ppa_containers_cache():
    """Return path object representing the location of the ppa
    containers cache.

    :return path: ``pathlib.Path``
    """
    return Path(
        get_autopkgtest_cloud_conf()["web"]["ppa_containers_cache_dir"]
    ).expanduser()


def get_all_releases():
    udi = distro_info.UbuntuDistroInfo()
    return udi.all


def get_release_arches(db_con):
    """Determine available releases and architectures

    :param db_con:
        sqlite3 connection to autopkgtest db
    :type db_con: ``sqlite3.Connection``
    :return ``dict(release -> [arch])``:
    """
    udi = distro_info.UbuntuDistroInfo()
    all_ubuntu_releases = udi.all

    supported_ubuntu_release = sorted(
        set(udi.supported() + udi.supported_esm()),
        key=all_ubuntu_releases.index,
    )

    release_arches = {}
    releases = []
    for row in db_con.execute("SELECT DISTINCT release from test"):
        if row[0] in supported_ubuntu_release:
            releases.append(row[0])
    for r in releases:
        for row in db_con.execute(
            "SELECT DISTINCT arch from test WHERE release=?", (r,)
        ):
            release_arches.setdefault(r, []).append(row[0])
    return release_arches


def get_source_versions(db_con, release):
    """Get latest version of packages for given release

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


def get_supported_releases():
    udi = distro_info.UbuntuDistroInfo()
    all_ubuntu_releases = get_all_releases()
    return sorted(
        set(udi.supported() + udi.supported_esm()),
        key=all_ubuntu_releases.index,
    )


def get_github_context(params: typing.Dict[str, str]) -> str:
    if "testname" in params:
        return "%s/%s %s" % (
            params["release"],
            params["arch"],
            params["testname"],
        )
    else:
        return "%s/%s" % (params["release"], params["arch"])


def srchash(src: str) -> str:
    """Get srchash of package name

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


def get_indexed_packages():
    db_con = get_autopkgtest_db_conn()
    indexed_packages = {}

    for row in db_con.execute(
        "SELECT package, MAX(version) "
        "FROM test, result "
        "WHERE id == test_id "
        "AND version != 'unknown' "
        "GROUP BY package "
        "ORDER BY package"
    ):
        # strip off epoch
        v = row[1][row[1].find(":") + 1 :]
        indexed_packages.setdefault(srchash(row[0]), []).append((row[0], v))

    return indexed_packages


class timeout:
    def __init__(self, seconds=1, error_message="Timeout"):
        self.seconds = seconds
        self.error_message = error_message

    def handle_timeout(self, signum, frame):
        raise TimeoutError(self.error_message)

    def __enter__(self):
        signal.signal(signal.SIGALRM, self.handle_timeout)
        signal.alarm(self.seconds)

    def __exit__(self, type, value, traceback):
        signal.alarm(0)


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


def init_db(path, **kwargs):
    """Create DB if it does not exist, and connect to it"""
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    db = sqlite3.connect(path, **kwargs)
    c = db.cursor()
    try:
        c.execute("PRAGMA journal_mode = WAL")
        c.execute(
            "CREATE TABLE IF NOT EXISTS test ("
            "  id INTEGER PRIMARY KEY, "
            "  release CHAR[20], "
            "  arch CHAR[20], "
            "  package char[120])"
        )
        c.execute(
            "CREATE TABLE IF NOT EXISTS result ("
            "  test_id INTEGER, "
            "  run_id CHAR[30], "
            "  version VARCHAR[200], "
            "  triggers TEXT, "
            "  duration INTEGER, "
            "  exitcode INTEGER, "
            "  requester TEXT, "
            "  env TEXT, "
            "  uuid TEXT UNIQUE,  "
            "  PRIMARY KEY(test_id, run_id), "
            "  FOREIGN KEY(test_id) REFERENCES test(id))"
        )
        # /packages/<name> mostly benefits from the index on package (0.8s -> 0.01s),
        # but adding the other fields improves it a further 50% to 0.005s.
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS test_package_uix ON test("
            "  package, release, arch)"
        )
        c.execute("CREATE INDEX IF NOT EXISTS result_run_ix ON result(  run_id desc)")
        # /user/<username> page benefits greatly from this idx
        # Prior to idx, this page would take ~90s to load, down to ~5s.
        c.execute(
            "CREATE INDEX IF NOT EXISTS result_requester_idx ON result(requester) "
        )
        # /admin mostly benefits from the index on test_id (~80s -> 50ms)
        # /packages/<name> also sees some improvements (~14s -> 30ms)
        c.execute("CREATE INDEX IF NOT EXISTS result_test_id_ix ON result(test_id);")
        # exact same pages (/admin and /packages/<name>) goes from (~50ms to ~3ms)
        # with this other index
        c.execute("CREATE INDEX IF NOT EXISTS test_id_ix ON test(id);")
        db.commit()
        logging.debug("database %s created", path)
    except sqlite3.OperationalError as e:
        if "already exists" not in str(e):
            raise
        logging.debug("database %s already exists", path)

    return db


def amqp_connect():
    """Connect to AMQP server"""

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


def get_test_id(db_con, release, arch, src):
    """
    get id of test
    """
    if not get_test_id._cache:
        # prime the cache with all test IDs; much more efficient than doing
        # thousands of individual queries
        c = db_con.cursor()
        c.execute("SELECT * FROM test")
        while True:
            row = c.fetchone()
            if row is None:
                break
            get_test_id._cache[row[1] + "/" + row[2] + "/" + row[3]] = row[0]

    cache_idx = release + "/" + arch + "/" + src
    try:
        return get_test_id._cache[cache_idx]
    except KeyError:
        # create new ID
        c = db_con.cursor()
        while True:
            try:
                insert_me = {
                    "id": None,
                    "release": release,
                    "arch": arch,
                    "package": src,
                }
                c.execute(
                    (
                        "INSERT INTO test(id, release, arch, package) "
                        "VALUES (:id, :release, :arch, :package)"
                    ),
                    insert_me,
                )
            except sqlite3.IntegrityError:
                # our cache got out of date in the meantime
                c.execute(
                    "SELECT id from test where release "
                    + "= ? and arch = ? and package = ?",
                    (release, arch, src),
                )
                test_id = c.fetchone()[0]
                break
            except sqlite3.OperationalError as e:
                if "database is locked" in str(e):
                    sleep_time = random.uniform(0.1, 2)
                    logging.info(
                        "database is currently locked, waiting %f seconds and trying again..."
                        % sleep_time
                    )
                    time.sleep(sleep_time)
                else:
                    logging.info("insert operation failed with: %s" % str(e))
                    break
            else:
                test_id = c.lastrowid
                db_con.commit()
                break
        get_test_id._cache[cache_idx] = test_id
        return test_id


def swift_connect() -> swiftclient.Connection:
    """
    Establish connection to swift storage
    """
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


def is_db_empty(db_con):
    cursor = db_con.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table';")
    tables = cursor.fetchall()
    if len(tables) == 0:
        return True
    for table in tables:
        cursor.execute(f"SELECT * FROM {table[0]};")
        entries = cursor.fetchall()
        if len(entries) > 0:
            return False
    return True


def get_db_path():
    return get_autopkgtest_cloud_conf()["web"]["database"]


def get_repo_head_commit_hash(repo_dir) -> str:
    try:
        repo = pygit2.Repository(repo_dir)
        return str(repo.head.target)
    except pygit2.GitError:
        return None


get_test_id._cache = {}
