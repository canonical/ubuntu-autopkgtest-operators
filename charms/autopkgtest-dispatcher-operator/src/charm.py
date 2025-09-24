#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import glob
import logging
import os
import pathlib
import subprocess
import textwrap

import action_types
import config_types
import ops
from ops.framework import StoredState
from systemd_helper import SystemdHelper

import charms.operator_libs_linux.v0.apt as apt
import charms.operator_libs_linux.v2.snap as snap

logger = logging.getLogger(__name__)

USER = "ubuntu"

AUTOPKGTEST_REPO = "https://salsa.debian.org/ubuntu-ci-team/autopkgtest.git"
AUTOPKGTEST_BRANCH = "ubuntu/production-tip"
AUTOPKGTEST_LOCATION = pathlib.Path(f"~{USER}/autopkgtest").expanduser()

AUTOPKGTEST_CLOUD_REPO = "https://git.launchpad.net/autopkgtest-cloud"
AUTOPKGTEST_CLOUD_BRANCH = "master"
AUTOPKGTEST_CLOUD_LOCATION = pathlib.Path(f"~{USER}/autopkgtest-cloud").expanduser()

AUTOPKGTEST_PACKAGE_CONFIG_REPO = "https://git.launchpad.net/~ubuntu-release/autopkgtest-cloud/+git/autopkgtest-package-configs"
AUTOPKGTEST_PACKAGE_CONFIG_BRANCH = "main"
AUTOPKGTEST_PACKAGE_CONFIG_LOCATION = pathlib.Path(
    f"~{USER}/autopkgtest-package-configs"
).expanduser()

DEB_DEPENDENCIES = [
    "autodep8",
    # some python dependencies of the worker don't provide prebuild binaries
    # and should be installed here
    "python3-amqp",
    "python3-swiftclient",
    "python3-novaclient",
    "python3-influxdb",
    "python3-osc-lib"
    ]
SNAP_DEPENDENCIES = [{"name": "lxd", "channel": "6/stable"}]

RABBITMQ_USERNAME = "dispatcher"
RABBITMQ_VHOST = "/"
RABBITMQ_CREDS_PATH = pathlib.Path(f"~{USER}/rabbitmq.cred").expanduser()

WORKER_CONFIG_PATH = pathlib.Path(f"~{USER}/worker.conf").expanduser()
SWIFT_CONFIG_PATH = pathlib.Path(f"~{USER}/swift-password.cred").expanduser()

# this has to be a glob as part of the path depends on the unit revision number
SYSTEMD_UNIT_FILES_PATH = (
    "/var/lib/juju/agents/unit-autopkgtest-dispatcher-*/charm/units"
)


class AutopkgtestDispatcherCharm(ops.CharmBase):
    """Autopkgtest dispatcher charm class."""

    _stored = StoredState()

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)

        self._stored.set_default(config={}, workers={})

        self.typed_config = self.load_config(
            config_types.DispatcherConfig, errors="blocked"
        )
        self.systemd_helper = SystemdHelper()

        # basic hooks
        framework.observe(self.on.install, self._on_install)
        framework.observe(self.on.start, self._on_start)

        # action hooks
        framework.observe(self.on.add_worker_action, self._on_add_worker)
        framework.observe(self.on.set_unit_count_action, self._on_set_unit_count)
        framework.observe(
            self.on.show_target_config_action, self._on_show_target_config
        )
        framework.observe(
            self.on.create_worker_units_action, self._on_create_worker_units
        )

        # config hook
        framework.observe(self.on.config_changed, self._on_config_changed)

        # relation hooks
        framework.observe(self.on.amqp_relation_joined, self._on_amqp_relation_joined)
        framework.observe(self.on.amqp_relation_changed, self._on_amqp_relation_changed)

    # basic hooks

    def _on_install(self, event: ops.InstallEvent):
        """Install the workload on the machine."""
        self.unit.status = ops.MaintenanceStatus("setting up proxy settings")
        self.set_up_proxy()
        self.unit.status = ops.MaintenanceStatus("installing dependencies")
        self.install_dependencies()
        self.unit.status = ops.MaintenanceStatus("cloning repositories")
        self.clone_repositories()
        self.unit.status = ops.MaintenanceStatus("installing systemd units")
        self.install_systemd_units()
        self.unit.status = ops.MaintenanceStatus("writing worker config")
        self.write_worker_config()
        self.unit.status = ops.ActiveStatus("ready")

    def _on_start(self, event: ops.StartEvent):
        """Handle start event."""
        self.unit.status = ops.MaintenanceStatus("starting workload")
        self.unit.status = ops.ActiveStatus()

    # utils
    def run_as_user(self, command: str):
        # using shell=True to be able to use quotes in the command
        subprocess.run(f"su {USER} -c '{command}'", shell=True, check=True)

    def set_rabbitmq_creds(self, host: str, username: str, password: str):
        """Set rabbitmq creds"""
        self.rabbitmq_host = host
        self.rabbitmq_username = username
        self.rabbitmq_password = password

        self.unit.status = ops.MaintenanceStatus("writing rabbitmq creds")
        with open(RABBITMQ_CREDS_PATH, "w") as file:
            file.write(
                textwrap.dedent(f"""\
                                        RABBIT_HOST="{host}"
                                        RABBIT_USER="{username}"
                                        RABBIT_PASSWORD="{password}"
                                       """)
            )
        self.unit.status = ops.ActiveStatus()

    def set_up_proxy(self):
        with open("/etc/environment", "w") as env_file:
            env_file.write(
                textwrap.dedent(f"""\
                           PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/games:/usr/local/games:/snap/bin"
                           http_proxy={os.environ["JUJU_CHARM_HTTP_PROXY"]}
                           https_proxy={os.environ["JUJU_CHARM_HTTPS_PROXY"]}
                           no_proxy={os.environ["JUJU_CHARM_NO_PROXY"]}
                           """)
            )

        # changed environment variables don't get picked up by this file
        # so set them explicitly
        os.environ["http_proxy"] = os.environ["JUJU_CHARM_HTTP_PROXY"]
        os.environ["https_proxy"] = os.environ["JUJU_CHARM_HTTPS_PROXY"]
        os.environ["no_proxy"] = os.environ["JUJU_CHARM_NO_PROXY"]

    # basic hooks

    def install_dependencies(self) -> None:
        apt.update()
        apt.add_package(DEB_DEPENDENCIES)
        for needed_snap in SNAP_DEPENDENCIES:
            snap.add(needed_snap["name"], channel=needed_snap["channel"])

    def clone_repositories(self) -> None:
        for repo, branch, location in [
            (AUTOPKGTEST_REPO, AUTOPKGTEST_BRANCH, AUTOPKGTEST_LOCATION),
            (
                AUTOPKGTEST_CLOUD_REPO,
                AUTOPKGTEST_CLOUD_BRANCH,
                AUTOPKGTEST_CLOUD_LOCATION,
            ),
            (
                AUTOPKGTEST_PACKAGE_CONFIG_REPO,
                AUTOPKGTEST_PACKAGE_CONFIG_BRANCH,
                AUTOPKGTEST_PACKAGE_CONFIG_LOCATION,
            ),
        ]:
            # TODO: the currently packaged version of pygit2 does not support cloning through
            # a proxy. the next release should hopefully include this feature.
            # pygit2.clone_repository(repo, location, checkout_branch=branch)
            self.run_as_user(f"git clone -b {branch} {repo} {location}")

    def install_systemd_units(self):
        units_path = glob.glob(SYSTEMD_UNIT_FILES_PATH)
        assert len(units_path) == 1, "there should be one units directory"
        units_path = units_path[0]

        dest_dir = pathlib.Path("/etc/systemd/system/")

        to_enable = []
        for unit in pathlib.Path(units_path).iterdir():
            dest = dest_dir.joinpath(unit.name)
            try:
                os.symlink(unit, dest)
            except FileExistsError:
                if not os.path.islink(dest):
                    os.unlink(dest)
                    os.symlink(unit, dest)
            if "@" not in unit.name:
                to_enable.append(unit.name)

        self.systemd_helper.enable_units(to_enable)

    def write_worker_config(self):
        with open(WORKER_CONFIG_PATH, "w") as file:
            file.write(
                textwrap.dedent(f"""\
                                [autopkgtest]
                                checkout_dir = ../../../../../../autopkgtest
                                per_package_config_dir = ../../../../../../autopkgtest-package-configs
                                releases = {self.typed_config.releases}
                                setup_command =
                                setup_command2 =
                                worker_upstream_percentage = {self.typed_config.worker_upstream_percentage}
                                stable_release_percentage = {self.typed_config.stable_release_percentage}
                                retry_delay = 300
                                debug = 0
                                architectures =

                                [virt]
                                args = lxd -r $LXD_REMOTE $LXD_REMOTE:autopkgtest/ubuntu/$RELEASE/$ARCHITECTURE
                                """)
            )

    def write_swift_config(self):
        with open(SWIFT_CONFIG_PATH, "w") as file:
            for key in self.config:
                if key.startswith("swift") and self.config[key] is not None:
                    file.write(
                        f'{key.upper().replace("-", "_")}={str(self.config[key]).strip()}\n'
                    )

    # action hooks

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
        self._stored.workers[worker_arch] = self.typed_config.default_worker_count
        event.set_results({"result": f"Added worker for {worker_arch}"})

    def add_worker(self, arch: str, token: str):
        self.run_as_user(f"lxc remote add worker-{arch} {token}")

    def _on_set_unit_count(self, event: ops.ActionEvent):
        params = event.load_params(action_types.SetUnitCountAction, errors="fail")
        worker_arch = params.arch.value

        event.log(f"Setting unit count for arch {worker_arch}")
        self._stored.workers[worker_arch] = params.count
        event.set_results(
            {"results": f"Set unit count for {worker_arch} to {params.count}"}
        )

    def _on_show_target_config(self, event: ops.ActionEvent):
        event.set_results({"results": f"{self._stored.workers}"})

    def _on_create_worker_units(self, event: ops.ActionEvent):
        self.systemd_helper.set_up_systemd_units(self._stored.workers)

    # config hook

    def _on_config_changed(self, event: ops.ConfigChangedEvent):
        changes = self.config_changes()
        if not changes:
            logger.debug("No configuration changes detected")
            return
        
        if any(
            [
                change.startswith("swift-") for change in changes
            ]
        ):
            self.write_swift_config()

        if any(
            [
                "releases" in changes,
                "worker_upstream_percentage" in changes,
                "stable_release_percentage" in changes,
            ]
        ):
            self.write_worker_config()

    def config_changes(self):
        new_config = self.config
        old_config = self._stored.config
        to_apply = {}
        for k, v in new_config.items():
            if k not in old_config or v != old_config[k]:
                to_apply[k] = v

        return to_apply

    # relation hooks

    def _on_amqp_relation_joined(self, event: ops.RelationJoinedEvent):
        self.unit.status = ops.MaintenanceStatus(
            f"Setting up {event.relation.name} connection"
        )

        event.relation.data[self.unit].update(
            {"username": RABBITMQ_USERNAME, "vhost": RABBITMQ_VHOST}
        )

    def _on_amqp_relation_changed(self, event):
        unit_data = event.relation.data[event.unit]

        if "password" not in unit_data:
            logger.info("rabbitmq-server has not sent password yet")
            return

        hostname = unit_data["hostname"]
        password = unit_data["password"]

        self.set_rabbitmq_creds(hostname, RABBITMQ_USERNAME, password)
        self.unit.status = ops.ActiveStatus()


if __name__ == "__main__":  # pragma: nocover
    ops.main(AutopkgtestDispatcherCharm)
