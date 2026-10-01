.. GitHub renders this file without Sphinx: hide Sphinx-only directives
   (literalinclude) there rather than showing them as raw text.

.. github display off

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

Copy `riscv-userdata.yaml <riscv64/riscv-userdata.yaml>`__ to the
provisioning environment, replacing the proxy hostname before creating the VMs.

.. warning::

   This configuration erases and repartitions the ephemeral disk. It identifies
   that disk through an existing filesystem label matching ``ephemeral*``,
   expecting OpenStack to expose the ephemeral storage as an ext4 filesystem
   labelled ``ephemeral0``. Verify that this assumption holds for your image
   and flavor. Do not run this disk preparation against storage containing
   data you need to keep.

.. literalinclude:: riscv64/riscv-userdata.yaml
   :language: yaml
   :caption: riscv-userdata.yaml

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
list in ``riscv-userdata.yaml`` preserves the structure of the deployment's
exclusions, with internal addresses anonymized and loopback entries left
unchanged.

These LXD proxy settings were found to be missing while investigating failed
image builds, including a manual build. If the remote has already been
initialized, updating the cloud-config alone does not update its running LXD
configuration. Do not rerun the destructive disk preparation to apply proxy
settings. After applying them, the image build completed successfully.

Create the VMs
--------------

From the prepared OpenStack provisioning environment, deploy and run the
`deploy-vms.sh <riscv64/deploy-vms.sh>`__ script with ``riscv-userdata.yaml``
in the current directory. Replace the placeholders first.

.. literalinclude:: riscv64/deploy-vms.sh
   :language: sh
   :caption: deploy-vms.sh

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

Replace ``<remote_leader>`` with the index identifying the remote, and
``<token>`` with the token generated on that remote. For these manually
provisioned remotes, use the numeric hostname suffix without leading zeros:
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
the VM IP addresses, one per line. Run `trust_and_generate_commands.sh
<riscv64/trust_and_generate_commands.sh>`__ from an environment with SSH access
to all those VMs and permission to run ``lxc`` there.

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

.. literalinclude:: riscv64/trust_and_generate_commands.sh
   :language: sh
   :caption: trust_and_generate_commands.sh

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
``12``, then reconcile their worker units. This can be done using
`enable_workers.sh <riscv64/enable_workers.sh>`__:

.. literalinclude:: riscv64/enable_workers.sh
   :language: sh
   :caption: enable_workers.sh

.. warning::

   Remember to check and adapt the leaders in the script depending on your
   setup.

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
preseed of ``riscv-userdata.yaml``. A race condition was initially suspected
but has not been confirmed. After configuring the LXD proxy, the image build
completed successfully.

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
