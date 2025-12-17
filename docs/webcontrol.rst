autopkgtest-web
===============

``autopkgtest-website-operator`` is the part of the service which runs the web UI. The
canonical instance of this is running on `<https://autopkgtest.ubuntu.com>`_.
It is used for browsing results, seeing which tests are currently in
progress, and as a webhook endpoint for configured GitHub projects to
requests tests.

The web frontend is stateless, in that the service can be destroyed and
restored, as long as Swift continues to contain all of the results to be
presented.

It's made up of the following parts:

* ``autopkgtest-db-writer`` is a service which listens to the ``testcomplete`` fanout
  exchange and writes the results of finished test runs into ``autopkgtest.db``. This
  service will also initialize the database if the required tables are not present.
  It runs continuously and restarts if it fails.
* ``autopkgtest-db-publisher`` copies the database to a public directory, to be made available
  for users to download without incurring a race condition mid-connection.
  It runs every minute.
* ``autopkgtest-running-collector`` listens to the ``teststatus`` fanout exchange to get
  metadata such as run duration and log heads and tails of currently running tests published by
  each worker. This information is used to populate the ``/running`` page.
  It runs continuously.
* ``autopkgtest-queues-collector`` will periodically iterate over all valid queues and make
  lists of all queue items. This is used to populate the queue information on the ``/running``
  page.
* ``browse.cgi`` is a simple Flask app that renders results and statistics from
  the above database, currently running tests from ``running.json``, and
  queued tests from ``queued.json``.
* ``request.cgi`` provides a Launchpad SSO authenticated CGI API for
  (re)triggering test requests for Ubuntu and github. Pages like britney's
  ``excuses.html`` link to it for retrying regressions.
