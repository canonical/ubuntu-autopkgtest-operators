#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

import os

import action_types
import autopkgtest_website
import config_types
import ops
from charms.data_platform_libs.v0.data_interfaces import DatabaseRequires
from charms.traefik_k8s.v2.ingress import IngressPerAppRequirer as IngressRequirer
from ops.framework import StoredState

RABBITMQ_USERNAME = "website"
HTTP_PORT = 80
POSTGRESQL_DATABASE_NAME = "autopkgtest"


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

        self.postgresql = DatabaseRequires(
            self,
            relation_name="postgresql",
            database_name=POSTGRESQL_DATABASE_NAME,
        )

        self._stored.set_default(
            installed=False,
            got_amqp_creds=False,
            amqp_hostname=None,
            amqp_password=None,
            got_postgresql_creds=False,
            postgresql_hostname=None,
            postgresql_username=None,
            postgresql_password=None,
        )

        self.typed_config = self.load_config(
            config_types.WebsiteConfig, errors="blocked"
        )

        framework.observe(self.on.install, self._on_install)
        framework.observe(self.on.upgrade_charm, self._on_install)
        framework.observe(self.on.start, self._on_start)
        framework.observe(self.on.config_changed, self._on_config_changed)
        framework.observe(self.on.secret_changed, self._on_secret_changed)

        framework.observe(self.on.set_alert_action, self._on_set_alert)
        framework.observe(self.on.remove_alert_action, self._on_remove_alert)

        framework.observe(self.on.amqp_relation_joined, self._on_amqp_relation_joined)
        framework.observe(self.on.amqp_relation_changed, self._on_amqp_relation_changed)
        framework.observe(self.on.amqp_relation_broken, self._on_amqp_relation_broken)
        framework.observe(
            self.on.postgresql_relation_joined, self._on_postgresql_relation_joined
        )
        framework.observe(
            self.on.postgresql_relation_changed, self._on_postgresql_relation_changed
        )
        framework.observe(
            self.on.postgresql_relation_broken, self._on_postgresql_relation_broken
        )
        framework.observe(
            self.postgresql.on.database_created, self._on_postgresql_database_created
        )

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

        if not self._stored.got_postgresql_creds:
            self.unit.status = ops.BlockedStatus("waiting for PostgreSQL relation")
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

        postgresql_creds = {
            "postgresql_hostname": self._stored.postgresql_hostname,
            "postgresql_username": self._stored.postgresql_username,
            "postgresql_password": self._stored.postgresql_password,
        }

        self.unit.status = ops.MaintenanceStatus("configuring website")
        autopkgtest_website.configure(
            hostname=self.typed_config.hostname,
            releases=self.typed_config.releases,
            http_port=HTTP_PORT,
            amqp_creds=amqp_creds,
            swift_creds=swift_creds,
            postgresql_creds=postgresql_creds,
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

    def _on_set_alert(self, event: ops.ActionEvent):
        params = event.load_params(action_types.SetAlertAction, errors="fail")
        autopkgtest_website.set_alert(params.level.value, params.message)

    def _on_remove_alert(self, event: ops.ActionEvent):
        autopkgtest_website.remove_alert()

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

    def _on_postgresql_relation_joined(self, event: ops.RelationJoinedEvent):
        """Handle PostgreSQL relation joined event."""
        self.unit.status = ops.MaintenanceStatus(
            f"Setting up {event.relation.name} connection"
        )

    def _on_postgresql_relation_changed(self, event: ops.RelationChangedEvent):
        """Handle PostgreSQL relation changed event."""
        relation = event.relation
        if not relation or not relation.data:
            return

        relation_data = relation.data.get(relation.app)
        if not relation_data:
            return

        if "username" in relation_data and "password" in relation_data:
            username = relation_data.get("username")
            password = relation_data.get("password")
            # endpoints format: "hostname:port"
            endpoints = relation_data.get("endpoints", "")

            if username and password and endpoints:
                hostname = endpoints.split(":")[0] if ":" in endpoints else endpoints
                self._stored.postgresql_hostname = hostname
                self._stored.postgresql_username = username
                self._stored.postgresql_password = password
                self._stored.got_postgresql_creds = True
                self.on.config_changed.emit()

    def _on_postgresql_relation_broken(self, event: ops.RelationBrokenEvent):
        """Handle PostgreSQL relation broken event."""
        self._stored.got_postgresql_creds = False
        self._stored.postgresql_hostname = None
        self._stored.postgresql_username = None
        self._stored.postgresql_password = None
        self.on.config_changed.emit()

    def _on_postgresql_database_created(self, event):
        """Handle PostgreSQL database created event from DatabaseRequires."""
        self._stored.postgresql_hostname = (
            event.endpoints.split(":")[0] if ":" in event.endpoints else event.endpoints
        )
        self._stored.postgresql_username = event.username
        self._stored.postgresql_password = event.password
        self._stored.got_postgresql_creds = True
        self.on.config_changed.emit()


if __name__ == "__main__":  # pragma: nocover
    # Inject proxy variables into the environment
    os.environ["http_proxy"] = os.getenv("JUJU_CHARM_HTTP_PROXY", "")
    os.environ["https_proxy"] = os.getenv("JUJU_CHARM_HTTPS_PROXY", "")
    os.environ["no_proxy"] = os.getenv("JUJU_CHARM_NO_PROXY", "")

    ops.main(AutopkgtestWebsiteCharm)
