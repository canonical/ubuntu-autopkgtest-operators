#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import os
import shutil
import subprocess
from pathlib import Path
from textwrap import dedent

import jinja2
from charmlibs import apt, snap, systemd
from systemd_helper import SystemdHelper

logger = logging.getLogger(__name__)

USER = "ubuntu"
AUTOPKGTEST_REPO = "https://salsa.debian.org/ubuntu-ci-team/autopkgtest.git"
AUTOPKGTEST_LOCATION = Path(f"~{USER}/autopkgtest").expanduser()

AUTOPKGTEST_PACKAGE_CONFIG_REPO = "https://git.launchpad.net/~ubuntu-release/autopkgtest-cloud/+git/autopkgtest-package-configs"
AUTOPKGTEST_PACKAGE_CONFIG_BRANCH = "main"
AUTOPKGTEST_PACKAGE_CONFIG_LOCATION = Path(
    f"~{USER}/autopkgtest-package-configs"
).expanduser()

DEB_DEPENDENCIES = [
    "python3-pika",
    "python3-swiftclient",
    # autopkgtest dependencies
    "apt-utils",
    "autodep8",
    "libdpkg-perl",
    "mawk",
    "python3-debian",
    "python3-distro-info",
    "retry",
]
SNAP_DEPENDENCIES = [{"name": "lxd", "channel": "6/stable"}]

CONF_DIRECTORY = Path("/etc/autopkgtest-dispatcher")

RABBITMQ_CREDS_PATH = CONF_DIRECTORY / "rabbitmq.cred"

WORKER_CONFIG_PATH = CONF_DIRECTORY / "worker.conf"
SWIFT_CONFIG_PATH = CONF_DIRECTORY / "swift.cred"

# charm files path
CHARM_SOURCE_PATH = Path(__file__).parent.parent
CHARM_APP_DATA = CHARM_SOURCE_PATH / "app"

WORKER_TOOLS_DEST = Path("/usr/local/bin/")

systemd_helper = SystemdHelper()


def run_as_user(command: str):
    subprocess.run(
        [
            "su",
            "--login",
            "--whitelist-environment=https_proxy,http_proxy,no_proxy",
            USER,
            "--command",
            command,
        ],
        check=True,
    )


def write_worker_config(releases):
    with open(WORKER_CONFIG_PATH, "w") as file:
        file.write(
            dedent(
                f"""\
                [autopkgtest]
                checkout_dir = {AUTOPKGTEST_LOCATION}
                per_package_config_dir = {AUTOPKGTEST_PACKAGE_CONFIG_LOCATION}
                releases = {" ".join(releases)}
                setup_command =
                setup_command2 =
                retry_delay = 300
                debug = 0
                architectures =

                [virt]
                args = lxd $VMOPT -r $LXD_REMOTE $LXD_REMOTE:autopkgtest/ubuntu/$RELEASE/$ARCHITECTURE$VMFLAG
                """
            )
        )


def write_swift_config(swift_creds):
    with open(SWIFT_CONFIG_PATH, "w") as file:
        for k, v in swift_creds.items():
            file.write(f"{k.upper().replace('-', '_')}={v}\n")


def write_rabbitmq_creds(hostname, username, password):
    """Set rabbitmq creds."""
    with open(RABBITMQ_CREDS_PATH, "w") as file:
        file.write(
            dedent(
                f"""\
                RABBIT_HOST="{hostname}"
                RABBIT_USER="{username}"
                RABBIT_PASSWORD="{password}"
                """
            )
        )


def install(autopkgtest_branch, releases):
    """Install dispatcher."""
    if "JUJU_CHARM_HTTPS_PROXY" in os.environ or "JUJU_CHARM_HTTP_PROXY" in os.environ:
        logger.info("installing proxy environment file")
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

    logger.info("updating package index")
    apt.update()

    logger.info("installing packages")
    apt.add_package(DEB_DEPENDENCIES)
    for needed_snap in SNAP_DEPENDENCIES:
        snap.add(needed_snap["name"], channel=needed_snap["channel"])

    logger.info("creating directories")
    CONF_DIRECTORY.mkdir(exist_ok=True)

    logger.info("cloning repositories")
    for repo, branch, location in [
        (
            AUTOPKGTEST_REPO,
            autopkgtest_branch,
            AUTOPKGTEST_LOCATION,
        ),
        (
            AUTOPKGTEST_PACKAGE_CONFIG_REPO,
            AUTOPKGTEST_PACKAGE_CONFIG_BRANCH,
            AUTOPKGTEST_PACKAGE_CONFIG_LOCATION,
        ),
    ]:
        shutil.rmtree(location, ignore_errors=True)
        # TODO: the currently packaged version of pygit2 does not support cloning through
        # a proxy. the next release should hopefully include this feature.
        # pygit2.clone_repository(repo, location, checkout_branch=branch)
        run_as_user(f"git clone --depth 1 --branch '{branch}' '{repo}' '{location}'")

    logger.info("installing worker and tools")
    src_path = CHARM_APP_DATA / "bin"
    shutil.copy(src_path / "worker", WORKER_TOOLS_DEST)
    shutil.copy(src_path / "filter-amqp-dupes-upstream", WORKER_TOOLS_DEST)

    logger.info("writing worker config")
    write_worker_config(releases)

    logger.info("installing systemd units")
    units_path = CHARM_APP_DATA / "units"
    units_to_install = [u.name for u in (units_path).glob("*")]
    units_to_enable = [u.name for u in (units_path).glob("*.timer")]

    system_units_dir = Path("/etc/systemd/system/")
    j2env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(units_path),
        autoescape=jinja2.select_autoescape(),
    )
    j2context = {
        "user": USER,
        "conf_directory": CONF_DIRECTORY,
        "rabbitmq_creds_path": RABBITMQ_CREDS_PATH,
        "autopkgtest_package_config_location": AUTOPKGTEST_PACKAGE_CONFIG_LOCATION,
    }
    for unit in units_to_install:
        if unit.endswith(".j2"):
            unit_basename = unit.removesuffix(".j2")
            j2template = j2env.get_template(unit)
            with open(system_units_dir / unit_basename, "w") as f:
                f.write(j2template.render(j2context))
        else:
            shutil.copy(units_path / unit, system_units_dir)

    systemd.daemon_reload()
    if units_to_enable:
        systemd.service_enable("--now", *units_to_enable)


def start():
    pass


def configure(releases, swift_creds, amqp_hostname, amqp_username, amqp_password):
    write_worker_config(releases)
    write_swift_config(swift_creds)
    write_rabbitmq_creds(amqp_hostname, amqp_username, amqp_password)


def add_remote(arch: str, token: str):
    run_as_user(f"lxc remote add remote-{arch} {token}")


def reconcile_worker_units(worker_config: dict[str, int]):
    systemd_helper.reconcile_systemd_worker_units(worker_config)
