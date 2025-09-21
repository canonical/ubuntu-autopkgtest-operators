Tests on LXD
============

Ubuntu's ``armhf`` tests currently run inside LXD containers on ``arm64``
hosts. Each host is a cloud instance with 4 CPUs, 16G of RAM and 100G of disk
space, and can run up to 3 parallel tests. These hosts are statically assigned,
in that they stay around as long as they're working.

There is a separate ``autopkgtest-lxd-worker`` cloud worker for dispatching
and reporting these test requests/results.

The armhf instances are available on the ``autopkgtest-lxd-worker`` cloud worker as lxd remotes.

.. code-block::

  $ lxc remote list
  +------------------------+------------------------------------------+---------------+-------------+--------+--------+
  |          NAME          |                   URL                    |   PROTOCOL    |  AUTH TYPE  | PUBLIC | STATIC |
  +------------------------+------------------------------------------+---------------+-------------+--------+--------+
  | lxd-armhf-10.15.190.52 | https://10.15.190.52:8443                | lxd           | tls         | NO     | NO     |
  +------------------------+------------------------------------------+---------------+-------------+--------+--------+

Managing cluster nodes
~~~~~~~~~~~~~~~~~~~~~~

To deploy a new node, after having sourced the cloud ``.rc`` file (``source ~/.scalingstack/bos03-arm64.rc``, for example) on the bastion itself:

.. code-block:: bash

    $ IMAGE=$(openstack image list --format csv -c Name --quote none | grep auto-sync/ubuntu-jammy-22.04-arm64-server | tail -n1)
    $ NET_ID=$(openstack network show net_prod-proposed-migration --format json | jq -r '."id"')
    $ openstack server create --image $IMAGE --flavor autopkgtest-cpu8-ram16-disk160-arm64 --nic net-id=$NET_ID --key-name prod-proposed-migration-environment --security-group default --security-group lxd --user-data ~/autopkgtest-cloud/autopkgtest-cloud/tools/armhf-lxd.userdata -- lxd-armhfN
    $ # To quickly deploy a bunch of nodes, here is a loop:
    $ for nb in $(seq --format='%02.0f' 1 14); do  openstack server create --image "$IMAGE" --flavor autopkgtest-cpu8-ram16-disk160-arm64 --nic net-id="$NET_ID" --key-name prod-proposed-migration-environment --security-group default --security-group lxd --user-data ~/autopkgtest-cloud/autopkgtest-cloud/tools/armhf-lxd.userdata -- lxd-armhf-bos03-$nb ; done
    $ # To quickly run a bunch of commands on all nodes, here is a one-liner:
    $ for srvr in $(openstack server list --name armhf --sort-column Name --format csv -c Name --quote none | tail -n+2); do echo "==================================="; echo "$srvr"; ip="$(openstack server show $srvr | grep addresses | grep -o "10.145.243.*" | cut -d'|' -f1)"; echo "$ip"; date; ssh -oStrictHostKeyChecking=no -oUserKnownHostsFile=/dev/null ubuntu@$ip 'echo success && lxc image list && sudo apt update && sudo apt upgrade'; date; done

*Note*: on a new environment, you may need to:
  * upload the `prod-proposed-migration-environment` SSH key with something like ``openstack keypair create --public-key ~/.ssh/id_ed25519.pub prod-proposed-migration-environment``.
  * make sure the correct security group is created with the right rules (at least allowing ingress 8443/tcp), with ``mojo/make-lxd-secgroup`` (open that script and adapt it if needed, it's currently not very flexible but easy to tweak to your needs).

In the event that you want to deploy the new node on a specific host you can use the ``--hint same_host`` argument e.g. ``openstack server create --hint same_host=2e838a71-f6d9-46c4-94f9-dd0c6a2632fe``.

N.B. Wait for it to finish deploying (watch ``/var/log/cloud-init-output.log``) on the newly deployed node. If you do not wait then the unit files for the node on the autopkgtest-lxd-worker will fail as there will be missing images for releases. You can also use ``lxc image list`` to check what was correctly built, and finish the work manually if needed. Commands should be in the cloud-init log.

And you then need to add the IP to the ``autopkgtest-lxd-worker``
``lxd-remotes`` section, in the ``service-bundle`` for example:

.. code-block:: yaml

  lxd-remotes: |-
  armhf:
    10.15.190.52: 3
    ...: 3

Then on the controller ensure all units, using ``juju status``, are in a green state.
And then source the novarc file, ``.novarc``, with the region information for the
juju environment and execute ``mojo run`` on the controller.

..
  The armhf instances are available via an lxd cluster exposed there as a
  remote:

  .. code-block::

    $ lxc remote list
    +------------------------+------------------------------------------+---------------+-------------+--------+--------+
    |          NAME          |                   URL                    |   PROTOCOL    |  AUTH TYPE  | PUBLIC | STATIC |
    +------------------------+------------------------------------------+---------------+-------------+--------+--------+
    | lxd-armhf-10.15.190.52 | https://10.15.190.52:8443                | lxd           | tls         | NO     | NO     |
    +------------------------+------------------------------------------+---------------+-------------+--------+--------+
    $ lxc cluster list lxd-armhf-10.15.190.52:
    +--------------------------------------+--------------------------+----------+--------+-------------------+--------------+
    |                 NAME                 |           URL            | DATABASE | STATE  |      MESSAGE      | ARCHITECTURE |
    +--------------------------------------+--------------------------+----------+--------+-------------------+--------------+
    | a0c731f0-8008-11eb-b8e0-e83935eabcda | https://10.44.82.33:8443 | YES      | ONLINE | fully operational | aarch64      |
    +--------------------------------------+--------------------------+----------+--------+-------------------+--------------+
    | a1b148ae-800c-11eb-8a92-e83935eabcda | https://10.44.82.34:8443 | YES      | ONLINE | fully operational | aarch64      |
    +--------------------------------------+--------------------------+----------+--------+-------------------+--------------+
    | d6f396a0-800e-11eb-90a5-e83935eabcda | https://10.44.82.35:8443 | YES      | ONLINE | fully operational | aarch64      |
    +--------------------------------------+--------------------------+----------+--------+-------------------+--------------+

  In order to avoid having one of the cluster nodes being a single point of
  failure, access to the cluster is proxied via a ``haproxy``.

  Managing cluster nodes
  ~~~~~~~~~~~~~~~~~~~~~~

  To deploy a new node, use the
  ``autopkgtest-cloud/tools/create-armhf-cluster-member`` script. There are
  three types of node. It's called like this, after having sourced the cloud
  ``.rc`` file.

  .. code-block::

    $ IMAGE=$(openstack image list --format csv -c Name --quote none | grep auto-sync/ubuntu-focal-daily-arm64-server | tail -n1)
    $ NET_ID=$(openstack network show net_prod-proposed-migration --format json | jq -r '."id"')
    $ nova boot --image $IMAGE --flavor m1.xlarge --nic net-id=$NET_ID --key_name prod-proposed-migration-environment --security-groups default,lxd --user-data <(autopkgtest-cloud/autopkgtest-cloud/tools/create-armhf-cluster-member TYPE [IP]) -- lxd-armhfN

  Where ``TYPE`` is one of:

  * ``bootstrap``. Use this when creating the first node. It will build cloud
    images using ``autopkgtest-build-lxd`` which LXD will then distribute to
    the other cluster members. ``IP`` is not required as we are creating a new
    cluster here.
  * ``leader``. Same as bootstrap except ``IP`` is required as the address of an
    existing cluster node to join to.
  * ``node``. Join a cluster as a non leader. Use this when making additional
    nodes and there is a working leader. ``IP`` is required and should normally
    be the IP of the leader.

  Secondly you then need to add the IP to the ``haproxy-lxd-armhf`` services in the ``service-bundle``, and also update the test count to be ``3 * <number of hosts>`` in ``autopkgtest-lxd-worker``'s ``lxd-remotes`` section, for example:

  .. code-block:: yaml

    lxd-remotes: |-
    armhf:
      10.15.190.52: 9

  and then ``mojo run`` on the controller.
