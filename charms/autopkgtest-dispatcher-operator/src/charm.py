#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import logging
import os
import shutil
import subprocess
import textwrap
import time
from pathlib import Path

import action_types
import charms.operator_libs_linux.v0.apt as apt
import charms.operator_libs_linux.v1.systemd as systemd
import charms.operator_libs_linux.v2.snap as snap
import config_types
import jinja2
import ops
from ops.framework import StoredState
from systemd_helper import SystemdHelper

logger = logging.getLogger(__name__)

USER = "ubuntu"

AUTOPKGTEST_REPO = "https://salsa.debian.org/ubuntu-ci-team/autopkgtest.git"
AUTOPKGTEST_BRANCH = "ubuntu/production-tip"
AUTOPKGTEST_LOCATION = Path(f"~{USER}/autopkgtest").expanduser()

AUTOPKGTEST_PACKAGE_CONFIG_REPO = "https://git.launchpad.net/~ubuntu-release/autopkgtest-cloud/+git/autopkgtest-package-configs"
AUTOPKGTEST_PACKAGE_CONFIG_BRANCH = "main"
AUTOPKGTEST_PACKAGE_CONFIG_LOCATION = Path(
    f"~{USER}/autopkgtest-package-configs"
).expanduser()

DEB_DEPENDENCIES = [
    "autodep8",
    "python3-pika",
    "python3-swiftclient",
]
SNAP_DEPENDENCIES = [{"name": "lxd", "channel": "6/stable"}]

CONF_DIRECTORY = Path("/etc/autopkgtest-dispatcher")

RABBITMQ_USERNAME = "dispatcher"
RABBITMQ_CREDS_PATH = CONF_DIRECTORY / "rabbitmq.cred"

WORKER_CONFIG_PATH = CONF_DIRECTORY / "worker.conf"
SWIFT_CONFIG_PATH = CONF_DIRECTORY / "swift.cred"

# charm files path
CHARM_SOURCE_PATH = Path(__file__).parent.parent
CHARM_APP_DATA = CHARM_SOURCE_PATH / "app"


class AutopkgtestDispatcherCharm(ops.CharmBase):
    """Autopkgtest dispatcher charm class."""

    _stored = StoredState()

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)

        self._stored.set_default(
            installed=False,
            got_amqp_creds=False,
            amqp_hostname=None,
            amqp_password=None,
            workers={},
        )

        self.typed_config = self.load_config(
            config_types.DispatcherConfig, errors="blocked"
        )
        self.systemd_helper = SystemdHelper()

        # basic hooks
        framework.observe(self.on.install, self._on_install)
        framework.observe(self.on.config_changed, self._on_config_changed)
        framework.observe(self.on.start, self._on_start)
        framework.observe(self.on.upgrade_charm, self._on_install)

        # action hooks
        framework.observe(self.on.add_worker_action, self._on_add_worker)
        framework.observe(self.on.set_unit_count_action, self._on_set_unit_count)
        framework.observe(
            self.on.show_target_config_action, self._on_show_target_config
        )
        framework.observe(
            self.on.create_worker_units_action, self._on_create_worker_units
        )

        # relation hooks
        framework.observe(self.on.amqp_relation_joined, self._on_amqp_relation_joined)
        framework.observe(self.on.amqp_relation_changed, self._on_amqp_relation_changed)
        framework.observe(self.on.amqp_relation_broken, self._on_amqp_relation_broken)

    def _on_install(self, event: ops.InstallEvent):
        """Install the workload on the machine."""
        self.unit.status = ops.MaintenanceStatus("creating directories")
        CONF_DIRECTORY.mkdir(exist_ok=True)
        if (
            "JUJU_CHARM_HTTPS_PROXY" in os.environ
            or "JUJU_CHARM_HTTP_PROXY" in os.environ
        ):
            self.unit.status = ops.MaintenanceStatus("setting up proxy settings")
            self.set_up_proxy()
        self.unit.status = ops.MaintenanceStatus("installing dependencies")
        self.install_dependencies()
        self.unit.status = ops.MaintenanceStatus("cloning repositories")
        self.clone_repositories()
        self.unit.status = ops.MaintenanceStatus("installing worker and tools")
        self.install_worker_and_tools()
        self.unit.status = ops.MaintenanceStatus("writing worker config")
        self.write_worker_config()
        self.unit.status = ops.MaintenanceStatus("installing systemd units")
        self.install_systemd_units()

        self._stored.installed = True

    def _on_start(self, event: ops.StartEvent):
        """Handle start event."""
        if isinstance(self.unit.status, ops.BlockedStatus):
            return

        self.unit.status = ops.ActiveStatus()

    # utils
    def run_as_user(self, command: str):
        subprocess.check_call(
            [
                "su",
                "--login",
                "--whitelist-environment=https_proxy,http_proxy,no_proxy",
                USER,
                "--command",
                command,
            ],
        )

    def write_rabbitmq_creds(self):
        """Set rabbitmq creds"""
        with open(RABBITMQ_CREDS_PATH, "w") as file:
            file.write(
                textwrap.dedent(
                    f"""\
                    RABBIT_HOST="{self._stored.amqp_hostname}"
                    RABBIT_USER="{RABBITMQ_USERNAME}"
                    RABBIT_PASSWORD="{self._stored.amqp_password}"
                    """
                )
            )

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

        # changed environment variables don't get picked up by this file
        # so set them explicitly
        os.environ["http_proxy"] = os.getenv("JUJU_CHARM_HTTP_PROXY", "")
        os.environ["https_proxy"] = os.getenv("JUJU_CHARM_HTTPS_PROXY", "")
        os.environ["no_proxy"] = os.getenv("JUJU_CHARM_NO_PROXY", "")

    # basic hooks

    def install_dependencies(self) -> None:
        apt.update()
        apt.add_package(DEB_DEPENDENCIES)
        for needed_snap in SNAP_DEPENDENCIES:
            snap.add(needed_snap["name"], channel=needed_snap["channel"])

    def clone_repositories(self) -> None:
        for repo, branch, location in [
            (
                AUTOPKGTEST_REPO,
                AUTOPKGTEST_BRANCH,
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
            self.run_as_user(f"git clone -b '{branch}' '{repo}' '{location}'")

    def install_worker_and_tools(self):
        src_path = CHARM_APP_DATA / "bin"
        dest_dir = Path("/usr/local/bin/")
        shutil.copy(src_path / "worker", dest_dir)
        shutil.copy(src_path / "filter-amqp-dupes-upstream", dest_dir)

    def install_systemd_units(self):
        units_path = CHARM_APP_DATA / "units"
        units_to_install = [u.name for u in (units_path).glob("*")]
        units_to_enable = [u.name for u in (units_path).glob("*.timer")]

        system_units_dir = Path("/etc/systemd/system/")
        j2env = jinja2.Environment(loader=jinja2.FileSystemLoader(units_path))
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

    def write_worker_config(self):
        with open(WORKER_CONFIG_PATH, "w") as file:
            file.write(
                textwrap.dedent(
                    f"""\
                    [autopkgtest]
                    checkout_dir = {AUTOPKGTEST_LOCATION}
                    per_package_config_dir = {AUTOPKGTEST_PACKAGE_CONFIG_LOCATION}
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
                    """
                )
            )

    def write_swift_config(self):
        with open(SWIFT_CONFIG_PATH, "w") as file:
            for k, v in self.swift_creds.items():
                file.write(f"{k.upper().replace('-', '_')}={v}\n")

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
        if not self._stored.installed:
            self.on.install.emit()

        self.unit.status = ops.MaintenanceStatus("configure: gathering data")

        if not self._stored.got_amqp_creds:
            self.unit.status = ops.BlockedStatus("waiting for AMQP relation")
            return

        # https://github.com/juju/terraform-provider-juju/issues/770#issuecomment-3051899587
        while True:
            try:
                swift_password = self.typed_config.swift_juju_secret.get_content().get(
                    "password"
                )
                break
            except ops.model.ModelError:
                self.unit.status = ops.BlockedStatus("swift secret not yet available")
                time.sleep(10)

        self.unit.status = ops.MaintenanceStatus("configuring service")

        self.swift_creds = {
            k: v
            for k, v in self.typed_config.model_dump().items()
            if k.startswith("swift_") and isinstance(v, str)
        }
        self.swift_creds["swift_password"] = swift_password

        self.write_worker_config()
        self.write_swift_config()
        self.write_rabbitmq_creds()

        self.on.start.emit()

    # relation hooks

    def _on_amqp_relation_joined(self, event: ops.RelationJoinedEvent):
        self.unit.status = ops.MaintenanceStatus(
            f"Setting up {event.relation.name} connection"
        )

        event.relation.data[self.unit].update(
            {"username": RABBITMQ_USERNAME, "vhost": "/"}
        )

    def _on_amqp_relation_changed(self, event: ops.RelationChangedEvent):
        unit_data = event.relation.data[event.unit]

        if "password" not in unit_data:
            logger.info("rabbitmq-server has not sent password yet")
            return

        self.unit.status = ops.MaintenanceStatus(
            f"Updating up {event.relation.name} connection"
        )

        hostname = unit_data["hostname"]
        password = unit_data["password"]

        self._stored.got_amqp_creds = True
        self._stored.amqp_hostname = hostname
        self._stored.amqp_password = password

        self.on.config_changed.emit()

    def _on_amqp_relation_broken(self, event: ops.RelationBrokenEvent):
        self._stored.got_amqp_creds = False
        self._stored.amqp_hostname = None
        self._stored.amqp_password = None
        os.remove(RABBITMQ_CREDS_PATH)

        self.on.config_changed.emit()

    def _on_secret_changed(self, event: ops.SecretChangedEvent):
        self.on.config_changed.emit()


if __name__ == "__main__":  # pragma: nocover
    ops.main(AutopkgtestDispatcherCharm)
