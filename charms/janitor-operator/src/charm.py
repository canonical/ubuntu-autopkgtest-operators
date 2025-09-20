#!/usr/bin/env python3
# Copyright 2025 uralt
# See LICENSE file for licensing details

import glob
import os
import ops
import pathlib
import logging
import subprocess
import textwrap

import charms.operator_libs_linux.v0.apt as apt
import charms.operator_libs_linux.v1.systemd as systemd
import charms.operator_libs_linux.v2.snap as snap

from ops.framework import StoredState

import action_types
import config_types

logger = logging.getLogger(__name__)


USER = "ubuntu"

AUTOPKGTEST_REPO = "https://salsa.debian.org/ubuntu-ci-team/autopkgtest.git"
AUTOPKGTEST_BRANCH = "ubuntu/production-tip"
AUTOPKGTEST_LOCATION = pathlib.Path(f"~{USER}/autopkgtest").expanduser()

SNAP_DEPENDENCIES = [
    {
        "name": "lxd",
        "channel": "6/stable"
    }
]

ARCH_RELEASE_ALLOW_MAPPING = {
    "trusty": ["amd64", "i386"],
    "xenial": ["amd64", "i386"]
}

ARCH_RELEASE_DISALLOW_MAPPING = {
    "bionic": ["riscv64"],
    "focal": ["riscv64"]
}

# this has to be a glob as part of the path depends on the unit revision number
SYSTEMD_UNIT_FILES_PATH = "/var/lib/juju/agents/unit-autopkgtest-janitor-*/charm/units"

class AutopkgtestJanitorCharm(ops.CharmBase):
    """Autopkgtest janitor charm class."""

    _stored = StoredState()

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)
        self._stored.set_default(
            workers=set()
        )

        self.typed_config = self.load_config(config_types.JanitorConfig, errors='blocked')
        self.releases = self.typed_config.releases

        framework.observe(self.on.install, self._on_install)
        framework.observe(self.on.start, self._on_start)

        framework.observe(self.on.add_worker_action, self._on_add_worker)
        framework.observe(self.on.remove_worker_action, self._on_remove_worker)

        framework.observe(self.on.config_changed, self._on_config_changed)

    def _on_install(self, event: ops.InstallEvent):
        self.unit.status = ops.MaintenanceStatus("setting up proxy settings")
        self.set_up_proxy()
        self.unit.status = ops.MaintenanceStatus("installing system dependencies")
        self.install_dependencies()
        self.unit.status = ops.MaintenanceStatus("cloning autopkgtest repository")
        subprocess.run(f"su ubuntu -c 'git clone -b {AUTOPKGTEST_BRANCH} {AUTOPKGTEST_REPO} {AUTOPKGTEST_LOCATION}'", shell=True, check=True)
        self.unit.status = ops.MaintenanceStatus("installing systemd units")
        self.install_systemd_units()
        self.unit.status = ops.ActiveStatus()

    def _on_start(self, event: ops.StartEvent):
        self.unit.status = ops.ActiveStatus()

    # basic hooks

    def set_up_proxy(self):
        with open("/etc/environment", "w") as env_file:
            env_file.write(textwrap.dedent(f"""
                           PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/games:/usr/local/games:/snap/bin"
                           http_proxy={os.getenv("JUJU_CHARM_HTTP_PROXY", "")}
                           https_proxy={os.getenv("JUJU_CHARM_HTTPS_PROXY", "")}
                           no_proxy={os.getenv("JUJU_CHARM_NO_PROXY", "")}
                           """))

        # changed environment variables don't get picked up by this file
        # so set them explicitly
        os.environ["http_proxy"] = os.getenv("JUJU_CHARM_HTTP_PROXY", "")
        os.environ["https_proxy"] = os.getenv("JUJU_CHARM_HTTPS_PROXY", "")
        os.environ["no_proxy"] = os.getenv("JUJU_CHARM_NO_PROXY", "")

    def install_dependencies(self):
        apt.update()
        for dep in SNAP_DEPENDENCIES:
            snap.add(dep["name"], channel=dep["channel"])

    def install_systemd_units(self):
        # TODO: this doesn't do anything for now, but will install
        # other maintenance tasks in the future
        units_path = glob.glob(SYSTEMD_UNIT_FILES_PATH)
        assert len(units_path) == 1, "there should be one units directory"
        units_path = units_path[0]

        dest_dir = pathlib.Path("/etc/systemd/system/")

        for unit in pathlib.Path(units_path).iterdir():
            dest = dest_dir.joinpath(unit.name)
            try:
                os.symlink(unit, dest)
            except FileExistsError:
                if not os.path.islink(dest):
                    os.unlink(dest)
                    os.symlink(unit, dest)
            if "@" not in unit.name:
                systemd.service_enable(unit.name)
        
        systemd.daemon_reload()

    # action helpers

    def is_release_allowed(self, arch, release):
        if release in ARCH_RELEASE_ALLOW_MAPPING:
            return arch in ARCH_RELEASE_ALLOW_MAPPING[release]
        if release in ARCH_RELEASE_DISALLOW_MAPPING:
            return arch not in ARCH_RELEASE_DISALLOW_MAPPING[release]
        
        return True
    
    def disable_image_builders(self, arch, releases):
        for release in releases:
            systemd.service_stop(f"build-adt-container@worker-{arch}-{release}.timer")
            systemd.service_stop(f"build-adt-vm@worker-{arch}-{release}.timer")
            try:
                systemd.service_disable(f"build-adt-container@worker-{arch}-{release}.timer")
                systemd.service_disable(f"build-adt-vm@worker-{arch}-{release}.timer")
            except systemd.SystemdError:
                # fine if this fails, probably disabling a release we weren't building on this arch
                pass

    def enable_image_builders(self, arch, releases):
        for release in releases:
            if self.is_release_allowed(arch, release):
                systemd.service_enable(f"build-adt-container@worker-{arch}-{release}.timer")
                systemd.service_enable(f"build-adt-vm@worker-{arch}-{release}.timer")

                # trigger image build immediately upon starting the timer
                try:
                    subprocess.run(["systemctl", "start", f"build-adt-container@worker-{arch}-{release}.service", "--no-block"])
                    subprocess.run(["systemctl", "start", f"build-adt-vm@worker-{arch}-{release}.service", "--no-block"])
                except systemd.SystemdError:
                    # enable the rest of the timers even if something fails
                    pass

    def _on_add_worker(self, event: ops.ActionEvent):
        """Handle adding a new worker"""
        params = event.load_params(action_types.AddWorkerAction, errors="fail")
        worker_arch = params.arch.value

        event.log(f"Adding worker for arch {worker_arch}")
        try:
            self.add_worker(worker_arch, params.token)
        except:
            event.fail(f"Failed to add worker for arch {worker_arch}")
            return
        
        event.log("Enabling image build for new worker")
        self._stored.workers.add(worker_arch)
        self.enable_image_builders(worker_arch, self.typed_config.releases.split(' '))
        systemd.daemon_reload()

        event.set_results({"result": f"Added worker for {worker_arch}"})

    def add_worker(self, arch: str, token: str):
        subprocess.run(f"su ubuntu -c 'lxc remote add worker-{arch} {token}'", shell=True, check=True)

    def _on_remove_worker(self, event: ops.ActionEvent):
        """Handle removing a worker"""
        params = event.load_params(action_types.RemoveWorkerAction, errors="fail")
        worker_arch = params.arch.value

        subprocess.run(f"su ubuntu -c 'lxc remote remove worker-{worker_arch}'", shell=True)

        # disable all image builders even if removing the worker failed
        self.disable_image_builders(worker_arch, self.typed_config.releases)
        systemd.daemon_reload()

    # config helpers

    def _on_config_changed(self, event: ops.ConfigChangedEvent):
        if self.releases != self.typed_config.releases:
            current_config = self.releases.split(' ')
            new_config = self.typed_config.releases

            new_releases = [release for release in new_config if release not in current_config]
            old_releases = [release for release in current_config if release not in new_config]

            for arch in self._stored.workers:
                self.enable_image_builders(arch, new_releases)
                self.disable_image_builders(arch, old_releases)
            systemd.daemon_reload()

            self.releases = self.typed_config.releases

if __name__ == "__main__": # pragma: nocover
    ops.main(AutopkgtestJanitorCharm)
