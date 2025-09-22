# Copyright 2025 Canonical Ltd.
# See LICENSE file for licensing details.

"""Functions for managing and interacting with the workload.

The intention is that this module could be used outside the context of a charm.
"""

import logging

import charms.operator_libs_linux.v0.apt as apt

logger = logging.getLogger(__name__)

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


def start() -> None:
    """Start the workload (by running a commamd, for example)."""
    # You'll need to implement this function.
    # Ideally, this function should only return once the workload is ready to use.


# Functions for interacting with the workload, for example over HTTP:


def get_version() -> str | None:
    """Get the running version of the workload."""
    # You'll need to implement this function (or remove it if not needed).
    return None
