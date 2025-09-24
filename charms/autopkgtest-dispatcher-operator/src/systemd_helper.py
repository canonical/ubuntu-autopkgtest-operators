import subprocess
from collections import defaultdict

import charms.operator_libs_linux.v1.systemd as systemd


class SystemdHelper:
    def get_autopkgtest_unit_names(self, arch, ns):
        """Return the names of the autopkgtest worker unit files
        for the given arch and requested unit numbers
        """
        return [f"autopkgtest@worker-{arch}-{n}.service" for n in ns]

    def reload_all_units(self):
        systemd.daemon_reload()

    def reload_unit(self, unit):
        systemd.service_reload(unit, restart_on_failure=True)

    def enable_units(self, units):
        for unit in units:
            systemd.service_enable(unit)
            systemd.service_start(unit)
        self.reload_all_units()

    def disable_units(self, units):
        for unit in units:
            systemd.service_stop(unit)
            systemd.service_disable(unit)
        self.reload_all_units()

    def list_units_by_pattern(self, pattern):
        proc = subprocess.run(
            ["systemctl", "list-units", f"{pattern}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            encoding="utf-8",
        )

        # have to mangle the systemctl output manually here
        units = []
        for line in proc.stdout.split("\n"):
            if "service" not in line or "masked" in line:
                continue
            units.append(line.split(" ")[1])

        return units

    def get_autopkgtest_units(self):
        """Return names for all autopkgtest services in the following tuple:
        (lxd_workers, build_adt_image), where lxd_workers is a dict with keys lxd_worker[arch][n]
        and build_adt_image is a dict with keys build_adt_keys[arch][release]
        """
        lxd_worker_names = defaultdict(lambda: defaultdict(dict))

        lxd_workers = self.list_units_by_pattern("autopkgtest@*")

        for unit in lxd_workers:
            (name, _, _, _, _, _, _, _, _, _) = unit
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
                self.enable_units(unit_names)
            elif target_units < n_units:
                unit_names = self.get_autopkgtest_unit_names(
                    arch, range(target_units + 1, n_units + 1)
                )
                self.disable_units(unit_names)

    def reload_worker_units(self):
        (lxd_worker_object_paths, _) = self.get_autopkgtest_units()

        for arch in lxd_worker_object_paths:
            for unit in lxd_worker_object_paths[arch]:
                unit_name = lxd_worker_object_paths[arch][unit]
                self.reload_unit(unit_name)
