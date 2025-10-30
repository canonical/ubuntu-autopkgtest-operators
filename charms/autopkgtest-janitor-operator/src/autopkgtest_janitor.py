# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Functions for managing and interacting with the workload.

The intention is that this module could be used outside the context of a charm.
"""

import json
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from textwrap import dedent

import charms.operator_libs_linux.v1.systemd as systemd
import jinja2
from charmlibs import apt, snap

logger = logging.getLogger(__name__)

CHARM_SOURCE_PATH = Path(__file__).parent.parent
CHARM_APP_DATA = CHARM_SOURCE_PATH / "app"
USER = "ubuntu"
CHARM_TOOLS_DEST = Path("/usr/local/bin")

AUTOPKGTEST_REPO = "https://salsa.debian.org/ubuntu-ci-team/autopkgtest.git"
AUTOPKGTEST_LOCATION = Path(f"~{USER}/autopkgtest").expanduser()

# Releases not listed here are assumed to support any architecture.
# For ESM supported architectures see https://ubuntu.com/security/esm.
RELEASE_ARCH_RESTRICTIONS = {
    "trusty": ["amd64"],
    "xenial": ["amd64", "s390x"],
    "bionic": ["amd64", "arm64", "ppc64el", "s390x"],
    "focal": ["amd64", "arm64", "ppc64el", "s390x"],
    "jammy": ["amd64", "arm64", "armhf", "ppc64el", "riscv64", "s390x"],
    "noble": ["amd64", "arm64", "armhf", "ppc64el", "riscv64", "s390x"],
    "plucky": ["amd64", "arm64", "armhf", "ppc64el", "riscv64", "s390x"],
}

# List of architecture for which the charm should create VM images.
VM_ARCHITECTURES = ["amd64"]

DEB_DEPENDENCIES = [
    "distro-info",
    "retry",
]
SNAP_DEPENDENCIES = [
    {"name": "lxd", "channel": "6/stable"},
]

# utils


def run_as_user(command: str, *, capture_output=False, check=True):
    return subprocess.run(
        [
            "su",
            "--login",
            "--whitelist-environment=https_proxy,http_proxy,no_proxy",
            USER,
            "--command",
            command,
        ],
        capture_output=capture_output,
        check=check,
        text=True,
    )


def set_limits(arch: str, max_containers, max_vms) -> None:
    """Set instance limits."""
    remote = f"remote-{arch}"

    run_as_user(f"lxc project set {remote}:default limits.containers {max_containers}")
    run_as_user(f"lxc project set {remote}:default limits.virtual-machines {max_vms}")


def disable_image_builders(arch, releases):
    """Disable image builders."""
    # We don't try to be smart here hoping to have a good representation
    # of the state of the units. We just query for existing matching units
    # and stop/disable all of them.

    for release in releases:
        # stop all matching units
        systemd.service_stop(f"autopkgtest-build-image@{arch}-{release}-*.*")

        # disable all enabled matching units

        # note: list-units-files returns 1 is no units are matched,
        # hence the check=False.
        out = subprocess.run(
            [
                "systemctl",
                "list-unit-files",
                "--no-legend",
                "--no-pager",
                "--state=enabled",
                f"autopkgtest-build-image@{arch}-{release}-*.*",
            ],
            text=True,
            capture_output=True,
            check=False,
        ).stdout
        if out:
            services = [line.split()[0] for line in out.splitlines()]
            systemd.service_disable(*services)

        # reset failed state
        systemd._systemctl(
            "reset-failed", f"autopkgtest-build-image@{arch}-{release}-*.*"
        )


def enable_image_builders(arch, releases):
    for i, release in enumerate(releases):
        if (
            release in RELEASE_ARCH_RESTRICTIONS
            and arch not in RELEASE_ARCH_RESTRICTIONS[release]
        ):
            logger.info(f"Not creating image for {release}/{arch}")
            continue

        # don't drown systemd
        if i > 0:
            time.sleep(3)

        timers = [f"autopkgtest-build-image@{arch}-{release}-container.timer"]
        services = [f"autopkgtest-build-image@{arch}-{release}-container.service"]
        if arch in VM_ARCHITECTURES:
            timers.append(f"autopkgtest-build-image@{arch}-{release}-vm.timer")
            services.append(f"autopkgtest-build-image@{arch}-{release}-vm.service")

        logger.info(f"Enabling periodic image builds for {arch}/{release}")
        systemd.service_enable("--now", *timers)

        logger.info(f"Starting image builds for {arch}/{release}")
        systemd.service_start(*services)


def install(autopkgtest_branch):
    """Install janitor."""
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

    logger.info("updating package index")
    apt.update()

    logger.info("installing packages")
    apt.add_package(DEB_DEPENDENCIES)

    logger.info("installing snaps")
    for dep in SNAP_DEPENDENCIES:
        snap.add(dep["name"], channel=dep["channel"])

    logger.info("installing charm tools")
    src_dir = CHARM_APP_DATA / "bin"
    shutil.copy(src_dir / "cleanup-lxd", CHARM_TOOLS_DEST)
    shutil.copy(src_dir / "build-image-on-remote", CHARM_TOOLS_DEST)

    logger.info("cloning autopkgtest repository")
    shutil.rmtree(AUTOPKGTEST_LOCATION, ignore_errors=True)
    run_as_user(
        f"git clone --depth 1 --branch '{autopkgtest_branch}' '{AUTOPKGTEST_REPO}' '{AUTOPKGTEST_LOCATION}'"
    )


def start():
    pass


def configure(
    arches,
    autopkgtest_branch,
    mirror,
    stored_releases,
    target_releases,
    max_containers,
    max_vms,
):
    logger.info("updating distro-info-data")
    apt.update()
    # Note apt.add_package() does not upgrade an already installed package.
    subprocess.run(
        [
            "apt-get",
            "-o=APT::Get::Always-Include-Phased-Updates=true",
            "install",
            "distro-info-data",
        ],
        check=True,
    )

    logger.info("updating autopkgtest")
    run_as_user(
        f"git -C '{AUTOPKGTEST_LOCATION}' fetch --depth 1 origin '{autopkgtest_branch}'"
    )
    run_as_user(f"git -C {AUTOPKGTEST_LOCATION} checkout {autopkgtest_branch}")

    logger.info("installing systemd units")
    units_path = CHARM_APP_DATA / "units"
    units_to_install = [u.name for u in units_path.glob("*")]
    # enable all non-template timers
    units_to_enable = [u.name for u in units_path.glob("*.timer") if "@" not in u.name]

    system_units_dir = Path("/etc/systemd/system/")
    j2env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(units_path),
        autoescape=jinja2.select_autoescape(),
    )
    j2context = {
        "user": USER,
        "autopkgtest_location": AUTOPKGTEST_LOCATION,
        "mirror": mirror,
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

    logger.info("enabling/disabling builder units")
    logger.info(f"target releases: {' '.join(target_releases)}")

    old_releases = [r for r in stored_releases if r not in target_releases]
    if old_releases:
        logger.info(f"releases to sunset: {' '.join(old_releases)}")
        for arch in arches:
            disable_image_builders(arch, old_releases)

    new_releases = [r for r in target_releases if r not in stored_releases]
    if new_releases:
        logger.info(f"new releases to activate {' '.join(new_releases)}")
        for arch in arches:
            enable_image_builders(arch, new_releases)

    logger.info("setting instance limits")
    for arch in arches:
        set_limits(arch, max_containers, max_vms)


def get_remotes():
    return json.loads(
        run_as_user("lxc remote list --format=json", capture_output=True).stdout
    )


def add_remote(arch: str, token: str, all_releases: list[str], max_containers, max_vms):
    """Handle adding a new remote."""
    remote = f"remote-{arch}"

    if remote in get_remotes():
        raise Exception(f"LXD remote already configured for {arch}")

    run_as_user(f"lxc remote add '{remote}' '{token}'")

    if remote not in get_remotes():
        raise Exception(f"LXD not reporting remote for {arch} as expected")

    set_limits(arch, max_containers, max_vms)

    enable_image_builders(arch, all_releases)


def remove_remote(arch: str, all_releases):
    """Remove an existing remote."""
    disable_image_builders(arch, all_releases)
    run_as_user(f"lxc remote remove 'remote-{arch}'", check=False)


def rebuild_all_images():
    out = subprocess.run(
        [
            "systemctl",
            "list-units",
            "--plain",
            "--no-legend",
            "--no-pager",
            "--type=timer",
            "autopkgtest-build-image@*.timer",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    all_timers = [line.split()[0] for line in out.splitlines()]
    all_services = [t.removesuffix(".timer") + ".service" for t in all_timers]
    for service in all_services:
        systemd.service_start(service)
        time.sleep(2)
