# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Functions for managing and interacting with the workload.

The intention is that this module could be used outside the context of a charm.
"""

import logging
from pathlib import Path

import charms.operator_libs_linux.v0.apt as apt

logger = logging.getLogger(__name__)

# Config files create by the charm
SITES_AVAILABLE_PATH = Path("/etc/apache2/sites-available/")

# Packages to install
PACKAGES = [
    "apache2",
    "libjs-bootstrap",
    "libjs-jquery",
]


def install() -> None:
    """Install website"""

    logger.info("Updating package index")
    apt.update()
    logger.info("Installing packages")
    apt.add_package(PACKAGES)


def configure() -> None:
    pass


def start() -> None:
    """Start the workload"""
    pass
