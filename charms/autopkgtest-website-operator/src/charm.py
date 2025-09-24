#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charm the application."""

import logging

import autopkgtest_website
import config_types
import ops
from ops.framework import StoredState

from charms.traefik_k8s.v2.ingress import IngressPerAppRequirer as IngressRequirer

logger = logging.getLogger(__name__)


RABBITMQ_USERNAME = "webservice"


class AutopkgtestWebsiteCharm(ops.CharmBase):
    """Charm the application."""

    _stored = StoredState()

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)

        self._stored.set_default(
            got_amqp_creds=False,
            amqp_hostname=None,
            amqp_password=None,
        )

        self.typed_config = self.load_config(
            config_types.WebsiteConfig, errors="blocked"
        )

        self.ingress = IngressRequirer(
            self, port=80, strip_prefix=True, relation_name="ingress"
        )

        framework.observe(self.on.install, self._on_install)
        framework.observe(self.on.start, self._on_start)
        framework.observe(self.on.config_changed, self._on_config_changed)
        framework.observe(self.on.amqp_relation_joined, self._on_amqp_relation_joined)
        framework.observe(self.on.amqp_relation_changed, self._on_amqp_relation_changed)

    def _on_install(self, event: ops.InstallEvent):
        """Install the workload on the machine."""

        self.unit.status = ops.MaintenanceStatus("installing website software")
        autopkgtest_website.install()

    def _on_config_changed(self, event: ops.ConfigChangedEvent):
        """Configure/Reconfigure service"""

        self.unit.status = ops.MaintenanceStatus("configuring service")
        if not self._stored.got_amqp_creds:
            self.unit.status = ops.BlockedStatus("waiting for AMQP relation")
            return

        self.unit.status = ops.MaintenanceStatus("configuring website")
        autopkgtest_website.configure(
            hostname=self.typed_config.hostname,
            amqp_hostname=self._stored.amqp_hostname,
            amqp_username=RABBITMQ_USERNAME,
            amqp_password=self._stored.amqp_password,
        )

        self.on.start.emit()

    def _on_start(self, event: ops.StartEvent):
        """Handle start event."""

        if isinstance(self.unit.status, ops.BlockedStatus):
            return

        self.unit.status = ops.MaintenanceStatus("starting workload")
        autopkgtest_website.start()
        self.unit.open_port("tcp", 80)
        self.unit.status = ops.ActiveStatus()

    def _on_amqp_relation_joined(self, event: ops.RelationJoinedEvent):
        self.unit.status = ops.MaintenanceStatus(
            f"Setting up {event.relation.name} connection"
        )

        event.relation.data[self.unit].update(
            {"username": RABBITMQ_USERNAME, "vhost": "/"}
        )

    def _on_amqp_relation_changed(self, event):
        self.unit.status = ops.MaintenanceStatus(
            f"Updating up {event.relation.name} connection"
        )

        unit_data = event.relation.data[event.unit]

        if "password" not in unit_data:
            logger.info("rabbitmq-server has not sent password yet")
            return

        self._stored.amqp_hostname = unit_data["hostname"]
        self._stored.amqp_password = unit_data["password"]
        self._stored.got_amqp_creds = True
        self.on.config_changed.emit()


if __name__ == "__main__":  # pragma: nocover
    ops.main(AutopkgtestWebsiteCharm)
