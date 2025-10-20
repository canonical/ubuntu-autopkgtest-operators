import subprocess
from collections import defaultdict

import charms.operator_libs_linux.v1.systemd as systemd


class SystemdHelper:
    def get_autopkgtest_unit_names(self, arch, ns):
        """Return autopkgtest worker unit filenames for given arch and numbers."""
        return [f"autopkgtest@worker-{arch}-{n}.service" for n in ns]

    def list_units_by_pattern(self, pattern):
        proc = subprocess.run(
            ["systemctl", "list-units", f"{pattern}"],
            capture_output=True,
            text=True,
            check=True,
        )

        # have to mangle the systemctl output manually here
        units = []
        for line in proc.stdout.splitlines():
            if "service" not in line or "masked" in line:
                continue
            units.append(line.split(" ")[2])

        return units

    def get_autopkgtest_units(self):
        """Return names for all autopkgtest services in a dict with keys lxd_worker[arch][n]."""
        lxd_worker_names = defaultdict(lambda: defaultdict(dict))

        lxd_workers = self.list_units_by_pattern("autopkgtest@*")

        for name in lxd_workers:
            # worker unit names are autopkgtest@cluster-{arch}-{n}.service
            name_parts = name.split("-")
            lxd_worker_names[name_parts[1]][name_parts[2]] = name

        return lxd_worker_names

    def set_up_systemd_units(self, target_config):
        """Enable requested units and remove unneeded ones.

        target_config is a dict which maps arches to number of workers.
        """
        lxd_worker_names = self.get_autopkgtest_units()

        for arch in target_config:
            n_units = len(lxd_worker_names[arch])
            target_units = target_config[arch]

            print(f"Got {target_units} units for {arch}")

            if n_units < target_units:
                unit_names = self.get_autopkgtest_unit_names(
                    arch, range(n_units + 1, target_units + 1)
                )
                systemd.service_enable("--now", *unit_names)
            elif target_units < n_units:
                unit_names = self.get_autopkgtest_unit_names(
                    arch, range(target_units + 1, n_units + 1)
                )
                systemd.service_disable("--now", *unit_names)

    def reload_worker_units(self):
        (lxd_worker_object_paths, _) = self.get_autopkgtest_units()

        for arch in lxd_worker_object_paths:
            for unit in lxd_worker_object_paths[arch]:
                unit_name = lxd_worker_object_paths[arch][unit]
                systemd.reload_unit(unit_name, restart_on_failure=True)
