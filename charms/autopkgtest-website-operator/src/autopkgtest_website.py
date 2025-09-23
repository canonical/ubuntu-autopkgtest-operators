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

import jinja2

# import charms.operator_libs_linux.v0.passwd as passwd
import charms.operator_libs_linux.v0.apt as apt
import charms.operator_libs_linux.v1.systemd as systemd

logger = logging.getLogger(__name__)

# Unprivileged user and group
# GROUP = "www-data"

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
    "distro-info",
    "git",
    "jq",
    "python3-amqp",
    "python3-distro-info",
    "python3-flask",
    "python3-flask-openid",
    "python3-influxdb",
    "python3-pygit2",
    "python3-swiftclient",
    "python3-werkzeug",
    "zstd",
]


def install() -> None:
    """Install website"""

    logger.info("Updating package index")
    apt.update()

    logger.info("Installing packages")
    apt.add_package(PACKAGES)

    logger.info("Creating directories")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PUBLIC_DATA_DIR.mkdir(exist_ok=True)
    WWW_DIR.mkdir(exist_ok=True)
    shutil.chown(DATA_DIR, user="www-data", group="www-data")
    shutil.chown(PUBLIC_DATA_DIR, user="www-data", group="www-data")

    logger.info("Installing website")
    shutil.copytree(CHARM_APP_DATA / "www", WWW_DIR, dirs_exist_ok=True)
    os.symlink(Path("/usr/share/javascript/bootstrap"), WWW_DIR / "static/bootstrap")
    os.symlink(Path("/usr/share/javascript/jquery"), WWW_DIR / "static/jquery")


def configure(
    *,
    hostname: str,
) -> None:
    """Configuring service"""
    logger.info("Stopping apache2")
    systemd.service_stop("apache2")

    logger.info("Making runtime tmpfiles")
    with open("/etc/tmpfiles.d/autopkgtest-web-runtime.conf", "w") as f:
        f.write("D %t/autopkgtest_webcontrol 0755 www-data www-data\n")
    subprocess.check_call(["systemd-tmpfiles", "--create"])

    logger.info("Configuring apache2")
    subprocess.check_call(["a2dissite", "000-default"])
    subprocess.check_call(["a2dismod", "mpm_event", "mpm_worker"])
    subprocess.check_call(
        ["a2enmod", "mpm_prefork", "include", "cgi", "proxy", "proxy_http", "remoteip"]
    )

    j2env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(CHARM_APP_DATA / "config")
    )
    j2template = j2env.get_template("a2-autopkgtest.conf.j2")
    j2context = {
        "http_port": 80,
        "documentroot": WWW_DIR,
        "servername": hostname,
    }
    with open(SITES_AVAILABLE_PATH / "autopkgtest.conf", "w") as f:
        f.write(j2template.render(j2context))
    subprocess.run(["a2ensite", "autopkgtest"])

    logger.info("Generating autopkgtest config")
    j2template = j2env.get_template("autopkgtest-cloud.conf.j2")
    j2context = {
        "database": DATA_DIR / "autopkgtest.db",
        "database_ro": PUBLIC_DATA_DIR / "autopkgtest.db",
    }
    conf_file = Path("/etc/autopkgtest-cloud.conf")
    with open(conf_file, "w") as f:
        f.write(j2template.render(j2context))

    logger.info("Installing systemd units")
    system_units_dir = Path("/etc/systemd/system/")
    units_to_install = {"autopkgtest-web.target", "publish-db.timer"}
    units_to_enable = {"autopkgtest-web.target", "publish-db.timer"}
    for unit in units_to_install:
        shutil.copy(CHARM_APP_DATA / "units" / unit, system_units_dir)

    j2env = jinja2.Environment(loader=jinja2.FileSystemLoader(CHARM_APP_DATA / "units"))

    j2template = j2env.get_template("publish-db.service.j2")
    j2context = {
        "webcontrol": WWW_DIR,
    }
    with open(system_units_dir / "publish-db.service", "w") as f:
        f.write(j2template.render(j2context))

    systemd.daemon_reload()
    systemd.service_enable("--now", *units_to_enable)


def start() -> None:
    """Start the workload"""

    logger.info("Starting apache2")
    systemd.service_start("apache2")
