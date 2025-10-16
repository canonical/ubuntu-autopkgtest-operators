#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details

import json
import logging
import os
import shutil
import subprocess
import textwrap
import time
from pathlib import Path
from typing import List

import action_types
import charms.operator_libs_linux.v0.apt as apt
import charms.operator_libs_linux.v1.systemd as systemd
import charms.operator_libs_linux.v2.snap as snap
import config_types
import jinja2
import ops
from ops.framework import StoredState

logger = logging.getLogger(__name__)

CHARM_SOURCE_PATH = Path(__file__).parent.parent
CHARM_APP_DATA = CHARM_SOURCE_PATH / "app"
USER = "ubuntu"

AUTOPKGTEST_REPO = "https://salsa.debian.org/ubuntu-ci-team/autopkgtest.git"
AUTOPKGTEST_LOCATION = Path(f"~{USER}/autopkgtest").expanduser()

# Releases not listed here are assumed to support any architecture.
# For ESM supported architectures see https://ubuntu.com/security/esm.
RELEASE_ARCH_RESTRICTIONS = {
    "trusty": ["amd64"],
    "xenial": ["amd64", "s390x"],
    "bionic": ["amd64", "arm64", "ppc64el", "s390x"],
    "focal": ["amd64", "arm64", "ppc64el", "riscv64", "s390x"],
    "jammy": ["amd64", "arm64", "armhf", "i386", "ppc64el", "riscv64", "s390x"],
    "noble": ["amd64", "arm64", "armhf", "i386", "ppc64el", "riscv64", "s390x"],
    "plucky": ["amd64", "arm64", "armhf", "i386", "ppc64el", "riscv64", "s390x"],
}

# List of architecture for which the charm should create VM images.
VM_ARCHITECTURES = []

DEB_DEPENDENCIES = [
    "distro-info-data",
    "python3-distro-info",
    "retry",
]
SNAP_DEPENDENCIES = [
    {"name": "lxd", "channel": "6/stable"},
]


class AutopkgtestJanitorCharm(ops.CharmBase):
    """Autopkgtest janitor charm class."""

    _stored = StoredState()

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)

        self.typed_config = self.load_config(
            config_types.JanitorConfig, errors="blocked"
        )

        self._stored.set_default(
            workers=set(),
            releases=[],
        )

        framework.observe(self.on.install, self._on_install)
        framework.observe(self.on.start, self._on_start)
        framework.observe(self.on.upgrade_charm, self._on_install)

        framework.observe(self.on.add_worker_action, self._on_add_worker)
        framework.observe(self.on.remove_worker_action, self._on_remove_worker)
        framework.observe(self.on.reconfigure_action, self._on_reconfigure)
        framework.observe(
            self.on.rebuild_all_images_action, self._on_rebuild_all_images
        )

        framework.observe(self.on.config_changed, self._on_config_changed)

    def _on_install(self, event: ops.InstallEvent):
        if (
            "JUJU_CHARM_HTTPS_PROXY" in os.environ
            or "JUJU_CHARM_HTTP_PROXY" in os.environ
        ):
            self.unit.status = ops.MaintenanceStatus("setting up proxy settings")
            self.set_up_proxy()

        self.unit.status = ops.MaintenanceStatus(
            "enabling -proposed for distro-info-data"
        )
        self.enable_proposed()
        src_dir = CHARM_APP_DATA / "conf"
        shutil.copy(src_dir / "distro-info-data.pref", "/etc/apt/preferences.d/")

        self.unit.status = ops.MaintenanceStatus("installing system dependencies")
        self.install_dependencies()

        self.unit.status = ops.MaintenanceStatus("installing charm tools")
        src_dir = CHARM_APP_DATA / "bin"
        shutil.copy(src_dir / "cleanup-lxd", "/usr/local/bin/")
        shutil.copy(src_dir / "build-image-on-worker", "/usr/local/bin/")

        self.unit.status = ops.MaintenanceStatus("cloning autopkgtest repository")
        shutil.rmtree(AUTOPKGTEST_LOCATION, ignore_errors=True)
        self.run_as_user(
            f"git clone --depth 1 --branch '{self.typed_config.autopkgtest_git_branch}' '{AUTOPKGTEST_REPO}' '{AUTOPKGTEST_LOCATION}'"
        )

    def _on_start(self, event: ops.StartEvent):
        self.unit.status = ops.ActiveStatus()

    # utils

    def run_as_user(self, command: str, *, capture_output=False, check=True):
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

    def enable_proposed(self) -> None:
        sourceslist = Path("/etc/apt/sources.list.d/ubuntu.sources")
        old_sources = sourceslist.read_text().splitlines()
        new_sources = []
        for line in old_sources:
            parts = line.split()
            if parts and parts[0] == "Suites:" and "-" not in parts[1]:
                if not any([t.endswith("-proposed") for t in parts]):
                    line += f" {parts[1]}-proposed"
            new_sources.append(line)

        if new_sources != old_sources:
            sourceslist.write_text("\n".join(new_sources) + "\n")

    def get_releases(self) -> List[str]:
        """Return all releases to build images for"""

        # we can't do a top-level import because it's the charm itself that
        # installs python3-distro-info.
        import distro_info

        # get all supported releases + extra in reverse order, without duplicates
        udi = distro_info.UbuntuDistroInfo()
        all_releases = (
            udi.supported_esm() + udi.supported() + self.typed_config.extra_releases
        )
        all_releases = [r for r in reversed(udi.all) if r in all_releases]

        return all_releases

    def set_limits(self, arch: str) -> None:
        """Set instance limits"""

        remote = f"worker-{arch}"
        max_containers = self.typed_config.max_containers
        max_vms = self.typed_config.max_virtual_machines

        self.run_as_user(
            f"lxc project set {remote}:default limits.containers {max_containers}"
        )
        self.run_as_user(
            f"lxc project set {remote}:default limits.virtual-machines {max_vms}"
        )

    # basic hooks

    def set_up_proxy(self):
        Path("/etc/environment.d").mkdir(exist_ok=True)
        with open("/etc/environment.d/proxy.conf", "w") as file:
            file.write(
                textwrap.dedent(
                    f"""\
                    http_proxy={os.getenv("JUJU_CHARM_HTTP_PROXY", "")}
                    https_proxy={os.getenv("JUJU_CHARM_HTTPS_PROXY", "")}
                    no_proxy={os.getenv("JUJU_CHARM_NO_PROXY", "")}
                    """
                )
            )

        # changed environment variables don't get picked up by charm
        # execution environment so set them explicitly
        os.environ["http_proxy"] = os.getenv("JUJU_CHARM_HTTP_PROXY", "")
        os.environ["https_proxy"] = os.getenv("JUJU_CHARM_HTTPS_PROXY", "")
        os.environ["no_proxy"] = os.getenv("JUJU_CHARM_NO_PROXY", "")

    def install_dependencies(self):
        apt.update()
        apt.add_package(DEB_DEPENDENCIES)
        for dep in SNAP_DEPENDENCIES:
            snap.add(dep["name"], channel=dep["channel"])

    def install_systemd_units(self):
        units_path = CHARM_APP_DATA / "units"
        units_to_install = [u.name for u in units_path.glob("*")]
        # enable all non-template timers
        units_to_enable = [
            u.name for u in units_path.glob("*.timer") if "@" not in u.name
        ]

        system_units_dir = Path("/etc/systemd/system/")
        j2env = jinja2.Environment(loader=jinja2.FileSystemLoader(units_path))
        j2context = {
            "user": USER,
            "autopkgtest_location": AUTOPKGTEST_LOCATION,
            "mirror": self.typed_config.mirror,
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

    # action helpers

    def disable_image_builders(self, arch, releases):
        """Disable image builders"""

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

    def enable_image_builders(self, arch, releases):
        for release in releases:
            if (
                release in RELEASE_ARCH_RESTRICTIONS
                and arch not in RELEASE_ARCH_RESTRICTIONS[release]
            ):
                logger.info(f"Not creating image for {release}/{arch}")
                continue

            timers = [f"autopkgtest-build-image@{arch}-{release}-container.timer"]
            services = [f"autopkgtest-build-image@{arch}-{release}-container.service"]
            if arch in VM_ARCHITECTURES:
                timers.append(f"autopkgtest-build-image@{arch}-{release}-vm.timer")
                services.append(f"autopkgtest-build-image@{arch}-{release}-vm.service")

            logger.info(f"Enabling worker units for {arch}/{release}")
            systemd.service_enable("--now", *timers)

            logger.info(f"Starting worker units for {arch}/{release}")
            systemd.service_start(*services)

            # don't drown systemd
            time.sleep(3)

    def _on_add_worker(self, event: ops.ActionEvent):
        """Handle adding a new worker"""
        params = event.load_params(action_types.AddWorkerAction, errors="fail")
        arch = params.arch
        token = params.token
        remote = f"worker-{arch}"

        out = self.run_as_user(
            "lxc remote list --format=json",
            capture_output=True,
        ).stdout
        remotes = json.loads(out)

        if remote in remotes:
            event.fail(f"LXD remote already configured for {arch}")
            return

        event.log(f"Adding LXD remote for arch {arch}")
        try:
            self.run_as_user(f"lxc remote add '{remote}' '{token}'")
        except:
            event.fail(f"Failed to add LXD remote for arch {arch}")
            return

        out = self.run_as_user(
            "lxc remote list --format=json",
            capture_output=True,
        ).stdout
        remotes = json.loads(out)
        if remote not in remotes:
            event.fail(f"LXD not reporting remote for {arch} as added")
            return

        self._stored.workers.add(arch)

        event.log("Setting instance limits for new worker")
        self.set_limits(arch)

        event.log("Enabling image build timers for new worker")
        self.enable_image_builders(arch, self._stored.releases)

        event.set_results({"result": f"Added worker for {arch}"})

    def _on_remove_worker(self, event: ops.ActionEvent):
        """Handle removing a worker"""
        params = event.load_params(action_types.RemoveWorkerAction, errors="fail")
        arch = params.arch

        self.disable_image_builders(arch, self._stored.releases)
        self.run_as_user(f"lxc remote remove 'worker-{arch}'", check=False)
        if arch in self._stored.workers:
            self._stored.workers.remove(arch)

    def _on_reconfigure(self, event: ops.ActionEvent):
        """Reconfigure"""

        self.unit.status = ops.MaintenanceStatus("reconfiguring")
        self.on.config_changed.emit()

    def _on_rebuild_all_images(self, event: ops.ActionEvent):
        """Rebuild all images"""

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

    # config helpers

    def _on_config_changed(self, event: ops.ConfigChangedEvent):
        self.unit.status = ops.MaintenanceStatus("updating distro-info-data")
        apt.update()
        apt.add_package("distro-info-data")

        self.unit.status = ops.MaintenanceStatus("installing systemd units")
        self.install_systemd_units()

        self.unit.status = ops.MaintenanceStatus("enabling/disabling builder units")
        releases = self.get_releases()
        logger.info(f"Target releases: {' '.join(releases)}")

        old_releases = [r for r in self._stored.releases if r not in releases]
        if old_releases:
            logger.info(f"Releases to sunset: {' '.join(old_releases)}")
            for arch in self._stored.workers:
                self.disable_image_builders(arch, old_releases)

        new_releases = [r for r in releases if r not in self._stored.releases]
        if new_releases:
            logger.info(f"New releases to activate: {' '.join(new_releases)}")
            for arch in self._stored.workers:
                self.enable_image_builders(arch, new_releases)

        self._stored.releases = releases.copy()

        self.unit.status = ops.MaintenanceStatus("setting instance limits")
        for arch in self._stored.workers:
            self.set_limits(arch)

        self.on.start.emit()


if __name__ == "__main__":  # pragma: nocover
    ops.main(AutopkgtestJanitorCharm)
