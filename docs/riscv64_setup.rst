Setting up riscv64 remotes manually
==================================

This procedure documents the manual deployment of riscv64 LXD remotes for
autopkgtest when Juju cannot deploy those remotes. Juju is still used in the
orchestrator environment to register them with the janitor and dispatchers.

This is a work in progress, recording the deployment through creation of an
image after correcting the LXD proxy configuration, and documenting batch
registration with the janitor and dispatchers, followed by worker setup.

Prerequisites
-------------

Assume an already prepared OpenStack environment. Creating security groups,
flavors, images, networks and key pairs is outside the scope of this procedure:
these resources were prepared beforehand.

Commands below are run as ``root``, in the environment indicated for each
step, except for the manual image build or LXD commands, which runs as
``ubuntu``. This will be reminded. The provisioning environment needs an
authenticated OpenStack CLI. The orchestrator environment needs Juju access to
the existing orchestrator model.

Replace the placeholders below with the values for your environment:

* ``<image-id>``: the riscv64 image that boots with U-Boot. Multiple images can
  match the same name, so selecting by name alone is not sufficient.
* ``<riscv64-flavor>``: the prepared riscv64 flavor. The deployment described
  here uses 10 CPUs, 32 GiB RAM and 500 GiB of ephemeral storage.
* ``<network-id>`` and ``<key-name>``: the prepared remote network and SSH key
  pair.
* ``<egress-proxy-host>``: the HTTP proxy hostname, using port 3128 here.

The example also assumes a ``Ceph_NVMe`` volume type and a ``default`` security
group. Use the appropriate resources for your environment. The orchestrator
must be able to reach the remotes on TCP port 8443.

Prepare cloud-init user data
---------------------------

Save the following as ``riscv-userdata.yaml`` in the provisioning environment,
replacing the proxy hostname before creating the VMs.

.. warning::

   This configuration erases and repartitions the ephemeral disk. It identifies
   that disk through an existing filesystem label matching ``ephemeral*``,
   expecting OpenStack to expose the ephemeral storage as an ext4 filesystem
   labelled ``ephemeral0``. Verify that this assumption holds for your image
   and flavor. Do not run this disk preparation against storage containing
   data you need to keep.

.. code-block:: yaml

   #cloud-config

   apt:
     http_proxy: http://<egress-proxy-host>:3128/
     https_proxy: http://<egress-proxy-host>:3128/

   package_update: true
   package_upgrade: true

   snap:
     commands:
       00: ["set", "system", "proxy.http=http://<egress-proxy-host>:3128"]
       01: ["set", "system", "proxy.https=http://<egress-proxy-host>:3128"]
       02: ["install", "lxd", "--channel=6/edge"]

   mounts:
     # Disable auto-mounting the ephemeral partition.
     - [ "ephemeral0" ]

   write_files:
     - path: /etc/cron.hourly/restart-stuck-lxd
       permissions: '0755'
       content: |
         #!/bin/sh
         exec >/dev/null 2>&1
         if ! timeout 1m lxc ls; then
             timeout 10m snap restart lxd || reboot
             sleep 10
             timeout 1m lxc ls || reboot
         fi
     - path: /root/lxd-preseed.yaml
       permissions: '0644'
       content: |
         storage_pools:
           - name: default
             driver: btrfs
             config:
               source: /dev/disk/by-partlabel/ephemeral-lxd
               # default mount options: user_subvol_rm_allowed
               btrfs.mount_options: user_subvol_rm_allowed,noatime,commit=180
         networks:
           # A simple bridge is sufficient: autopkgtest does not need
           # instances on different cluster members to reach one another.
           - name: lxdbr0
             type: bridge
         profiles:
           - name: default
             description: LXD profile for autopkgtest
             devices:
               root:
                 path: /
                 pool: default
                 type: disk
               eth0:
                 name: eth0
                 network: lxdbr0
                 type: nic
             config:
               security.nesting: true
         config:
           core.https_address: "[::]:8443"
           core.proxy_http: http://<egress-proxy-host>:3128
           core.proxy_https: http://<egress-proxy-host>:3128
           core.proxy_ignore_hosts: "<internal-cidr-1>,<internal-ip-1>,<internal-ip-2>,<internal-ip-3>,<internal-cidr-2>,<internal-cidr-3>,127.0.0.1,<additional-ip-1>,<additional-ip-2>,<additional-ip-3>,::1,localhost"
           images.remote_cache_expiry: 1
           images.auto_update_cached: false

   # Device names and the cloud-init ephemeral0 alias are not reliable.
   # Use the filesystem label supplied by OpenStack to locate the disk.
   runcmd:
     - |
       (
       set -eux
       set -- /dev/disk/by-label/ephemeral*
       ebd=$1
       test -L "$ebd" || exit 1
       ebd=$(realpath "$ebd")
       sgdisk -o "$ebd"
       sgdisk -n 1:0:+32G -t 1:8200 -c 1:ephemeral-swap "$ebd"
       sgdisk -n 2:0:0 -c 2:ephemeral-lxd "$ebd"
       partprobe
       udevadm settle
       test -L /dev/disk/by-partlabel/ephemeral-swap || exit 1
       test -L /dev/disk/by-partlabel/ephemeral-lxd || exit 1
       mkswap -L swap /dev/disk/by-partlabel/ephemeral-swap
       echo "LABEL=swap none swap sw 0 0" >> /etc/fstab
       swapon -a
       wipefs -a /dev/disk/by-partlabel/ephemeral-lxd
       )
     - lxd init --preseed </root/lxd-preseed.yaml

The cloud-config installs LXD from ``6/edge`` and splits the ephemeral disk into
32 GiB of swap and a Btrfs partition occupying the remaining space for LXD.
The riscv64 Btrfs mount options intentionally do not include ``compress=lzo``.
The LXD preseed is already embedded in ``write_files``. No separate preseed
file needs to be supplied.

The hourly recovery script checks whether LXD responds, attempts to restart
it if necessary, and can reboot the VM if recovery fails.

The ``core.https_address`` setting is required to expose the LXD HTTPS API.
Listening on both IPv4 and IPv6 on port 8443 was confirmed with ``[::]:8443``
in this environment. Access is still subject to firewall and security group
rules.

LXD also needs its own proxy configuration: the APT, snap and shell proxy
settings do not replace ``core.proxy_http``, ``core.proxy_https`` and
``core.proxy_ignore_hosts``. Replace the hostname, CIDR and IP placeholders
with the values for your environment before using the preseed. The ignore
list above preserves the structure of the deployment's exclusions, with
internal addresses anonymized and loopback entries left unchanged.

These LXD proxy settings were found to be missing while investigating failed
image builds, including a manual build. If the remote has already been
initialized, updating the cloud-config alone does not update its running LXD
configuration. Do not rerun the destructive disk preparation to apply proxy
settings. After applying them, the image build completed successfully.

Create the VMs
--------------

From the prepared OpenStack provisioning environment, run the following script
with ``riscv-userdata.yaml`` in the current directory. Replace the placeholders
first.

.. code-block:: sh

   #!/bin/sh

   set -eu

   # Select the U-Boot image explicitly. Image names may not be unique.
   image='<image-id>'

   for i in $(seq -w 01 12); do
       openstack --os-compute-api-version 2.67 server create \
           --block-device "source_type=image,uuid=$image,destination_type=volume,volume_size=200,volume_type=Ceph_NVMe,boot_index=0,delete_on_termination=true" \
           --flavor '<riscv64-flavor>' \
           --nic 'net-id=<network-id>' \
           --key-name '<key-name>' \
           --security-group default \
           --user-data riscv-userdata.yaml \
           "autopkgtest-remote-$i"
   done

This creates 12 VMs named ``autopkgtest-remote-01`` through
``autopkgtest-remote-12``, each with a 200 GiB boot volume on ``Ceph_NVMe``.
The boot volume is deleted when its VM is deleted.

Allow cloud-init to finish preparing the remote before registering it.

One can check the cloud-init status through ssh:

.. code-block:: sh

   ssh ${host} cloud-init status

The output should look like "status: done"

Register each remote with the janitor
-------------------------------------

On each remote, as ``ubuntu`` (or ``root``, both work), generate a trust token:

.. code-block:: sh

   lxc config trust add --name janitor

Copy the generated token for use in the next step.

.. warning::

   Trust tokens are sensitive. Do not commit them to documentation or source
   control, or paste them into shared session notes.

In the orchestrator environment, with the orchestrator Juju model selected,
register that remote:

.. code-block:: sh

   juju run janitor/leader add-remote arch=riscv64 index=<remote_leader> token=<token>

Replace ``<remote_leader>`` with the index identifying the remote (the
``leader_de_la_remote`` value used during the deployment), and ``<token>``
with the token generated on that remote. For these manually provisioned
remotes, use the numeric hostname suffix without leading zeros:
``autopkgtest-remote-08`` has index ``8``.

Repeat token generation and registration for each remote. The janitor should
then start generating images. During this walkthrough, an image was
successfully created after correcting the LXD proxy settings.

To check access from the janitor, run as ``ubuntu`` on the janitor machine:

.. code-block:: sh

   lxc ls remote-riscv64-1:

This example lists instances on the remote registered with index ``1``.
Use the corresponding index for other remotes. A successful response confirms
LXD access, but does not by itself confirm that image generation has completed.

Register remotes in batches
^^^^^^^^^^^^^^^^^^^^^^^^^^

Instead of copying tokens individually, prepare a file named ``ip`` containing
the VM IP addresses, one per line. Run the following script from an environment
with SSH access to all those VMs and permission to run ``lxc`` there.

By default, the script retrieves existing unused tokens for the janitor and
dispatchers and generates the corresponding commands to run in the
orchestrator environment. Pass ``--apply-trust`` to create the tokens first.
Token creation and command generation use separate loops over the targets,
with a single token listing per host.

.. important::

   Dispatcher unit numbers ``10``, ``11`` and ``12`` are specific to this
   deployment, not fixed identifiers. Check ``juju status`` in the orchestrator
   model and adapt ``targets`` before running the script. These unit numbers
   are independent of the remote indexes derived from VM hostnames. If only
   some targets need registration, keep only those entries in ``targets``.

Save the script as ``trust_and_generate_commands.sh``:

.. code-block:: sh

   #!/bin/sh

   # Adapt dispatcher unit numbers to the orchestrator deployment.
   targets="janitor/leader dispatcher/10 dispatcher/11 dispatcher/12"

   case "$#:${1:-}" in
       0:|1:--apply-trust) ;;
       *)
           printf 'Usage: %s [--apply-trust]\n' "$0" >&2
           exit 2
           ;;
   esac

   trust_name() {
       case "$1" in
           janitor/leader) printf 'janitor\n' ;;
           dispatcher/*) printf 'dispatcher-%s\n' "${1#*/}" ;;
           *)
               printf 'unsupported target: %s\n' "$1" >&2
               return 1
               ;;
       esac
   }

   for host in $(cat ip); do
       hostname="$(ssh "$host" hostname)" || continue
       index="$(printf '%s\n' "$hostname" | awk -F '-' '{print $3 + 0}')"

       if [ "${1:-}" = "--apply-trust" ]; then
           for target in $targets; do
               name="$(trust_name "$target")" || exit 2
               if ! ssh "$host" "lxc config trust add --name $name" >/dev/null; then
                   printf 'failed to create %s token for %s\n' "$name" "$host" >&2
                   continue 2
               fi
           done
       fi

       output="$(ssh "$host" lxc config trust list-tokens -c nt -f csv)" ||
           continue

       for target in $targets; do
           name="$(trust_name "$target")" || exit 2

           token="$(printf '%s\n' "$output" |
               awk -F ',' -v name="$name" '
                   $1 == name { token = $NF; count++ }
                   END {
                       if (count > 1) exit 1
                       if (count == 1) print token
                   }
               ')" || {
                   printf 'multiple tokens for %s on %s\n' "$name" "$host" >&2
                   continue
               }

           if [ -z "$token" ]; then
               printf 'no token for %s on %s\n' "$name" "$host" >&2
               continue
           fi

           printf 'juju run %s add-remote arch=riscv64 index=%s token=%s\n' \
               "$target" "$index" "$token"
       done
   done

The index comes from the third field of hostnames such as
``autopkgtest-remote-08``. Adding zero in ``awk`` converts it to decimal,
removing leading zeros without changing ``10`` to ``1``. This avoids passing
``08`` or ``09`` to an index parser that treats leading zeros as octal.

The output of ``trust add`` is discarded so that only generated Juju commands
reach standard output. Its errors remain on standard error. If token creation
fails, the script reports the failure and skips that host without generating
any commands for it. Trust tokens already created on that host remain in
place: create any missing ones separately, then rerun without ``--apply-trust``.

``lxc config trust list-tokens`` only lists unused tokens. Names are matched
exactly, so ``dispatcher-1`` cannot select ``dispatcher-10``. If a target has
no token or multiple tokens, the script reports it on standard error and
generates no command for that target on that host. With ``--apply-trust``, the
script creates fresh tokens: do not use that option for targets with unused
tokens still pending.

To create the tokens and capture just the generated commands in a file named
``commands``, leaving error messages on the terminal:

.. code-block:: sh

   umask 077
   sh trust_and_generate_commands.sh --apply-trust > commands

If the tokens have already been created, omit ``--apply-trust``:

.. code-block:: sh

   sh trust_and_generate_commands.sh > commands

The redirection captures standard output only. Do not add ``2>&1``, which
would mix errors into the command file. Review the generated commands, then
transfer the file securely to the orchestrator environment. With the correct
Juju model selected, run:

.. code-block:: sh

   sh commands

.. warning::

   The ``commands`` file contains trust tokens. Keep it private, do not commit
   or share it, and remove all copies once registration is complete.

Start the workers
-----------------

Wait for the required images to finish building before starting workers.
In the orchestrator environment, configure two workers per remote on each
of ``dispatcher/10`` and ``dispatcher/11``, for remote indexes ``1`` through
``12``, then reconcile their worker units:

.. code-block:: sh

   #!/bin/sh
   set -eu

   for unit in 10 11; do
       for index in $(seq 1 12); do
           juju run "dispatcher/$unit" set-worker-count \
               arch=riscv64 count=2 index="$index"
       done
   done

   for unit in 10 11; do
       juju run "dispatcher/$unit" reconcile-worker-units
   done

Use explicit unit numbers here rather than ``dispatcher/leader`` so that
both intended dispatchers receive the actions. As with registration, adapt
these unit numbers to your deployment. ``index=1..12`` is shorthand for
separate calls, not a literal action argument.

For this deployment, ``dispatcher/12`` is left idle for now: it is registered
with the remotes but is not included in these worker configuration or
reconciliation commands. These commands do not stop any workers that may
already exist on that unit.

Image build services
--------------------

On the janitor, the systemd services for container image creation follow this
naming pattern:

.. code-block:: text

   autopkgtest-build-image@riscv64-<remote_leader>-<distro_release_name>-container.service

Here, ``<remote_leader>`` is the remote index used during registration and
``<distro_release_name>`` is the target Ubuntu release codename.

During this deployment, these services failed, and a manual build failed too.
Investigation identified missing LXD proxy settings, now included in the
preseed above. A race condition was initially suspected but has not been
confirmed. After configuring the LXD proxy, the image build completed
successfully.

Run an image build manually
^^^^^^^^^^^^^^^^^^^^^^^^^^^

To investigate a failed build, inspect the service template on the janitor
to find the command and its environment:

.. code-block:: sh

   cat /etc/systemd/system/autopkgtest-build-image@.service
   cat /etc/environment.d/proxy.conf

The relevant service settings are shown below, with the internal mirror
hostname replaced by a placeholder:

.. code-block:: ini

   User=ubuntu
   EnvironmentFile=-/etc/environment.d/proxy.conf
   Environment="PATH=/home/ubuntu/autopkgtest/tools:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/snap/bin"
   Environment="MIRROR=http://<mirror-host>/ubuntu/"
   Environment="AUTOPKGTEST_TEMP_INSTANCE=autopkgtest-prepare-%i"
   ExecStart=build-image-on-remote

Switch to the service user before preparing the environment and running the
build:

.. code-block:: sh

   sudo -iu ubuntu

Declare and export the proxy variables from ``/etc/environment.d/proxy.conf``
in the diagnostic shell before running the script. For this deployment, they
have the following form, with internal hostnames and network ranges anonymized:

.. code-block:: sh

   export http_proxy='http://<egress-proxy-host>:3128'
   export https_proxy='http://<egress-proxy-host>:3128'
   export no_proxy='127.0.0.1,localhost,::1,<internal-cidr-1>,<internal-cidr-2>,<internal-cidr-3>'

Replace the placeholders with the actual values from the janitor's proxy
configuration. Keep the loopback addresses as shown and preserve the internal
network exclusions so that connections to those networks bypass the proxy.

Use the actual mirror value from the service template. Replace ``%i`` with
the instance identifier being investigated, for example
``riscv64-1-jammy-container``.

Run the script with shell tracing enabled:

.. code-block:: sh

   PATH=/home/ubuntu/autopkgtest/tools:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/snap/bin \
   MIRROR='http://<mirror-host>/ubuntu/' \
   AUTOPKGTEST_TEMP_INSTANCE=autopkgtest-prepare-riscv64-1-jammy-container \
       sh -x /usr/local/bin/build-image-on-remote riscv64-1-jammy-container

Replace the example identifier in both places when investigating another
remote or release. This runs an actual image build, not a dry run.

Run this command as ``ubuntu``, matching the systemd service user and its
per-user LXD client configuration. The direct invocation does not reproduce
systemd's runtime directory,
notification handling or timeouts.

.. warning::

   Shell tracing can reveal sensitive values. Redact credentials and tokens
   before sharing its output.

This invocation is a diagnostic step. It also failed during this deployment,
leading to the discovery of the missing LXD proxy settings described above.
This is not by itself a recovery procedure.
