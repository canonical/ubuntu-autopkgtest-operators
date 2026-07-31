Deploying ubuntu-autopkgtest-operators
======================================

ubuntu-autopkgtest-operators is designed around the requirements of the production
deployment, and requires/assumes the following:

* A cloud to run the infrastructure itself on. Any cloud supported by Juju with
  a VM backend will do.
* LXD remotes to run tests on.
* A Swift instance to upload results to.

The supported deployment method is via `Terraform
<https://developer.hashicorp.com/terraform>`_ and `Juju <https://jaas.ai/>`_.
The Juju charms themselves need to be built using the `charm` tool. The
deployment scripts rely on `jq`. So you need the following:

* ``snap:terraform``
* ``snap:juju``

Set up Terraform environment on Canonical environments
------------------------------------------------------

This documentation allows to deploy on Canonical ProdStack environment. For
now, we use internal bastions to manage the environments, but on a mid-term
basis, it might end-up in `JaaS`.

For a deployment in another type of environment, there will be a need to
configure properly `juju` with a `bootstrap` call and then ensure that
terraform can operate it smoothly. This is beyond this document's scope.

First we get (if needed) the terraform models.

.. code-block::

  git clone https://github.com/canonical/ubuntu-engineering-terraform-models

The architecture of the `autopkgtest` deployment relies on an orchestrator
model and multiple remotes (one per architecture) models. These models should
be created and switched in between before running the different terraform
playbooks.

.. code-block::

  [juju switch to the orchestrator model]
  ln -v -s ubuntu-engineering-terraform-models/models/ps7/ubuntu-engineering/autopkgtest/stg-autopkgtest-orchestrator-ps7 plan
  cd plan
  terraform plan -var local_run=true
  [check plan]
  terraform apply -var local_run=true

at this point ``juju status`` should show 5 deployed charms:

.. code-block::
   App                   Version  Status  Scale  Charm                          Channel        Rev  Exposed  Message
   dispatcher                     active      1  ubuntu-autopkgtest-dispatcher  latest/edge     53  no       running 0 workers on 0 remotes
   ingress-configurator           active      1  ingress-configurator           latest/stable   95  no       Ready
   janitor                        active      1  ubuntu-autopkgtest-janitor     latest/edge     44  no       connected to 0 remotes, building for 2 releases
   rabbitmq              3.12.1   active      1  rabbitmq-server                3.12/stable    294  yes      Unit is ready
   website                        active      1  ubuntu-autopkgtest-website     latest/edge     34  yes

   Unit                      Workload  Agent  Machine  Public address  Ports           Message
   dispatcher/16*            active    idle   131      10.151.167.117                  running 0 workers on 0 remotes
   ingress-configurator/10*  active    idle   129      10.151.167.137                  Ready
   janitor/14*               active    idle   133      10.151.167.135                  connected to 0 remotes, building for 2 releases
   rabbitmq/15*              active    idle   132      10.151.167.185  5672,15672/tcp  Unit is ready
   website/14*               active    idle   130      10.151.167.252  80/tcp

The `ingress-configurator` has a need for an external relation with the
`admin/prod-machine-ingress-ps7.ingress-ps7-ubuntu-engineering-ubuntu-com`
hosted on the `juju-controller-36-production-ps7`. On production environment,
this is on the same controller so this is straightforward. On the staging
environment, it's a cross-controller offer. Initially it was not working out of
terraform, so a

.. code-block::

   juju integrate ingress-configurator:haproxy-route juju-controller-36-production-ps7:admin/prod-machine-ingress-ps7.ingress-ps7-ubuntu-engineering-ubuntu-com

Was needed. A fix is waiting in `GitHub
<https://github.com/canonical/ubuntu-engineering-terraform-models/pull/729>`__

The integrate command normally takes an app:endpoint app2:endpoint. When the
endpoint is provided by an external model, the form is slightly different

.. code-block::

   juju integrate <app>[:<endpoint>] [<controller>:]<offer_url>[:<endpoint>]

Now it should work smoothly.

Remotes deployment
------------------

The remotes are deployed in a similar fashion but relying on the `stg-autopkgtest-remotes-<arch>-ps7`
environments. For AMD64, as for an example:

.. code-block::

  [juju switch to the remote model]
  ln -v -s ubuntu-engineering-terraform-models/models/ps7/ubuntu-engineering/autopkgtest/stg-autopkgtest-remotes-amd64-ps7 plan
  cd plan
  terraform plan -var local_run=true
  [check plan]
  terraform apply -var local_run=true

After some time, the juju status should look like

.. code-block::

   App         Version  Status  Scale  Charm   Channel      Rev  Exposed  Message
   aproxy               active      3  aproxy  latest/edge   97  no       Service ready on target proxy egress.ps7.internal:3128
   lxd-remote           active      3  lxd     latest/edge  973  yes

   Unit            Workload  Agent  Machine  Public address  Ports     Message
   lxd-remote/66   active    idle   81       10.151.164.98   8443/tcp
     aproxy/23*    active    idle            10.151.164.98             Service ready on target proxy egress.ps7.internal:3128
   lxd-remote/67*  active    idle   82       10.151.164.253  8443/tcp
     aproxy/24     active    idle            10.151.164.253            Service ready on target proxy egress.ps7.internal:3128
   lxd-remote/68   active    idle   83       10.151.164.213  8443/tcp
     aproxy/25     active    idle            10.151.164.213            Service ready on target proxy egress.ps7.internal:3128

Note that creating the `VM` and `container` imges in the applications takes
some time, don't expect to be able to run tests minute 0.

Configure
---------

Now add each of the LXD remotes you would like to use as autopkgtest workers. If you are using the
``lxd-remote`` charm, you can use the dedicated action::
  $ juju run lxd-remote/<leader> get-client-token name=dispatcher
  $ juju run lxd-remote/<leader>get-client-token name=janitor

Otherwise, you can generate the tokens manually from within the machine you want to use as your LXD remote.

Use the tokens generated by the previous steps with the charms in the orchestration environment::
  $ juju run dispatcher/leader add-remote arch=<remote_arch> index=<remote_leader> token=<dispatcher_token>
  $ juju run janitor/leader add-remote arch=<remote_arch> index=<remote_leader> token=<dispatcher_token>

Once all remotes are configured you can check the resulting worker config::
  $ juju run dispatcher/leader show-target-config

Which should show you the target number of systemd units that will be spawned per worker. You can
change this number with::
  $ juju run dispatcher/leader set-worker-count arch=<worker_arch> count=<unit_count>

The default number of workers is stored in the config value ``default-worker-count``. Any changes
to this value will affect remotes added afterwards.

Deploy
------

Once you are happy with the unit count, you can create or destroy the systemd units as appropriate with::
  $ juju run dispatcher/leader reconcile-worker-units

The website charm should start the frontend automatically.

Testing an autopkgtest run
--------------------------

The website is stateless, so requesting a test to run from the website for a
specific suite (eg `stonking`) and arch (eg `amd64`) won't work as long as the
database doesn't list a test that ran in that specific release for this
specific arch.

Fortunately, the `website` app comes with a `run-autopkgtest` python script
that submits through `RabbitMQ` a request to run a test. The release and arch
will become visible in the website afterwards.

Making configuration changes
----------------------------

Edit the terraform plan and run ``terraform plan`` and ``terraform apply``.

About cloud environments quotas
-------------------------------

Each OpenStack environment has a quota, meaning there is a limit to the number
of instances, cpu cores, RAM, disk, etc, amount that can be spawned at the same
time.

Update the code
---------------

Note: see :ref:`testing wip changes` if you're pushing a work in progress
change.

The above reconfiguration only effects configuration changes. If you want to
change the charms themselves, you need to *build*, *upload to the charm store* and
then run a *refresh*. For example, for ``autopkgtest-dispatcher-operator``:

.. code-block::

 $ # this is all happening on your local development system
 $ charmcraft clean
 $ charmcraft pack
 [...]
 Charms packed:
    autopkgtest-dispatcher_amd64.charm
 $ charmcraft upload autopkgtest-dispatcher_amd64.charm --name ubuntu-autopkgtest-dispatcher
 Revision XX of 'ubuntu-autopkgtest-dispatcher' created
 $ # For staging use the edge channel
 $ # For production use the release channel
 $ charmcraft release ubuntu-autopkgtest-dispatcher --revision=XX --channel=$channel # using the revision number given above
 Revision XX of charm 'ubuntu-autopkgtest-dispatcher' released to edge
 $ # Test charm in staging
 $ # pack, upload and release to stable

IS GitOps will periodically refresh the deployed charms.


Using the staging environment
-----------------------------

If you've got access to the production deployment then there are a set of staging environments
available on the Ubuntu engineering bastion.

* The URL is `<https://autopkgtest.staging.ubuntu.com>`_.
* Smaller clusters are available.
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

  $ terraform plan
  $ terraform apply

Under the staging user as usual to test your change. Staging tracks edge by
default.
