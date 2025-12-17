Admin
=====

Some common tasks admins might need to perform.

Testing packages in VMs
-----------------------

Packages can be tested in VMs instead of containers by adding the appropriate lines to
the ``vm_packages`` list in
`<https://code.launchpad.net/~ubuntu-release/autopkgtest-cloud/+git/autopkgtest-package-configs>`_.
See `its README
<https://git.launchpad.net/~ubuntu-release/autopkgtest-cloud/+git/autopkgtest-package-configs/tree/README.md>`
for documentation on the syntax of the files.

Changes pushed to the repo will be used automatically by the workers shortly
after pushing. They are pulled every minute, so wait one minute before
retrying.

Give a package more time or more resources
------------------------------------------

Tests can be run with more time or on bigger instances by adding the appropriate lines to
the ``big_packages`` list in ``autopkgtest-package-configs``, as specified above.

Re-running tests
----------------

Britney's `excuses.html
<https://people.canonical.com/~ubuntu-archive/proposed-migration/update_excuses.html>`_
has retry symbols ♻ after "Regression"s, which submit a test request via
`autopkgtest-cloud's
webcontrol <https://git.launchpad.net/autopkgtest-cloud/tree/webcontrol>`_.
Requesting individual manual runs can also be done with the ``run-autopkgtest`` script on the ``website`` unit.
Then you can run ``run-autopkgtest --help`` to see the usage. e. g.::

  # specific architecture
  run-autopkgtest -s resolute -a armhf --trigger glib2.0/2.46.1-2 libpng udisks2
  # all configured britney architectures (current default: i386, amd64, amd64v3, armhf, arm64, ppc64el, s390x, riscv64)
  run-autopkgtest -s resolute --trigger glibc/2.21-0ubuntu4 libpng udisks2

Note that you must always submit a correct "trigger", i. e. the
package/version on excuses.html that caused this test to run. This is
necessary so that britney can correctly map results to requests and as we
only use packages from -proposed for the trigger (via apt pinning). This apt
pinning can be disabled with the ``--all-proposed`` option. If
``--all-proposed`` is too broad - it can make packages migrate when they need
things from proposed - you can alternatively just specify ``--trigger``
multiple times, for all packages in -proposed that need to be tested and
landed together::

 run-autopkgtest -s resolute --trigger php-foo/1-1 --trigger php-foo-helpers/2-2 php-foo

``lp:ubuntu-archive-tools`` contains a script
``retry-autopkgtest-regressions`` which will build a series of request.cgi
URLs for re-running all current regressions. It has options for picking a
different series, running for a PPA, or for a different test state (e.
g. ``--state=RUNNING`` is useful to requeue lost test requests). You can also
limit the age range. See ``--help`` for details and how to run it
efficiently.

Autopkgtest controller access
-----------------------------

There are dedicated environments for the orchestrator charms (website, janitor, dispatcher) and each arch
on PS7::
  ssh <lpuser>@ubuntu-engineering-bastion-ps7.canonical.is
  pe autopkgtest

Rolling out new worker or web code and changing configuration
-------------------------------------------------------------

See :ref:`Update the code`.

Most configuration is exposed via charm settings. Edit the ``terraform``
files, pull it on the desired environment and run ``terraform plan -var local_run=true`` and ``terraform apply -var local_run=true``.

NOTE: ``-var local_run=true`` is always required when working on the PS7 environments.

The charms and remotes should then reload themselves if necessary.

If this doesn't happen for any reason, you can::

  juju refresh <unit>

where ``<unit>`` is the charm shown in ``juju status``.

Updating autopkgtest
---------------------------------
The dispatcher and janitor applications have
checkouts of the Ubuntu Release team's autopkgtest branches.
These branches can be updated (which will remove any local
changes) on a unit via the following::

  juju ssh <unit>
  cd autopkgtest; git pull


Creating new LXD images before official ones are available
----------------------------------------------------------

This should happen automatically upon adding a new release to the ``janitor``.


Watching all logs
^^^^^^^^^^^^^^^^^

On the cloud/lxd controller, run::

  journalctl -u autopkgtest-worker@*.service

Watching one cloud/arch
^^^^^^^^^^^^^^^^^^^^^^^

.. code-block:: bash

 journalctl -u autopkgtest-worker@remote-<arch>-*.service

Opening up a new series
-----------------------

TODO: update archive opening steps


Removing an End of Life series
------------------------------

Before proceeding with the steps below, please make sure that the series is
properly removed from the terraform plan of the orchestration environment, and
that the change was applied to all three of the website, dispatcher, and janitor.


Removing the tests results, logs, and images from swift and the datacenters
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

TODO: port over the ``clean_eol.sh`` script in some way.

Removing the results from the web unit database
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

You’ll first want to stop the apache2 service so that browsing results will not
fail while the database is being modified. Then there are two jobs which use
the autopkgtest.db which will also need disabling. The ``autopkgtest-db-writer``
service is constantly using the ``/srv/autopkgtest/data/autopkgtest.db`` file and will need to be
stopped. The ``autopkgtest-db-publisher`` service which updates ``/srv/autopkgtest/data/public/autopkgtest.db``
is run minutely and will need to be disabled with ``systemctl disable autopkgtest-db-{writer,publisher}``.
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

TODO: make sure API keys are ported over, or replace with LP calls

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

TODO: new deployment instructions for API keys


Using API Keys
--------------

Requests can be requested by using an API key instead of authenticating using SSO.
To do so, attach a cookie to whatever script is making the test request, with the name
"X-Api-Key". The value should look like this:

``user:api-key``

Where the user and api-key fields are provided by the Ubuntu Release Management team.


Integration with GitHub and GitLab pull/merge requests
------------------------------------------------------

GitHub or GitLab web hooks can be used for triggering
tests on PR/MR creation/changes.

TODO: port over upstream test functionality

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
