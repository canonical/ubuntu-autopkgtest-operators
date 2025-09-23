#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charm the application."""

import logging

import autopkgtest_website
import config_types
import ops

from charms.traefik_k8s.v2.ingress import IngressPerAppRequirer as IngressRequirer

logger = logging.getLogger(__name__)


RABBITMQ_USERNAME = "webservice"
RABBITMQ_VHOST = "autopkgtest"


class AutopkgtestWebsiteCharm(ops.CharmBase):
    """Charm the application."""

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)

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
        self.unit.status = ops.MaintenanceStatus("installing website")
        autopkgtest_website.install()

    def _on_config_changed(self, event: ops.ConfigChangedEvent):
        amqp_relation = self.model.get_relation("amqp")
        if not amqp_relation:
            self.unit.status = ops.BlockedStatus("waiting for AMQP relation")
            return

        self.unit.status = ops.MaintenanceStatus("configuring website")
        autopkgtest_website.configure(
            hostname=self.typed_config.hostname,
            amqp_hostname=self.amqp_hostname,
            amqp_vhost=RABBITMQ_VHOST,
            amqp_username=RABBITMQ_USERNAME,
            amqp_password=self.amqp_password,
        )

    def _on_start(self, event: ops.StartEvent):
        """Handle start event."""
        self.unit.status = ops.MaintenanceStatus("starting workload")
        autopkgtest_website.start()
        self.unit.open_port("tcp", 80)
        self.unit.status = ops.ActiveStatus()

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

        self.amqp_hostname = unit_data["hostname"]
        self.amqp_password = unit_data["password"]

        self.unit.status = ops.ActiveStatus()


if __name__ == "__main__":  # pragma: nocover
    ops.main(AutopkgtestWebsiteCharm)
