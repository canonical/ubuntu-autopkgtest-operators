#!/usr/bin/env python3
# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details

import action_types
import autopkgtest_janitor
import config_types
import ops
from ops.framework import StoredState


class AutopkgtestJanitorCharm(ops.CharmBase):
    """Autopkgtest janitor charm class."""

    _stored = StoredState()

    def __init__(self, framework: ops.Framework):
        super().__init__(framework)

        self.typed_config = self.load_config(
            config_types.JanitorConfig, errors="blocked"
        )

        self._stored.set_default(
            remotes=set(),
            releases=[],
        )

        framework.observe(self.on.install, self._on_install)
        framework.observe(self.on.start, self._on_start)
        framework.observe(self.on.upgrade_charm, self._on_install)

        framework.observe(self.on.add_remote_action, self._on_add_remote)
        framework.observe(self.on.remove_remote_action, self._on_remove_remote)
        framework.observe(self.on.reconfigure_action, self._on_reconfigure)
        framework.observe(
            self.on.rebuild_all_images_action, self._on_rebuild_all_images
        )

        framework.observe(self.on.config_changed, self._on_config_changed)

    def _on_install(self, event: ops.InstallEvent):
        self.unit.status = ops.MaintenanceStatus("installing janitor charm")
        autopkgtest_janitor.install(self.typed_config.autopkgtest_git_branch)

    def _on_start(self, event: ops.StartEvent):
        autopkgtest_janitor.start()
        self.unit.status = ops.ActiveStatus()

    def _on_add_remote(self, event: ops.ActionEvent):
        """Handle adding a new remote."""
        params = event.load_params(action_types.AddRemoteAction, errors="fail")
        arch = params.arch
        token = params.token
        try:
            autopkgtest_janitor.add_remote(
                arch,
                token,
                self._stored.releases,
                self.typed_config.max_containers,
                self.typed_config.max_virtual_machines,
            )
        except Exception as e:
            event.fail(f"failed to add remote: {e}")
            return

        self._stored.remotes.add(arch)

        event.set_results({"result": f"Added remote for {arch}"})

    def _on_remove_remote(self, event: ops.ActionEvent):
        """Handle removing a remote."""
        params = event.load_params(action_types.RemoveRemoteAction, errors="fail")
        arch = params.arch
        autopkgtest_janitor.remove_remote(arch, self._stored.releases)
        if arch in self._stored.remotes:
            self._stored.remotes.remove(arch)

    def _on_reconfigure(self, event: ops.ActionEvent):
        """Reconfigure."""
        self.unit.status = ops.MaintenanceStatus("reconfiguring")
        self.on.config_changed.emit()

    def _on_rebuild_all_images(self, event: ops.ActionEvent):
        """Rebuild all images."""
        autopkgtest_janitor.rebuild_all_images()

    # config helpers

    def _on_config_changed(self, event: ops.ConfigChangedEvent):
        autopkgtest_janitor.configure(
            arches=self._stored.remotes,
            autopkgtest_branch=self.typed_config.autopkgtest_git_branch,
            mirror=self.typed_config.mirror,
            stored_releases=self._stored.releases,
            extra_releases=self.typed_config.extra_releases,
            max_containers=self.typed_config.max_containers,
            max_vms=self.typed_config.max_virtual_machines,
        )
        self._stored.releases = autopkgtest_janitor.get_releases(
            self.typed_config.extra_releases
        ).copy()
        self.on.start.emit()


if __name__ == "__main__":  # pragma: nocover
    ops.main(AutopkgtestJanitorCharm)
