#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import action_types
import autopkgtest_dispatcher
import config_types
import ops
from ops.framework import StoredState

RABBITMQ_USERNAME = "dispatcher"


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

        # basic hooks
        framework.observe(self.on.install, self._on_install)
        framework.observe(self.on.config_changed, self._on_config_changed)
        framework.observe(self.on.start, self._on_start)
        framework.observe(self.on.upgrade_charm, self._on_install)

        # action hooks
        framework.observe(self.on.add_remote_action, self._on_add_remote)
        framework.observe(self.on.remove_remote_action, self._on_remove_remote)
        framework.observe(self.on.set_worker_count_action, self._on_set_worker_count)
        framework.observe(
            self.on.show_target_config_action, self._on_show_target_config
        )
        framework.observe(
            self.on.reconcile_worker_units_action, self._on_reconcile_worker_units
        )
        # relation hooks
        framework.observe(self.on.amqp_relation_joined, self._on_amqp_relation_joined)
        framework.observe(self.on.amqp_relation_changed, self._on_amqp_relation_changed)
        framework.observe(self.on.amqp_relation_broken, self._on_amqp_relation_broken)

    def _on_install(self, event: ops.InstallEvent):
        """Install the workload on the machine."""
        self.unit.status = ops.MaintenanceStatus("installing workload")
        autopkgtest_dispatcher.install(
            self.typed_config.autopkgtest_git_branch, self.typed_config.releases
        )

        self._stored.installed = True

    def _on_start(self, event: ops.StartEvent):
        """Handle start event."""
        if isinstance(self.unit.status, ops.BlockedStatus):
            return

        autopkgtest_dispatcher.start()
        self.unit.status = ops.ActiveStatus()

    # action hooks

    def _on_add_remote(self, event: ops.ActionEvent):
        """Handle adding a new remote."""
        params = event.load_params(action_types.AddRemoteAction, errors="fail")
        remote_arch = params.arch.value

        event.log(f"Adding remote for arch {remote_arch}")
        try:
            autopkgtest_dispatcher.add_remote(remote_arch, params.token)
        except:
            event.fail(f"Failed to add remote for arch {remote_arch}")
            return
        event.log(
            f"New remote defaults to {self.typed_config.default_worker_count} workers"
        )
        self._stored.workers[remote_arch] = self.typed_config.default_worker_count
        event.set_results({"result": f"Added remote for {remote_arch}"})

    def _on_remove_remote(self, event: ops.ActionEvent):
        """Handle removing a remote."""
        params = event.load_params(action_types.RemoveRemoteAction, errors="fail")
        remote_arch = params.arch.value
        autopkgtest_dispatcher.remove_remote(remote_arch)
        self._stored.workers[remote_arch] = 0
        autopkgtest_dispatcher.reconcile_worker_units(self._stored.workers)

    def _on_set_worker_count(self, event: ops.ActionEvent):
        params = event.load_params(action_types.SetWorkerCountAction, errors="fail")
        worker_arch = params.arch.value

        event.log(f"Setting worker count for arch {worker_arch}")
        self._stored.workers[worker_arch] = params.count
        event.set_results(
            {"results": f"Set unit count for {worker_arch} to {params.count}"}
        )

    def _on_show_target_config(self, event: ops.ActionEvent):
        event.set_results({"results": f"{self._stored.workers}"})

    def _on_reconcile_worker_units(self, event: ops.ActionEvent):
        autopkgtest_dispatcher.reconcile_worker_units(self._stored.workers)

    # config hook

    def _on_config_changed(self, event: ops.ConfigChangedEvent):
        if not self._stored.installed:
            self.on.install.emit()

        if not self._stored.got_amqp_creds:
            self.unit.status = ops.BlockedStatus("waiting for AMQP relation")
            return

        if self.typed_config.swift_juju_secret:
            try:
                swift_password = self.typed_config.swift_juju_secret.get_content().get(
                    "password"
                )
            except ops.model.ModelError:
                self.unit.status = ops.BlockedStatus("swift secret not yet available")
                return
        else:
            swift_password = ""

        self.unit.status = ops.MaintenanceStatus("configuring service")

        self.swift_creds = {
            k: v
            for k, v in self.typed_config.model_dump().items()
            if k.startswith("swift_") and isinstance(v, str)
        }
        self.swift_creds["swift_password"] = swift_password

        autopkgtest_dispatcher.configure(
            releases=self.typed_config.releases,
            swift_creds=self.swift_creds,
            amqp_hostname=self._stored.amqp_hostname,
            amqp_username=RABBITMQ_USERNAME,
            amqp_password=self._stored.amqp_password,
        )

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

        self.on.config_changed.emit()

    def _on_secret_changed(self, event: ops.SecretChangedEvent):
        self.on.config_changed.emit()


if __name__ == "__main__":  # pragma: nocover
    ops.main(AutopkgtestDispatcherCharm)
