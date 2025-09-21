Architecture
============

Overview
--------

.. graphviz::
    :caption: The architecture of the autopkgtest-cloud environment.

    digraph autopkgtestcloud {
        subgraph cluster_autopkgtestcloud {
            subgraph cluster_prodstack {
                node [shape=Mrecord]
                web [label="{autopkgtest-web web UI|* results browser for developers\n* shows which tests are running\n* receives requests to run tests from developers or GitHub PRs\n* can be scaled out}"]
                rabbitmq [label="{RabbitMQ AMQP server|debci-$release-$arch\ndebci-ppa-$release-$arch\ndebci-upstream-$release-$arch\ndebci-huge-$release-$arch}"]
                cloudworker [label="{autopkgtest-cloud-worker|cloud-worker charm with ssh+nova config\ncall autopkgtest with OpenStack runner}"]
                lxdworker [label="{autopkgtest-lxd-worker|cloud-worker charm with lxd config\ncall autopkgtest with lxd runner}"]
                swift [label="{OpenStack Swift object store|public test results\nautopkgtest-$release\nautopkgtest-$release-$lpusername-$ppaname}"]
                swiftprivate [label="{OpenStack Swift object store|* private PPA test results\n* embargoed CVEs}"]
                haproxyweb [label="{HAproxy|* provides SSL termination\n* https://autopkgtest.ubuntu.com}"]
                runner_x86 [label="{'PS5' OpenStack Instances|dynamically allocated test runners\namd64 (i386 via cross-arch)}"]

                "cloudworker" -> "runner_x86"
                "cloudworker" -> "rabbitmq" [label="pop test requests" style=dashed]
                "lxdworker" -> "rabbitmq" [label="pop test requests" style=dashed]
                "web" -> "rabbitmq" [label="request.cgi\npush test request" style=dashed]
                "cloudworker" -> "swift"
                "cloudworker" -> "swiftprivate"
                "lxdworker" -> "swift"
                "lxdworker" -> "swiftprivate"
                "swift" -> "web" [label="download new results\ninto database"]
                "web" -> "swift" [label="link to logs and artifacts" style="dotted"]
                "haproxyweb" -> "web" [label="one proxy, many backends"]

                label="'prodstack' OpenStack cloud"
                color=blue
            }
            subgraph cluster_scalingstack {
                node [shape=Mrecord]
                static [label="{statically provisioned lxd runners|arm64 host with armhf containers}"]
                runner_other [label="{dynamically provisioned runners|arm64, ppc64el, s390x}"]

                "lxdworker" -> "static"
                "cloudworker" -> "runner_other"


                label="'scalingstack' OpenStack cloud"
            }
            color=pink
            label="autopkgtest-cloud devops environment"
        }
        subgraph cluster_external {
            node [shape=Mrecord]
            archive [label="{Ubuntu developers|packages in -proposed}"]
            britney [label="{britney|* gates pkgs from -proposed to release pocket\n* completed builds\n* installability\n* non-regressing autopkgtests\n* generates update_excuses.html/update_output.txt}"]

            "archive" -> "britney" [label="Ubuntu developers\nupload packages"]
            "britney" -> "rabbitmq" [label="push tests requests" style=dashed]

            archivesecure [label="{Ubuntu security team|packages in PPAs}"]
            britneysecure [label="{security britney}"]
            "archivesecure" -> "britneysecure" [label="Ubuntu security team\nupload packages"]
            "britneysecure" -> "rabbitmq" [label="push tests requests" style=dashed]

            debiansync [label="{Sync from Debian}"]
            "debiansync" -> "britney"

            ubuntudeveloper [label="{Any developer|upload to PPA}"]
            "ubuntudeveloper" -> "haproxyweb" [label="trigger test on PPA\nGET /request.cgi"]

            color=green
            label="external inputs"
        }

    }


The basic architecture is that a client (e.g. proposed-migration) submits
test requests to an AMQP instance. Cloud workers then receive these messages
from AMQP and dispatch the tests to workers to be run. They upload the
results to Swift and then proposed-migration and the website fetch them and
act accordingly.

Test request format
-------------------

A particular test request (i. e. a queue message) has the format ``srcpkgname
<parameter JSON>``.

The following parameters are currently supported:

* ``triggers``: List of ``trigsrcpkgname/version`` strings of packages which
  caused ``srcpkgname`` to run (i. e. triggered the ``srcpkgname`` test).
  Ubuntu test requests issued by ``proposed-migration`` must always contain
  this, so that a particular test run for ``srcpkgname`` can be mapped to a
  new version of ``trigsrcpkgname`` in ``-proposed``. In case multiple reverse
  dependencies ``trigsrc1`` and ``trigsrc2`` of ``srcpkgname`` get uploaded to
  ``-proposed`` around the same time, the trigger list can contain multiple
  entries.
* ``ppas``: List of PPA specification strings ``lpuser/ppaname``. When given,
  ask Launchpad for the PPAs' GPG fingerprints and add setup commands to
  install the GPG keys and PPA apt sources. In this case the result is put
  into the container "autopkgtest-release-lpuser-ppaname" for the ''last''
  entry in the list; this is is fine grained enough for easy lifecycle
  management (e. g. remove results for old releases wholesale) and still
  predictable to the caller for polling results.
* ``env``: List of ``VAR=value`` strings. These get passed verbatim to
  ``autopkgtest``'s ``--env`` option. This can be used to influence a test's
  behaviour from a test request.
* ``test-git``: A single URL or ``URL branchname``. The test will be ``git
  clone`` d from that URL (if given, a non-default branch will be checked out)
  and ran from the checkout. This will ''not'' build binary packages from the
  branch and run tests against those, the test dependencies will be taken
  from the archive, or PPA if given. The ``srcpkgname`` will only be used for
  the result path in swift and be irrelevant for the actual test.
* ``build-git``: Like ``test-git``, except that this will first build binary
  packages from the branch and run tests against those.
* ``test-bzr``: A single URL. The test will be checked out with ``bzr`` from
  that URL. Otherwise this has the same behaviour as ``test-git``.
* ``all-proposed``: If this is set to 1, apt pinning to only use the trigger
  package from ``-proposed`` will be disabled, and the test will run against
  all of ``-proposed``. This is sometimes necessary when several packages need
  to land in lockstep but don't declare versioned ``Depends:``/``Breaks:`` to
  each other, but might cause mis-blaming if some other package than the
  trigger got broken in ``-proposed``.
* ``testname``: If given, this gets forwarded to autopkgtest's ``--testname``
  option to run a single test only.
* ``swiftuser``: Usable for private test runs. Name of the Swift user that
  should have read access to the private test run result (to the resulting
  Swift container).
* ``readable-by``: Usable for private test runs. Launchpad username or list of
  usernames that should have read access to the selected private test results
  and logs (i. e. retrievable via the SSO protected private-results).

Examples:

 * A typical request issued by proposed-migration when a new ``glib2.0 2.20-1`` is uploaded and we want to test one of its reverse dependencies ``gedit``:

   ``gedit {"triggers": ["glib2.0/2.20-1"]}``

 * Run the ``systemd`` package tests against the packages in the `systemd CI
   PPA
   <https://launchpad.net/~upstream-systemd-ci/+archive/ubuntu/systemd-ci>`_:

   ``systemd {"ppas": ["upstream-systemd-ci/systemd-ci"]}``

 * Run the ``gedit`` tests under a different env variable:

   ``gedit {"env": ["XDG_SESSION_DESKTOP=xfce"]}``

Private test support
-------------------

The current autopkgtest-cloud infrastructure provides basic support for
running "private tests". A private test is a test which's details and results
are private and visible only to selected, privileged users. Such tests will
not appear on any public test results page and will be listed as
``Running private test`` on the running autopkgtest-web frontend.

A private test run is requested by submitting a regular ADT test request
with at least the ``swiftuser`` additional parameter provided. The result is
then uploaded to a newly created "private-<ADT container name>" Swift
container that is made readable only by the provided identity. An additional
parameter of ``readable-by`` can be supplied to allow selected Launchpad users
from outside of Swift to be able to read the actual test results. This,
however, still requires knowing the test result URL to proceed, similarly to
usual test result fetching, just this time using the special
`<https://autopkgtest.ubuntu.com/private-result/>`_ webfront.
