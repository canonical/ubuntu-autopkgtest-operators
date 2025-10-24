import subprocess
import time
from collections import Counter

import charms.operator_libs_linux.v1.systemd as systemd


class SystemdHelper:
    def generate_worker_unit_names(self, arch, ns):
        """Return autopkgtest worker unit names for given arch and numbers."""
        return [f"autopkgtest-worker@remote-{arch}-{n}.service" for n in ns]

    def count_worker_units(self):
        """Count number of worker units per architecture."""
        proc = subprocess.run(
            [
                "systemctl",
                "list-units",
                "--plain",
                "--no-legend",
                "--no-pager",
                "--type=service",
                "autopkgtest-worker@remote-*.service",
            ],
            capture_output=True,
            text=True,
            check=True,
        )

        worker_count = Counter(
            line.split()[0].split("@", 1)[1].removesuffix(".service").split("-")[1]
            for line in proc.stdout.splitlines()
        )

        return worker_count

    def reconcile_systemd_worker_units(self, target_config):
        """Enable requested units and disable unneeded ones.

        target_config is a dict which maps arches to number of workers.
        """
        worker_count = self.count_worker_units()

        for arch in target_config:
            n_units = worker_count[arch]
            target_units = target_config[arch]

            print(f"Target {arch} units: {target_units}, already existing: {n_units}")

            if n_units < target_units:
                unit_names = self.generate_worker_unit_names(
                    arch, range(n_units + 1, target_units + 1)
                )
                chunk = 10
                for i in range(0, len(unit_names), chunk):
                    if i > 0:
                        # don't drown amqp with connection requests
                        time.sleep(1)
                    units_slice = unit_names[i : i + chunk]
                    print(f"Activating units {units_slice}")
                    systemd.service_enable("--now", *units_slice)

            elif target_units < n_units:
                print("Deleting extra units")
                unit_names = self.generate_worker_unit_names(
                    arch, range(target_units + 1, n_units + 1)
                )
                # TODO: graceful shutdown of worker units
                systemd.service_disable("--now", *unit_names)
                systemd._systemctl("reset-failed", *unit_names)
