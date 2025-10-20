#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charm the application."""

import logging

import autopkgtest_website
import config_types
import ops
from charms.traefik_k8s.v2.ingress import IngressPerAppRequirer as IngressRequirer
from ops.framework import StoredState

logger = logging.getLogger(__name__)


RABBITMQ_USERNAME = "website"
HTTP_PORT = 80


class AutopkgtestWebsiteCharm(ops.CharmBase):
    """Charm the application."""

    _stored = StoredState()

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)

        self.ingress = IngressRequirer(
            self,
            port=HTTP_PORT,
            relation_name="ingress",
        )

        self._stored.set_default(
            installed=False,
            got_amqp_creds=False,
            amqp_hostname=None,
            amqp_password=None,
        )

        self.typed_config = self.load_config(
            config_types.WebsiteConfig, errors="blocked"
        )

        framework.observe(self.on.install, self._on_install)
        framework.observe(self.on.upgrade_charm, self._on_install)
        framework.observe(self.on.start, self._on_start)
        framework.observe(self.on.config_changed, self._on_config_changed)
        framework.observe(self.on.secret_changed, self._on_secret_changed)
        framework.observe(self.on.amqp_relation_joined, self._on_amqp_relation_joined)
        framework.observe(self.on.amqp_relation_changed, self._on_amqp_relation_changed)
        framework.observe(self.on.amqp_relation_broken, self._on_amqp_relation_broken)

    def _on_install(self, event: ops.InstallEvent):
        """Install the workload on the machine."""
        self.unit.status = ops.MaintenanceStatus("installing website software")
        autopkgtest_website.install()

        self._stored.installed = True

    def _on_config_changed(self, event: ops.ConfigChangedEvent):
        """Configure/Reconfigure service."""
        # If we blocked during install, it may happen that a config_changed
        # event gets processed before we installed. In this case, emit an
        # install event.
        if not self._stored.installed:
            self.on.install.emit()

        self.unit.status = ops.MaintenanceStatus("configure: gathering data")

        if not self._stored.got_amqp_creds:
            self.unit.status = ops.BlockedStatus("waiting for AMQP relation")
            return

        if self.typed_config.swift_juju_secret:
            try:
                swift_password = self.typed_config.swift_juju_secret.get_content().get(
                    "password"
                )
            except ops.ModelError:
                self.unit.status = ops.BlockedStatus("swift secret not available")
                return
        else:
            swift_password = ""

        self.unit.status = ops.MaintenanceStatus("configuring service")

        swift_creds = {
            k: v
            for k, v in self.typed_config.model_dump().items()
            if k.startswith("swift_") and isinstance(v, str)
        }
        swift_creds["swift_password"] = swift_password

        amqp_creds = {
            "rabbithost": self._stored.amqp_hostname,
            "rabbituser": RABBITMQ_USERNAME,
            "rabbitpassword": self._stored.amqp_password,
        }

        self.unit.status = ops.MaintenanceStatus("configuring website")
        autopkgtest_website.configure(
            hostname=self.typed_config.hostname,
            http_port=HTTP_PORT,
            amqp_creds=amqp_creds,
            swift_creds=swift_creds,
        )

        self.on.start.emit()

    def _on_start(self, event: ops.StartEvent):
        """Handle start event."""
        if isinstance(self.unit.status, ops.BlockedStatus):
            return

        self.unit.status = ops.MaintenanceStatus("starting workload")
        autopkgtest_website.start()
        self.unit.open_port("tcp", HTTP_PORT)
        self.unit.status = ops.ActiveStatus()

    def _on_amqp_relation_joined(self, event: ops.RelationJoinedEvent):
        self.unit.status = ops.MaintenanceStatus(
            f"Setting up {event.relation.name} connection"
        )

        event.relation.data[self.unit].update(
            {"username": RABBITMQ_USERNAME, "vhost": "/"}
        )

    def _on_amqp_relation_changed(self, event: ops.RelationChangedEvent):
        unit_data = event.relation.data[event.unit]

        # the first relation_changed event does not contain credentials
        if "password" not in unit_data:
            logger.info("rabbitmq-server has not sent password yet")
            return

        self.unit.status = ops.MaintenanceStatus(
            f"Updating up {event.relation.name} connection"
        )

        self._stored.amqp_hostname = unit_data["hostname"]
        self._stored.amqp_password = unit_data["password"]
        self._stored.got_amqp_creds = True
        self.on.config_changed.emit()

    def _on_amqp_relation_broken(self, event: ops.RelationBrokenEvent):
        self._stored.got_amqp_creds = False
        self._stored.amqp_hostname = None
        self._stored.amqp_password = None

        self.on.config_changed.emit()

    def _on_secret_changed(self, event: ops.SecretChangedEvent):
        self.on.config_changed.emit()


if __name__ == "__main__":  # pragma: nocover
    ops.main(AutopkgtestWebsiteCharm)
