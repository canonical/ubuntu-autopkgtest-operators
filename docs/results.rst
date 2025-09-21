Test result store
=================

Swift
-----
The swift object store is being used as the central API for storing and
querying results. This ensures that logs are kept safe in a redundant
non-SPOF storage, and we do not keep primary data in any cloud instance. Thus
we can completely re-deploy the whole system (or any part of it can fatally
fail) without losing test results and logs. Swift also provides a `flexible
API for querying particular results
<http://developer.openstack.org/api-ref-objectstorage-v1.html#storage_container_services>`_
so that consumers (like web interfaces for result browsing, report builders,
or proposed-migration) can easily find results based on releases,
architectures, package names, and/or time stamps. For this purpose the
containers are all publicly readable and browsable, so that no credentials
are needed.

Container Names
---------------
Logs and artifacts are stored in one container ``autopkgtest-release`` for
every release, as we want to keep the logs throughout the lifetime of a
release and thus it's easy to remove them after EoLing. Results for PPAs are
stored in the container ``autopkgtest-release-lpuser-ppaname`` (e.g.
``autopkgtest-wily-pitti-systemd``).

Container Layout
----------------
In order to allow efficient querying and polling for new results, the logs
are stored in this (pseudo-)directory structure:

.. code-block::

  /release/architecture/prefix/sourcepkg/YYYYMMDD_HHMMSS@/autopkgtest_output_files

"prefix" is the first letter (or first four letters if it starts with "lib")
of the source package name, as usual for Debian-style archives. Example:
``/trusty/amd64/libp/libpng/20140321_130412@/log.gz``.

The '``@``' character is a convenient separator for using with a container
query's ``delimiter=@`` option: With that you can list all the test runs
without getting the individual files for each run.

The result files are by and large the contents of autopkgtest's
``--output-directory`` plus an extra file exitcode with autopkgtest's exit
code; these files are grouped and tar'ed/compressed:

* ``result.tar`` contains the minimum files/information which clients like
  proposed-migration or web result browsers need to enumerate test runs and
  see their package names/versions/outcome: ``exitcode``,
  ``testpkg-version``, ``duration``, and ``testbed-packages``. All of these
  are very small (typically ~ 10 kB), thus it's fine to download and cache
  this information locally for fast access.
* ``log.gz`` is the compressed log from autopkgtest. Clients don't need to
  download and parse this, but it's the main thing developers look at, so it
  should be directly linkable/accessible. These have a proper MIME type and
  MIME encoding so that they can be viewed inline in a browser.
* ``artifacts.tar.gz`` contains ``testname-{stdout,stderr,packages}`` and any
  test specific additional artifacts. Like the log, these are not necessary
  for machine clients making decisions, but should be linked from the web UI
  and be available to developers.

Due to Swift's "eventual consistency" property, we can't rely on a group of
files (like ``exit-code`` and ``testpkg-version``) to be visible at exactly
the same time for a particular client, so we must store them in
``result.tar`` to achieve atomicity instead of storing them individually.
