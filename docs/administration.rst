Admin
=====

Some common tasks admins might need to perform.

Give a package more time or more resources
------------------------------------------

Tests can be run with more time or on bigger instances (bigger instances for
cloud tests only, so not ``armhf``) by adding the appropriate lines to
`<https://code.launchpad.net/~ubuntu-release/autopkgtest-cloud/+git/autopkgtest-package-configs>`_.
See `its README
<https://git.launchpad.net/~ubuntu-release/autopkgtest-cloud/+git/autopkgtest-package-configs/tree/README.md>`_
for documentation on the syntax of the files.

Changes pushed to the repo will be used automatically by the workers shortly
after pushing. They are pulled every minute, so wait one minute before
retrying.

Re-running tests
----------------

Britney's `excuses.html
<https://people.canonical.com/~ubuntu-archive/proposed-migration/update_excuses.html>`_
has retry symbols ♻ after "Regression"s, which submit a test request via
`autopkgtest-cloud's
webcontrol <https://git.launchpad.net/autopkgtest-cloud/tree/webcontrol>`_.
Requesting individual manual runs can also be done with britney's ``run-autopkgtest`` script on ``snakefruit``. Due to firewalling this currently can only be run on ``snakefruit``, so define this shell alias::

 alias run-autopkgtest='ssh snakefruit.canonical.com sudo -i -u ubuntu-archive run-autopkgtest'

Then you can run ``run-autopkgtest --help`` to see the usage. e. g.::

  # specific architecture
  run-autopkgtest -s xenial -a armhf --trigger glib2.0/2.46.1-2 libpng udisks2
  # all configured britney architectures (current default: i386, amd64, armhf, arm64, ppc64el, s390x)
  run-autopkgtest -s xenial --trigger glibc/2.21-0ubuntu4 libpng udisks2

Note that you must always submit a correct "trigger", i. e. the
package/version on excuses.html that caused this test to run. This is
necessary so that britney can correctly map results to requests and as we
only use packages from -proposed for the trigger (via apt pinning). This apt
pinning can be disabled with the ``--all-proposed`` option. If
``--all-proposed`` is too broad - it can make packages migrate when they need
things from proposed - you can alternatively just specify ``--trigger``
multiple times, for all packages in -proposed that need to be tested and
landed together::

 run-autopkgtest -s xenial --trigger php-foo/1-1 --trigger php-foo-helpers/2-2 php-foo

``lp:ubuntu-archive-tools`` contains a script
``retry-autopkgtest-regressions`` which will build a series of request.cgi
URLs for re-running all current regressions. It has options for picking a
different series, running for a bileto PPA, or for a different test state (e.
g. ``--state=RUNNING`` is useful to requeue lost test requests). You can also
limit the age range. See ``--help`` for details and how to run it
efficiently.

re-queueing all outstanding test requests
-----------------------------------------

If rabbitmq has an issue and ends up dumping all of the pending test
requests, you can get proposed-migration to requeue them. Ensure it is not
running, and as ``ubuntu-archive@snakefruit``, remove
``~ubuntu-archive/proposed-migration/data/RELEASE-proposed/autopkgtest/pending.json``.
Then on the next run, proposed-migration will have forgotten that it queued
any tests and will re-request them all. (This will include any which are
currently running - if that is a concern, stop britney and wait until these
jobs finish and next time the result will be fetched and the test request not
duplicated.)

Autopkgtest controller access
-----------------------------

Most workers (for i386, amd64, arm64, ppc64el, s390x) are running in a ProdStack
instance of juju service ``autopkgtest-cloud-worker``::

  ssh -t wendigo.canonical.com sudo -H -u prod-ues-proposed-migration juju ssh autopkgtest-cloud-worker/0

Rolling out new worker or web code and changing configuration
-------------------------------------------------------------

See :ref:`Update the code`.

Most configuration is exposed via charm settings. Edit the ``service-bundle``
file, pull it on the cloud controller and run ``mojo run``. The workers
should then reload themselves if necessary.

If this doesn't happen for any reason, there is a charm action, so you can::

  juju run-action <unit> [<unit> ...] reload-units

where ``<unit>`` is the cloud/lxd worker shown in ``juju status``.

Updating autopkgtest and autodep8
---------------------------------
The autopkgtest-cloud-worker and autopkgtest-lxd-worker applications have
checkouts of the Ubuntu Release team's autopkgtest and autodep8 branches.
These branches can be automatically updated (which will remove any local
changes) on a unit via the following::

  juju run-action <unit> [<unit> ...] update-sources

Deploying new LXD nodes
-----------------------

See :ref:`Managing cluster nodes`

Creating new LXD images before official ones are available
----------------------------------------------------------

On the machine which is building images, run::

  MIRROR=http://ftpmaster.internal/ubuntu/ RELEASE=<new release> autopkgtest/tools/autopkgtest-build-lxd images:ubuntu/<old release>/armhf

Journal log analysis
--------------------

Logs can be analyzed using journal fields ``ADT_PACKAGE``, ``ADT_ARCH``,
``ADT_RELEASE``, and ``ADT_PARAMS``, though the latter might be useless. For
example, ``journalctl ADT_PACKAGE=autopkgtest`` shows all worker logs for
tests of autopkgtest.

Watching all logs
^^^^^^^^^^^^^^^^^

On the cloud/lxd controller, run::

  journalctl -u autopkgtest@*.service

Watching one cloud/arch
^^^^^^^^^^^^^^^^^^^^^^^

.. code-block:: bash

 journalctl -u autopkgtest@<cloud>-<arch>-*.service

Metrics
-------

Both the staging and the production instances publish metrics to `a dashboard
on the Ubuntu KPIs
<https://ubuntu-release.kpi.ubuntu.com/d/76Oe_0-Gz/autopkgtest?orgId=1>`_.
This should let admins see at a glance if the system is healthy. A small
amount of churn (errors) is normal, but if there is a high level then this
indicates something to be looked into.

``armhf`` cluster nodes in error almost always need checking out, as they
usually indicate that the LXD host has gone down and needs redeploying.

If the queues are non empty but flat
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

This may indicate that the infra is somehow unable to process jobs, but
sometimes this is just related to ``cache-amqp`` being stuck somehow.
This script runs on the webunits, and does its job on the leader of those
units. It has a semaphore mechanism, so should be able to work in a fully
distributed system. However, this hasn't been maintained much, and sometime
this semaphores can break, either by having more than one message in the
``semaphore-<queuename>-<release>-<arch>`` queue, or by having none. You can fix
that by stopping all the ``cache-amqp`` services (on all units!), and manually
running ``cache-amqp --refresh-semaphores --debug`` on the leader, which will
nuke the semaphore queues and recreate them. The ``--debug`` will help you
figure out if something goes wrong.


Opening up a new series
-----------------------

Updating distro-info-data and building the images are not blocked on test results
being copied forward (``seed-new-release``) or devel results existing
(``download-all-results``) i.e. do them while waiting for those.

* Clean up old ppa containers by going to the bastion and running `load_creds openstack; cd ~/autopkgtest-cloud/autopkgtest-cloud/tools; ./cleanup-ppa-containers`
* Download the latest ``autopkgtest.db`` from the website/unit to the home
  directory on wendigo
* Run ``autopkgtest-cloud/tools/seed-new-release <old_release> <new_release> autopkgtest.db``
  on wendigo. This copies some of the old release results from swift into a new
  container for the new release.  It does not modify the ``autopkgtest.db`` file.
* Make sure an updated distro-info-data with the new series is available and
  install it on all worker, web, and haproxy nodes. (If not yet available,
  temporarily hack the new series into the ``/usr/share/distro-info/ubuntu.csv``
  on them.)
* Update the ``service-bundle`` to include the release in ``releases`` and
  deploy it by using ``mojo run``. Run ``systemctl start
  download-all-results.service`` (on the instances providing autopkgtest-web)
  to download the results from swift to the db.
  TODO: This should be done automatically by adding the release.
* Build new lxd images on the lxd-armhf leader (see :ref:`Creating new LXD
  images before official ones are available`).
* Build cloud images::

 sudo systemctl start build-adt-image@<release>-<cloud>-<arch>.service ...

* Notify the release team to remove cowboy disablement of proposed-migration,
  and manually run ``run-proposed-migration`` as ``ubuntu-archive@snakefruit``
  to do a test run of proposed-migration.
* Submit a test job for all arches via ``request.cgi`` or ``run-autopkgtest`` on a
  autopkgtest-cloud-worker (``gzip`` is a good candidate as it is fast e.g.
  ``run-autopkgtest --series <new_release> --arch amd64 --trigger gzip/<version>
  gzip``).
* Check `/running <https://autopkgtest.ubuntu.com/running/>`_ lists the new
  release, and check some package pages too.


Removing an End of Life series
------------------------------

Before proceeding with the steps below, please make sure that the series is
properly removed from ``mojo/service-bundle``, and that this change was applied
successfully to all workers.


Removing the tests results, logs, and images from swift and the datacenters
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

There is a script for that. On the bastion, in the *proposed-migration*
environment, from the ``autopkgtest-cloud`` repository, just run the following
and ensure it doesn't run into trouble:

``./dev-tools/clean_eol.sh mantic``

Removing the results from the web unit database
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

You’ll first want to stop the apache2 service so that browsing results will not
fail while the database is being modified. Then there are two jobs which use
the autopkgtest.db which will also need disabling. The ``sqlite-writer``
service is constantly using the ``~/autopkgtest.db`` file and will need to be
stopped. The ``publish-db`` service which updates ``~/public/autopkgtest.db``
is run minutely and will need to be disabled with ``systemctl disable publish-db``.
Please re-enable it once you're finished. *NOTE* it is not enough to simply
``systemctl stop`` the service since it gets restarted by a timer,
so it must be disabled.

Once those steps are done then the rows can be deleted from the database.

* ``sqlite3 -header -column autopkgtest.db "DELETE FROM current_version WHERE release='impish';"``
* ``sqlite3 -header -column autopkgtest.db "DELETE FROM result WHERE result.test_id IN (SELECT test.id FROM test WHERE test.release='impish');"``
* ``sqlite3 -header -column autopkgtest.db "DELETE FROM test WHERE test.release='impish';"``
* ``sqlite3 -header -column autopkgtest.db "vacuum;"``

Creating a new API key
----------------------

API keys exist on the web workers with the following format:

.. code-block:: bash

  {"username": "api-key", "username": "api-key"}

And so on and so forth. The ``username`` should be the launchpad username of the individual who is
requesting an API key. This is not strict, however, and could potentially be anything, as long as the user
attaches their API key using the correct name as provided by the Ubuntu Release Management Team. It just makes
the most sense to just use launchpad usernames.

The ``username`` can also potentially be a name to refer to a group of individuals sharing the same key, or a bot.

The convention to create API keys has thus far been the following, utilising uuid4:

.. code-block:: bash

  python3 -c 'import uuid; print(uuid.uuid4())'

We use python's uuid4 from the uuid library because:

- uuid4 is generally accepted as having the strongest guarantee of uniqueness and security out of all the uuid methods.
- uuid4 isn't guaranteed in the documentation to be cryptographically secure, however, it utilises urandom (https://docs.python.org/3/library/os.html#os.urandom), which is declared as suitable for cryptographic use.

DO NOT use a uuid4 function from another source - the python implementation is guaranteed to use a
cryptographically secure random string generator, so for ``autopkgtest-cloud`` API tokens just use
what's detailed above. Other implementations may waver from this.

Once the ``uuid`` for the api key has been created, add it to:

.. code-block:: bash

  /home/$STAGE-proposed-migration-environment/.local/share/mojo/LOCAL/mojo-$STAGE-proposed-migration/production/external-web-requests-api-keys.json

Where ``$STAGE`` is either ``prod`` or ``stg``.

After this, do a ``mojo run`` to deploy the altered file.

Alternatively, if a ``mojo run`` is for some reason, undesirable at the time, one can also directly add the new api key to the following file on the web units:

``/home/ubuntu/external-web-requests-api-keys.json``

The api keys are loaded for each request, so there's no need to restart ``apache2``.


Using API Keys
--------------

Requests can be requested by using an API key instead of authenticating using SSO.
To do so, attach a cookie to whatever script is making the test request, with the name
"X-Api-Key". The value should look like this:

``user:api-key``

Where the user and api-key fields are provided by the Ubuntu Release Management team.


Integration with GitHub and GitLab pull/merge requests
------------------------------------------------------

autopkgtest-cloud can be used as a GitHub or GitLab web hook for triggering
tests on PR/MR creation/changes.

Preparing the test
^^^^^^^^^^^^^^^^^^

You need to have an autopkgtest for your project that is in some git branch.
This can be in the actual GitHub project repo, but it's also possible and
plausible to reuse the existing autopkgtest in the Ubuntu packaging git and
just adjusting this a little to work for upstream PR tests. For example, you
might want to disable ``dh_install --fail-missing`` or strict
``dpkg-gensymbols`` checking when testing an upstream PR so that you don't
always need to adjust the packaging for these. This can be controlled through
environment variables which get defined in the GitHub web hook and passed to
your test. autopkgtest-cloud itself always provides ``$UPSTREAM_PULL_REQUEST``
with the PR number.

If the tests live in the actual GitHub repo, this is all that is needed. If the
tests live in the Debian/Ubuntu packaging repo, then your downstream
``debian/rules`` must ensure that, before it starts the package build, it
replaces the downstream code from its own checkout with an upstream checkout of
the pull request (and also drop all local patches). Look at `systemd's debian/rules <https://salsa.debian.org/systemd-team/systemd/-/blob/debian/master/debian/rules>`_
for an example, search for ``TEST_UPSTREAM``.

However you want to structure your test, ensure that it works locally with a command like

.. code-block:: bash

 autopkgtest --apt-upgrade https://coolcode.projects.org/foo.git \
    --env UPSTREAM_PULL_REQUEST=1234 --env TEST_UPSTREAM=1 -- \
    qemu autopkgtest-xenial-amd64.img

Web hook setup
^^^^^^^^^^^^^^

The GitHub project admin and a maintainer of the autopkgtest infrastructure need
to exchange a webhook password for triggering tests and an auth token for
sending status notifications back to GitHub.

On the GitHub project side:

1. Go to the project's Settings → Webhooks → Add webhook
2. The payload URL is a call to `request.cgi <https://git.launchpad.net/autopkgtest-cloud/tree/webcontrol/request.cgi>`_
   with the desired parameters:

   * ``release`` and ``arch`` determine the Ubuntu image in which you want to
     run the test.
   * ``build-git`` is the git clone URL of the repo that provides the
     autopkgtest (``debian/tests/``). If it's a Debian/Ubuntu packaging repo, that
     must check out the corresponding upstream code from the PR by itself (look
     at `systemd's debian/rules <https://salsa.debian.org/systemd-team/systemd/-/blob/debian/master/debian/rules>`_
     for an example, search for ``TEST_UPSTREAM``). If the GitHub project to be
     tested contains the autopkgtest by itself, then don't specify this parameter
     at all; it will be dynamically generated as ``clone_url#refs/pull/<PR
     number>/head``.
   * ``package`` is merely an identifier for the project name/test which will be
     used for the results in swift. It is ''not'' related to Ubuntu package
     names at all, as the test will come from a git branch. Use the project
     name, possibly with some suffix like ``-main`` if you have several
     different kinds of tests.
   * ``ppa`` specifies a ``launchpaduser/ppaname``. This must always be present
     so that the results don't land in the Ubuntu results Swift containers. The
     PPA is being added during the test run; it may be empty, but it is commonly
     used to provide some package backports when running tests on older
     releases. /!\ The PPA must publish indexes for the target release, so you
     must have copied/published at least one package to that series (it is okay
     to delete it again afterwards, Launchpad will keep the indexes for that
     series).
   * ``env`` can specify one or multiple (separated with ``;``) environment
     variables which are passed to the test. You can use that to speed up builds
     (``CFLAGS=-O0``) or change the behaviour of your tests
     (``TEST_UPSTREAM=1``).


Note that the entire payload URL must be properly escaped as GitHub is very
picky about it. Example:

.. code-block::

 https://autopkgtest.ubuntu.com/request.cgi?release=xenial&arch=amd64&build-git=https%3A%2F%2Fgit.launchpad.net%2F~pitti%2F%2Bgit%2Fsystemd-debian&env=CFLAGS%3D-O0%3BDEB_BUILD_PROFILES%3Dnoudeb%3BTEST_UPSTREAM%3D1&package=systemd-upstream&ppa=pitti%2Fsystemd-semaphore

3. Generate a random password (e. g. ``pwgen -N 1 15``) for the "Secret".
4. For "Content type", select "application/json".
5. In the "Which events" section, select "individual events" and in there "Push" and "Pull request".
6. Leave the other settings at their defaults and press "Add webhook".
7. Create the access token for test status updates:

   * Go to the user mugshot at the top right → Settings → Developer settings →
     Personal access tokens → Generate new token
   * Use something like "get status updates for PR test requests from
     autopkgtest.ubuntu.com" as the description
   * Select ''only'' ``repo:status`` as scope.
   * Press "Generate", and note down the token value; you will never be able to
     retrieve it again from that page later.

On the autopkgtest side on the controller:

1. In the secrets directory, add the new project name and webhook password to
   ``github-secrets.json``. Make *double sure* to not break JSON formatting (e.
   g. trailing commas).
2. Add the new developer name and token for the chosen ``package`` from above
   (i.e. project name) to ``github-status-credentials.txt``.
3. Run ``mojo run`` to deploy the updated configuration, as normal.
4. Verify that the files got updated on the servers:

.. code-block:: bash

  juju run --application autopkgtest-web 'cat ~ubuntu/github-status-credentials.txt; cat ~ubuntu/github-secrets.json'

You can debug what's going on with ``tail -f
/var/log/apache2/{access,error}.log`` on the web machines.

Test the setup with some dummy PR that changes some README or similar. You can
then re-trigger new tests by force-pushing to the branch. Once everything works,
you can add more web hooks with different test parameters to e. g. trigger tests
on multiple architectures or multiple Ubuntu releases.


Queue Cleanup
-------------

Regular queue cleanup can become necessary when the queues are quite large.
The best way to go about doing this is by first downloading the queues.json:

``curl https://autopkgtest.ubuntu.com/queues.json | jq '.huge.noble.amd64' | jq -c '.[]' -r | grep '"triggers"' | jq -c '.triggers' --sort-keys | sort | uniq -c | sed 's/+/\\+/' | less``

And what this does, is filter the queue, and returns a list of all the unique
triggering packages currently queued, with a count of how many queue items per
package, and pipes it to less. You can then check this output, and look for
obsoleted package versions, e.g.:

.. code-block::

  117 [\"dpkg/1.22.6ubuntu2\"],
  117 [\"dpkg/1.22.6ubuntu4\"],

Here, you can see that ``dpkg/1.22.6ubuntu2`` has been obsoleted by ``dpkg/1.22.6ubuntu4``.
So, the workflow now would be to remove said package from the queue::

.. code-block:: bash

  ./filter-amqp -v debci-huge-noble-$arch "dpkg/1.22.6ubuntu2\b"

However, this gets tedious with lots of obsoleted packages in the queue. So an approach,
when you have lots of obsoleted packages, would be like so:

.. code-block:: bash

  packages="package1/trigger-2.3.0 package2/trigger-2.4.3..." # obviously with more packages
  for pkg in $packages; do for arch in amd64 arm64 s390x ppc64el armhf i386; do ./filter-amqp -v debci-huge-noble-$arch "$pkg\b"; done; done

This way you can remove all the packages in one command on every architecture.

Resizing /tmp partitions
------------------------

When running an instance of autopkgtest-cloud, you may find that the `/tmp` partitions for the
autopkgtest-cloud-worker units can get quite full.

This can happen when you have very long running tests, which have a large `tests-tree` folder,
which is produced by autopkgtest itself. These long running tests can disproportionately use up
the disk space on `/tmp`, and this can end up introducing a "meta-quota", where your cloud
resources aren't restricted, but you hit bottlenecks due to the `/tmp` partition running out of
space. This typically will surface as `No space left on device` errors in the test logs,
covering a variety of tests.

In an occasion like this, consider increasing the size of the `/tmp` partitions.

You can somewhat estimate the partition size you'll need, like so:
- Take the total number of workers on a given unit - `juju config autopkgtest-cloud-worker n-workers` on the bastion, and add up the numbers
- Multiply this number by the average `/tmp` usage per test - you can do this by `stat`-ing the directories under `/tmp`
- This should give you an estimate of how large you require the `/tmp` volume to be.

For instance, at one point there were packages in production taking between
1.5 and 4.5GB - this was averaged to roughly 2GB, and multiplying by the number
of workers at the time gave:
`(110+22+22+22+29+22+22+28 = 277) * 2 = 554`

Indicating 554GB would be required. At the time, there was only 200GB.

The situation was remedied by increasing the `/tmp` volume size to 350GB, and
decreasing the number of workers. This stopped the amount of `No space left on device`
errors occurring in the logs.

The steps to increase the `/tmp` volume size are detailed below.

Before doing any of the steps detailed in this section, it's important to make sure no tests
are currently running on the cloud worker with the partition you want to resize.

.. code-block:: bash

  # on the worker machine with the volume you intend to resize
  chmod -x autopkgtest-cloud/worker/worker
  sudo systemctl stop autopkgtest.target # ensure that you WAIT for all running jobs to finish, i.e. for the stop command to exit
  while true; do ps aux | grep runner; sleep 3; clear; done # wait until there are no runner processes

First check that this specific version of openstack is available via:

.. code-block:: bash

  openstack --os-volume-api-version 3.42 volume list

The command should not fail.

To resize a volume:

.. code-block:: bash

  # get the 'openstack' volume id
  juju storage --volume # the volume id is in the "Provider ID" column
  # from the above command, get the id, and set it to a variable: VOLUME_ID
  openstack --os-volume-api-version 3.42 volume set ${VOLUME_ID} --size ${NEW_SIZE}
  # this will begin the process of resizing the volume
  # whilst this is happening, consider running this:
  while true; do openstack volume show ${VOLUME_ID}; sleep 5; clear; done
  # If the volume in question has been retyped (__DEFAULT__ <-> Ceph_NVMe), run the following (not necessary for volumes that haven't been retyped):
  nova reboot ${server_name}
  # where $server_name is the name of the server associated with the volume
  # to check this:
  juju storage # make note of the juju unit name associated with the storage you've resized
  # then
  openstack server list
  # and get the server name of the server running the unit mentioned in juju storage
  # after rebooting, run the following ON THE SERVER you've rebooted
  lsblk # check that the disk size has increased
  sudo growpart /dev/vdb 1
  sudo resize2fs /dev/vdb1
  lsblk # check that the disk size and partition sizes match

There are no conclusions as to why the reboot is required if the volume has already
been retyped. None of the typical methods for rescanning disks work, in this case.

When the volume hasn't been retyped prior, it is immediately acknowledged by the
openstack server. Keep this in mind if you're using the __DEFAULT__ volume type
(see `openstack volume show ${VOLUME_ID}` to check).


Killing running tests
---------------------

In order to kill a currently running test, grab the test uuid. This can be seen in
`running.json` or on the `/running` page.

`ssh` to a worker unit, and run:

.. code-block:: bash

  ps aux | grep runner | grep $uuid
  # grab the PID from the process - this approach will also remove the test request from the queue
  kill -9 $pid
  # if you want to stop or restart the test but preserve the test request, run the following to get the service name:
  service_name=$(ps aux | grep runner | grep $uuid | grep -oP '(?<=--security-groups).*?(?=--name)' | cut -d'@' -f2 | sed -e "s/.secgroup//g")
  # and then simply stop or restart as you please:
  sudo systemctl restart/stop autopkgtest@"${service_name}".service

This will kill the autopkgtest process, and then the worker will `ack` the test request
message, causing the test to not be re-queued, and then the worker will also ensure
that the openstack server used for the test is deleted.


Access the RabbitMQ web UI
--------------------------

RabbitMQ by default provides a web UI that is very convenient to look at various
metrics gathered by the process. This can help diagnose issues such as queues
not being cleaned correctly, and growing indefinitely.

Since it's not exposed on a publicly reached port in any way, you need to setup
SSH tunnelling to access it.

.. code-block:: bash

  # First setup a tunnel to the bastion (disable `byobu` to avoid indefinitely running tunnels)
  LC_BYOBU=0 ssh ubuntu-qa-bastion-ps5.internal -L 15672:localhost:15672
  # Then, from the right environment on the bastion, setup a second tunnel to the RabbitMQ unit
  ssh -i ~/.ssh/id_ed25519 -L 15672:localhost:15672 ubuntu@10.136.6.239

Now point your local browser to http://localhost:15672 and you should have
access to the UI. Grab the credentials by asking a team member.

**NOTE**: Don't forget to close the tunnels when you're done, especially if you
usually have a `tmux`/`byobu` session running wrapping the second tunnel!


Access a testbed spice console
------------------------------

The `spice <https://spice-space.org/>`_ console is the "physical" console
attached to an OpenStack VM, that can be helpful to debug issues where the
network is broken and you don't have access to the machine through SSH.

.. code-block:: bash

  # First SSH to the bastion with a SOCKS proxy port open
  ssh ubuntu-qa-bastion-ps5.internal -D 1080

Then setup your browser to go through that proxy (plenty of online doc for this).

.. code-block:: bash

  # Now, on the bastion in the right environment:
  # Source the credentials for the cloud your VM is in
  $ source path/to/cloud.rc
  # And finally print the spice console address
  $ openstack console url show --spice <instance_id>
  +----------+------------------------------------------------------------------------------------------------+
  | Field    | Value                                                                                          |
  +----------+------------------------------------------------------------------------------------------------+
  | protocol | spice                                                                                          |
  | type     | spice-html5                                                                                    |
  | url      | https://nova.ps5.canonical.com:6082/spice_auto.html?token=12345678-1234-4012-7890-0123456789ab |
  +----------+------------------------------------------------------------------------------------------------+

**NOTE**: the access is only authorized from the corresponding bastion in each
cloud. That means that for a VM in PS5, you need to setup your SOCKS proxy to a
PS5 bastion, and for a VM in PS6, you need a proxy to a PS6 bastion.


Blackhole harmful IP ranges
---------------------------

As with everything exposed to the Internet, the infra might be subject to
probing by some bots.
They can raise the load pretty high, leading to some DoS, but this is easily
prevented by looking at the HAProxy logs and blackholing the harmful IP address
range.

Example of harmful requests:

.. code-block::

  GET /packages/a/ableton-link/oracular/armhf/portal/attachment_getAttList.action?bean.RecId=1')+AND+EXTRACTVALUE(534543,CONCAT(0x5c,md5(999999999),0x5c))+AND+('n72Yk'='n72Yk&bean.TabName=1
  GET /index.php?lang=../../../../../../../../usr/local/lib/php/pearcmd&+config-create+/&/<?echo(md5(#22hi#22));?>+/tmp/index1.php
  GET /<IMG%20SRC=#22javascript:alert(cross_site_scripting.nasl);#22>.jsp
  GET /packages/a/abseil/oracular/amd64/seeyon/webmail.do?method=doDownloadAtt&filename=index.jsp&filePath=../conf/datasourceCtp.properties

The situation can be handled quickly with the following:

.. code-block:: bash

  # On the HAProxy unit
  cd /var/log
  # Change `CONCAT` here by other pattern, like `\.php` or `\.jsp`
  zgrep 'CONCAT' haproxy.log*.gz > /tmp/harmful.log
  # Manually inspect the harmful logs if you want
  less /tmp/harmful.log
  # Get the list of IP addresses sorted with the most harmful at the bottom
  grep -o '[0-9]*\.[0-9]*\.[0-9]*\.[0-9]*' /tmp/harmful.log | sort | uniq -c  | sort -n
  # Get the network range of that IP, very useful if you see a lot of similar but different IPs in the list
  # Run this on another machine, don't install the tools on the unit
  whois <ip address> | grep NetRange
  ipcalc-ng -d <first IP>-<last IP>
  # Back on the HAProxy unit
  # Blackhole the whole range
  sudo ip route add blackhole 123.123.123.123/12
  # Show the currently blackholed ranges
  sudo ip route show type blackhole
