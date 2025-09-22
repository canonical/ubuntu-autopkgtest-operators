#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charm the application."""

import logging

# A standalone module for workload-specific logic (no charming concerns):
import autopkgtest_website
import ops

from charms.traefik_k8s.v2.ingress import IngressPerAppRequirer as IngressRequirer

logger = logging.getLogger(__name__)

PORT = 8080


class AutopkgtestWebsiteCharm(ops.CharmBase):
    """Charm the application."""

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)
        self.ingress = IngressRequirer(
            self, port=PORT, strip_prefix=True, relation_name="ingress"
        )

        framework.observe(self.on.install, self._on_install)
        framework.observe(self.on.start, self._on_start)

    def _on_install(self, event: ops.InstallEvent):
        """Install the workload on the machine."""
        autopkgtest_website.install()

    def _on_start(self, event: ops.StartEvent):
        """Handle start event."""
        self.unit.status = ops.MaintenanceStatus("starting workload")
        autopkgtest_website.start()
        version = autopkgtest_website.get_version()
        if version is not None:
            self.unit.set_workload_version(version)
        self.unit.status = ops.ActiveStatus()

    def _on_config_changed(self, event: ops.ConfigChangedEvent):
        ops.MaintenanceStatus()
        # todo
        ops.ActiveStatus()


if __name__ == "__main__":  # pragma: nocover
    ops.main(AutopkgtestWebsiteCharm)
