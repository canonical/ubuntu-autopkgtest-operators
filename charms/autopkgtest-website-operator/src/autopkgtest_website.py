# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Functions for managing and interacting with the workload.

The intention is that this module could be used outside the context of a charm.
"""

import logging
import os
import shutil
import subprocess
from pathlib import Path
from textwrap import dedent

import jinja2
from charmlibs import apt, systemd

logger = logging.getLogger(__name__)

# Unprivileged user and group
USER = "www-data"
GROUP = "www-data"

# Charm source path
CHARM_SOURCE_PATH = Path(__file__).parent.parent
CHARM_APP_DATA = CHARM_SOURCE_PATH / "app"

# Directories used by the charm
APP_DIR = Path("/srv/autopkgtest")
DATA_DIR = APP_DIR / "data"
PUBLIC_DATA_DIR = DATA_DIR / "public"
WWW_DIR = APP_DIR / "www"

# Config files create by the charm
SITES_AVAILABLE_PATH = Path("/etc/apache2/sites-available/")

# Packages to install
PACKAGES = [
    "apache2",
    "libjs-bootstrap",
    "libjs-jquery",
    "amqp-tools",
    "git",
    "jq",
    "python3-distro-info",
    "python3-flask",
    "python3-flask-openid",
    "python3-pika",
    "python3-pygit2",
    "python3-swiftclient",
    "python3-werkzeug",
]


def install() -> None:
    """Install website."""
    if "JUJU_CHARM_HTTPS_PROXY" in os.environ or "JUJU_CHARM_HTTP_PROXY" in os.environ:
        logger.info("Installing proxy environment file")
        Path("/etc/environment.d").mkdir(exist_ok=True)
        with open("/etc/environment.d/proxy.conf", "w") as file:
            file.write(
                dedent(
                    f"""\
                    http_proxy={os.getenv("JUJU_CHARM_HTTP_PROXY", "")}
                    https_proxy={os.getenv("JUJU_CHARM_HTTPS_PROXY", "")}
                    no_proxy={os.getenv("JUJU_CHARM_NO_PROXY", "")}
                    """
                )
            )

        # changed environment variables don't get picked up by this file
        # so set them explicitly
        os.environ["http_proxy"] = os.getenv("JUJU_CHARM_HTTP_PROXY", "")
        os.environ["https_proxy"] = os.getenv("JUJU_CHARM_HTTPS_PROXY", "")
        os.environ["no_proxy"] = os.getenv("JUJU_CHARM_NO_PROXY", "")

    logger.info("Updating package index")
    apt.update()

    logger.info("Installing packages")
    apt.add_package(PACKAGES)

    logger.info("Creating directories")
    # data
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    shutil.chown(DATA_DIR, user=USER, group=GROUP)
    # public data
    PUBLIC_DATA_DIR.mkdir(exist_ok=True)
    shutil.chown(PUBLIC_DATA_DIR, user=USER, group=GROUP)
    # tools
    shutil.copytree(CHARM_APP_DATA / "bin", "/usr/local/bin", dirs_exist_ok=True)

    logger.info("Installing website")
    shutil.rmtree(WWW_DIR, ignore_errors=True)
    shutil.copytree(
        CHARM_APP_DATA / "www",
        WWW_DIR,
        ignore=shutil.ignore_patterns("tests", "tests.py"),
    )
    os.symlink(Path("/usr/share/javascript/bootstrap"), WWW_DIR / "static/bootstrap")
    os.symlink(Path("/usr/share/javascript/jquery"), WWW_DIR / "static/jquery")
    os.symlink(DATA_DIR / "running.json", WWW_DIR / "static/running.json")
    os.symlink(PUBLIC_DATA_DIR / "autopkgtest.db", WWW_DIR / "static/autopkgtest.db")
    os.symlink(
        PUBLIC_DATA_DIR / "autopkgtest.db.sha256",
        WWW_DIR / "static/autopkgtest.db.sha256",
    )


def configure(
    *,
    hostname: str,
    releases: str,
    http_port: int,
    amqp_creds: dict[str, str],
    swift_creds: dict[str, str],
) -> None:
    """Configure service."""
    logger.info("Stopping apache2")
    systemd.service_stop("apache2")

    logger.info("Making runtime tmpfiles")
    with open("/etc/tmpfiles.d/autopkgtest-web-runtime.conf", "w") as f:
        f.write("D %t/autopkgtest_webcontrol 0755 www-data www-data\n")
    subprocess.run(["systemd-tmpfiles", "--create"], check=True)

    logger.info("Configuring apache2")
    subprocess.run(["a2dissite", "000-default"], check=True)
    subprocess.run(["a2dismod", "mpm_event", "mpm_worker"], check=True)
    subprocess.run(
        [
            "a2enmod",
            "mpm_prefork",
            "include",
            "cgi",
            "proxy",
            "proxy_http",
            "remoteip",
            "rewrite",
            "ssl",
        ],
        check=True,
    )

    j2env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(CHARM_APP_DATA / "conf"),
        autoescape=jinja2.select_autoescape(),
    )
    j2template = j2env.get_template("a2-autopkgtest.conf.j2")
    j2context = {
        "http_port": http_port,
        "documentroot": WWW_DIR,
        "servername": hostname,
        "https_proxy": os.getenv("JUJU_CHARM_HTTPS_PROXY", ""),
        "http_proxy": os.getenv("JUJU_CHARM_HTTP_PROXY", ""),
        "no_proxy": os.getenv("JUJU_CHARM_NO_PROXY", ""),
        **amqp_creds,
        **swift_creds,
    }
    with open(SITES_AVAILABLE_PATH / "autopkgtest.conf", "w") as f:
        f.write(j2template.render(j2context))
    subprocess.run(["a2ensite", "autopkgtest"])

    logger.info("Generating autopkgtest config")
    j2template = j2env.get_template("autopkgtest-cloud.conf.j2")
    j2context = {
        "hostname": hostname,
        "data": DATA_DIR,
        "database": DATA_DIR / "autopkgtest.db",
        "database_ro": PUBLIC_DATA_DIR / "autopkgtest.db",
        "releases": releases,
        **amqp_creds,
        **swift_creds,
    }
    conf_file = Path("/etc/autopkgtest-cloud.conf")
    with open(conf_file, "w") as f:
        f.write(j2template.render(j2context))

    logger.info("Installing systemd units")
    system_units_dir = Path("/etc/systemd/system/")
    units_to_install = [u.name for u in (CHARM_APP_DATA / "units").glob("*")]
    units_to_enable = [u.name for u in (CHARM_APP_DATA / "units").glob("*.timer")] + [
        "autopkgtest-db-writer.service",
        "autopkgtest-running-collector.service",
        "autopkgtest-queue-collector.service",
        "autopkgtest-stats.service",
    ]

    j2env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(CHARM_APP_DATA / "units"),
        autoescape=jinja2.select_autoescape(),
    )
    j2context = {
        "user": USER,
        "webcontrol": WWW_DIR,
        **swift_creds,
    }
    for unit in units_to_install:
        if unit.endswith(".j2"):
            unit_basename = unit.removesuffix(".j2")
            j2template = j2env.get_template(unit)
            with open(system_units_dir / unit_basename, "w") as f:
                f.write(j2template.render(j2context))
        else:
            shutil.copy(CHARM_APP_DATA / "units" / unit, system_units_dir)

    systemd.daemon_reload()
    systemd.service_enable("--now", *units_to_enable)


def start() -> None:
    """Start the workload."""
    systemd.service_start("apache2")
