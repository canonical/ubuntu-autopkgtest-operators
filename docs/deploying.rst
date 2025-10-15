Deploying autopkgtest-cloud
===========================

autopkgtest-cloud is designed around the requirements of the production
deployment, and requires/assumes the following:

* A cloud to run the infrastructure itself on. Any cloud supported by Juju
  will do, even LXD.
* An Openstack cloud to run tests on, which is accessible from the
  infrastructure cloud.
* A Swift instance to upload results to.

The supported deployment method is via `Mojo <https://mojo.canonical.com/>`_
and `Juju <https://jaas.ai/>`_. The Juju charms themselves need to be built
using the `charm` tool. The deployment scripts rely on `jq`. So you need the
following:

* ``mojo`` (snap or deb)
* ``snap:juju``
* ``jq`` (snap or deb)

Set up Mojo environment
-----------------------

First, set up Juju and Mojo (note: you *always* need these environment
variables set when interacting with Mojo. The staging and production instances
set these for you, so skip this step when deploying there. This is for
developing locally.)

.. code-block::

  juju bootstrap # if necessary - follow any instructions, using LXD is fine for a local deployment

  export MOJO_ROOT=~/.local/share/mojo
  export MOJO_SERIES=focal
  export MOJO_PROJECT=autopkgtest-cloud
  export MOJO_WORKSPACE=autopkgtest-cloud
  export MOJO_SPEC=<autopkgtest-cloud-clone-location>/mojo/
  export MOJO_STAGE=devel # or staging/production for a staging resp. production deployment

  # you might run into https://bugs.launchpad.net/mojo/+bug/1833703 - apply the
  # MR there if so
  mojo project-new $MOJO_PROJECT -s $MOJO_SERIES --container containerless
  mojo workspace-new --project $MOJO_PROJECT -s $MOJO_SERIES $MOJO_SPEC $MOJO_WORKSPACE

at this point ``mojo workspace-list`` should list an ``autopkgtest-cloud``
workspace. Now we can start configuring the deployment. First we need a
directory to store our secrets (swift and cloud credentials, and some others
in production):

Supply secrets
--------------

First create the location for Mojo to find the secrets:

.. code-block::

  mkdir -p ${MOJO_ROOT}/LOCAL/${MOJO_WORKSPACE}/${MOJO_STAGE}
  cd !$

Now put them in. ``swift_password`` should just contain your swift password.
``novarcs.tar`` should contain the cloud ``.rc`` files which must be named:
``$REGION.rc`` for amd64 (which will also run i386 tests) and
``$REGION-$ARCH.rc`` for everything else. For example:

.. code-block::

  $ tar tf novarcs.tar
  lcy02.rc
  bos01-arm64.rc
  bos01-ppc64el.rc
  ...

These are all that are required for a devel deployment. For production there
are more secrets required, see the ``service-bundle`` and charm
``config.yaml`` for more information.

``public-swift-creds`` is also a required secrets file.
See ``charms/focal/autopkgtest-web/webcontrol/update-github-jobs`` script for
the required environmental variables.

Configure
---------

Now modify ``$MOJO_SPEC/$MOJO_STAGE/service-bundle`` and replace ``XXX`` with
concrete values. You'll want to at least edit the various ``swift-`` options
(get those from a cloud ``rc`` file), and probably ``n-workers`` depending on
the ``rc`` files you configured earlier.

You will also have to setup a security group rule to allow SSH access from
the cloud worker to the created instances, for example, use::

  openstack security group rule create --protocol tcp --dst-port 22 default

The ``lxd-remotes`` configuration item of the ``autopkgtest-lxd-worker``
application accepts YAML describing the LXD hosts to be used. This is of the
form ``[arch -> ip -> number of parallel jobs]``, for example::

  lxd-remotes: |-
    armhf:
      127.0.0.1: 4


The deployment will set up an *autopkgtest-haproxy-lxd* service. In
production we use this together with the `create-armhf-cluster-member` to
manage armhf LXD workers as a cluster. This is optional in development. See
:ref:`Managing cluster nodes` for more on this.

Deploy
------

We're ready to deploy the system now. Run ``mojo run``, and ``mojo`` will
download the charms, deploy and relate them. You can watch this happening
with ``juju debug-log --tail``.

Once has completed, you can run ``juju ssh autopkgtest-cloud-worker/0 cat
rabbitmq.cred`` to get the rabbitmq credentials, which you can plug into a
client like ``britney2-ubuntu``'s ``run-autopkgtest`` configuration file, and
start submitting jobs.

**NOTE**: For production use you should create a restricted RabbitMQ user and
not use the administrator credentials retrieved above::

  $ sudo rabbitmqctl add_user ${RABBITMQ_USERNAME} ${RABBITMQ_PASSWORD}
  $ sudo rabbitmqctl set_permissions test_request '' '^amq.default$' ''

This should probably be automated as a script run from the mojo bundle.

Making configuration changes
----------------------------

Edit the ``service-bundle`` file as above, and run ``mojo run`` again.

About cloud environments quotas
-------------------------------

Each OpenStack environment has a quota, meaning there is a limit to the number
of instances, cpu cores, RAM, disk, etc, amount that can be spawned at the same
time. This quota can be seen with the command `nova limits`.

Changing the quota requires IS approval, and how to do that depends on the cloud
environments, so it's out of scope of this doc.
Computing the required quota however fits right in here, so let's details a bit what we want.

Let's say we want 100 autopkgtest runners.
Regular flavor is ``cpu2-ram4-disk20``, so:

.. code-block:: text

  100 *  2 = 200 cores
  100 *  4 = 400GB of RAM
  100 * 20 = 2000GB of disk

We take a 10% margin to accommodate "big_packages", that run on
``cpu4-ram8-disk100``, which gives us:

.. code-block:: text

  220 cores
  440GB RAM
  2200GB disk

A 10% margin means we can concretely run 90 regular jobs, and 10 big_packages.
That should be enough for most cases, as in average, we're more around 2-4% of
big_packages tests (observed today, 2024-11-20, with ``stats.ipynb``).

autopkgtest-cloud Storage
----------------------------

There is a quota for storage in the ``autopkgtest-cloud`` environment.
Sometimes when a unit/machine/application is removed, the storage for that unit can be
left in a "detached" state, but still exist, taking up part of the
quota. ``mojo run`` will throw an error in this state if the quota
is exceeded.
Therefore, whenever you remove a unit/machine/application, you should
check to see if removing it left any detached storage behind.

``juju storage`` will show all the current storage, and
``juju remove-storage storage-name/99`` will remove the storage.

When running ``juju storage``, make sure the drives listed are
"attached". If they are "detached", they are not in use and
can be deleted.


Update the code
---------------

Note: see :ref:`testing wip changes` if you're pushing a work in progress
change.

The above reconfiguration only effects configuration changes. If you want to
change the charms themselves or the code of ``autopkgtest-cloud`` or
``autopkgtest-web``, you need to *build*, *upload to the charm store* and
then run an *upgrade*. For example, for ``autopkgtest-web``:

.. code-block::

 $ # this is all happening on your local development system
 $ charmcraft clean
 $ charmcraft pack
 Packing the charm
 Building charm in '/root' | (47.1s)
 Created 'autopkgtest-web_ubuntu-20.04-amd64.charm'.
 Charms packed:
    autopkgtest-web_ubuntu-20.04-amd64.charm
 $ charmcraft upload autopkgtest-web_ubuntu-20.04-amd64.charm --name ubuntu-release-autopkgtest-web
 Revision XX of 'ubuntu-release-autopkgtest-web' created
 $ # For staging use the edge channel
 $ # For production use the release channel
 $ charmcraft release ubuntu-release-autopkgtest-web --revision=XX --channel=$channel # using the revision number given above
 Revision XX of charm 'ubuntu-release-autopkgtest-web' released to edge
 $ # make sure you have committed the changes you've packed
 $ git tag -a autopkgtest-$charm-$revision
 $ git push --tags
 $ # Test charm in staging
 $ # Code is merged
 $ # pack, upload and release to stable
 $ # tag the commit on master with autopkgtest-$charm-$revision

Where $charm is either equal to "web" or "cloud-worker".

Then run ``mojo run`` on the system where you want to deploy the update - this
will pull the updated charm from the charm store.

If, however, you're doing a charm update that only involves code changes,
without any required changes to ``juju config`` options, you can run
the following script:

``autopkgtest-cloud/mojo/upgrade-charm``

Which will update the code for all the charms with the latest revision in
charmhub, without running all the various ``mojo run`` stages.

Tagging the charm revisions
---------------

When releasing the charm to stable, please then *tag* the update you are releasing,
so that others can see which git commit corresponds to a charm revision.
Use the format <charm>-<revision>. You should push a tag even if you are working
on a scratch branch (for staging). It makes it possible for others to see what
is being worked on, and make fixes to it if necessary.

Checking for cowboys
^^^^^^^^^^^^^^^^^^^^

A "cowboy change", or "cowboy", is one done to the code in staging or
production without updating the charm. While this is not best practice and
shouldn't be done it may have been to expedite a fix and keep the queue moving.
Before updating a charm if you know of a cowboy or have reason to believe there
is one in play you can follow these steps to check for differences between the
code on disk and in the charm.

.. code-block::

 $ # this is happening on the bastion
 $ juju download --channel latest/edge ubuntu-release-autopkgtest-cloud-worker
 $ juju scp ubuntu-release-autopkgtest-cloud-worker_XYZ.charm autopkgtest-cloud-worker/1:/tmp/
 $ # this is happening on the cloud worker
 $ mkdir /tmp/charm/; cd /tmp/
 $ unzip -q ubuntu-release-autopkgtest-cloud-worker_XYZ.charm -d /tmp/charm/
 $ diff -Nurp ~/autopkgtest-cloud/ /tmp/charm/autopkgtest-cloud/

Using the staging environment
-----------------------------

If you've got access to the production deployment then there is also a role
account ``stg-proposed-migration``. It is deployed identically to the
production deployment except-

* The URL is `<https://autopkgtest.staging.ubuntu.com>`_.
* Fewer workers are available.
* If there is a charm release in edge, it will be used.

Make sure to test all charm upgrades and work in progress stuff there. If
necessary the environment can be completely destroyed and redeployed, so
don't worry about messing it up. For that reason it's important to keep
automated deployments working and eliminate the need for post-deploy manual
hacks.

Testing WIP changes
^^^^^^^^^^^^^^^^^^^

The ``charm release`` command demonstrated above releases to the *stable*
channel by default. If you want to test a change in staging before it is
merged into the main branch, you can release into *edge* with ``charm release
--channel=edge ...``, and then use

.. code-block::

  $ mojo run

Under the staging user as usual to test your change. Staging tracks edge by
default.


Deploying a local "production" environment
------------------------------------------

When mentioning a local "production" environment, that means a fully charmed
environment, similar to how production is deployed. If you just want to hack
on some scripts, you'd be better off reading the various READMEs around them,
because it's very likely that there are simpler solutions.


Prerequisites
^^^^^^^^^^^^^

An OpenStack environment
""""""""""""""""""""""""

`microstack <https://microstack.run/>`_ is currently the most viable option,
albeit it doesn't provide ``swift`` storage yet (This might have changed by
the time you read this!). It is quick and easy to set up in a bridged VM if you
don't want it running on your host or don't have an Ubuntu host available.

Running this will allow you to fully understand everything and will help you get
more comfortable with debugging infrastructure issues.

For the following guide, we'll assume you have that environment RC file named
``devstack.rc`` with this content:

.. code-block::

  export OS_AUTH_URL=http://10.20.21.10/openstack-keystone
  export OS_USERNAME=demo
  export OS_PASSWORD=thatsapassword
  export OS_USER_DOMAIN_NAME=users
  export OS_PROJECT_DOMAIN_NAME=users
  export OS_PROJECT_NAME=demo
  export OS_AUTH_VERSION=3
  export OS_IDENTITY_API_VERSION=3

Here are some quick tips if you go with your own *microstack*. Do that after you
complete its `official setup <https://microstack.run/#get-started>`_:

  * Don't bother too much with ``multipass`` if it doesn't work right away. The
    only thing you really need is an Ubuntu LTS VM with the correct specs (don't
    hesitate to beef a bit the recommended specs for a smoother experience!),
    preferably on a network bridge for a direct access to the LAN if it's
    running on another machine.

  * You can use the `admin` user, but it is very much advised to use the `demo`
    one instead, to avoid developing features with overprivileged user. Its
    RC file should be created when you configure OpenStack under the name
    ``demo-openrc``.

  * Run ``sunbeam openrc`` to get the admin credentials.

  * Run ``sunbeam dashboard-url`` to know where the OpenStack admin web dashboard is.

  * Make sure you have the correct route to OpenStack ``external-network`` on
    your machine: ``sudo ip route add 10.20.20.0/24 via $microstack_machine_ip``.
    You may also need a route to the dashboard for which you got the IP at the
    previous step.

  * From OpenStack dashboard, logged in with the ``admin`` credentials:

    * Activate DHCP on ``external-subnet`` (subnet of ``external-network``), so
      that your instances easily get an IP.

    * Make ``external-network`` public, so that it can be used by the `demo` user.

    * Edit default security group to allow inbound SSH.

    * Upload a first image usable by `autopkgtest-cloud` (name it something like
      ``ubuntu-$release-daily-amd64-server-20231207-disk1.img``). You
      can grab a ``$release-server-cloudimg-amd64.img`` file from https://cloud-images.ubuntu.com/
      **NOTE**: by default, only ``jammy`` is enabled on a ``devel`` env. Don't
      bother with other releases, but don't forget that one!

    * Create a public flavor usable for `autopkgtest-cloud`. Default name is
      ``cpu2-ram4-disk20``, configurable in ``mojo/service-bundle``, look for
      ``worker-default-flavor``.
      This is actually easier with the CLI: ``openstack flavor create cpu2-ram4-disk20 --vcpus 2 --ram 4096 --disk 20``


swift storage
"""""""""""""

For Canonical employees, you can ask for some RadosGW credentials, to access a
hosted ``swift``-like interface.

Once ``microstack`` supports swift out of the box, you will be able to use that
instead.

A ``juju`` cloud
""""""""""""""""

The OpenStack environment will not be used to run the ``autopkgtest-cloud`` code
itself, as it is only for the cloud worker to use. The code will run locally on
whatever ``juju`` cloud you set up with ``juju bootstrap``. A local LXD cloud
is fine.

Please note that on a local LXD cloud, you may run into some strange network issues.
They may be `related to Docker + LXD <https://documentation.ubuntu.com/lxd/en/latest/howto/network_bridge_firewalld/#prevent-connectivity-issues-with-lxd-and-docker>`_.



Running autopkgtest-cloud
^^^^^^^^^^^^^^^^^^^^^^^^^

The procedure is mostly the same as described in this whole page, except there
are a few steps that can gain you some time:

1. To set up the ``mojo`` environment, you have a ``mojorc`` file at the root of this repo that you can source and should work out of the box for a devel environment.
2. Not every secrets are needed in local dev, so ``~/.local/share/mojo/LOCAL/autopkgtest-cloud/devel`` should only contain the following:
  * ``devstack.rc`` for your OpenStack access
  * ``influx-{hostname,password}.txt`` with dummy values, the files only need to exist
  * ``novarcs.tar`` created as described in the *Supply secrets* section (``tar cvf novarcs.tar devstack.rc`` will do)
  * ``swift_password`` with your OpenStack password found in your ``canonistack.rc`` (or any other ``swift`` password from another environment)
3. Edit ``mojo/service-bundle`` for the following values, each time in the ``devel`` branch of the code:
  * ``swift-username``
  * ``swift-project-name``
  * ``storage_path_internal`` (source ``canonistack.rc``, then run ``swift auth``)
  * ``charm`` paths for `autopkgtest-cloud-worker`, `autopkgtest-lxd-worker`, and `autopkgtest-web` applications: in `devel`, we want to use locally built charms with possible local changes
4. Build the local charms, as they are used in the `devel` configuration you just wrote:
  * ``cd charms/focal/autopkgtest-cloud-worker && charmcraft clean && charmcraft pack``
  * ``cd charms/focal/autopkgtest-web && charmcraft clean && charmcraft pack``
5. ``mojo run`` should now be working enough to get you a local working web UI

If you are developing and making additional modifications to your code that needs
to be redeployed as a new charm, the quickest way is by running something like
this:
``charmcraft pack && juju refresh autopkgtest-web --path ./autopkgtest-web_ubuntu-20.04-amd64.charm``
