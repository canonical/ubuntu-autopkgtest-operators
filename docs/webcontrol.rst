autopkgtest-web
===============

``autopkgtest-web`` is the part of the service which runs the web UI. The
canonical instance of this is running on `<https://autopkgtest.ubuntu.com>`_.
It is used for browsing results, seeing which tests are currently in
progress, and as a webhook endpoint for configured GitHub projects to
requests tests.

The web frontend is stateless, in that the service can be destroyed and
restored, as long as Swift continues to contain all of the results to be
presented.

It's made up of the following parts:

* ``download-results`` is a program which remains running all of the time. It
  connects to the ``complete`` fanout queue to receive notification from the
  controller when jobs complete, and then fetches the results from Swift.
* ``download-all-results`` downloads all new ``results.tar.gz`` files from
  Swift and puts their information into an SQLite database. This script is
  called when a new instance is deployed, to populate the database with
  initial contents, and can also be executed later on (via
  ``download-all-results.service``) in case ``download-results`` fails.
* ```publish-db``` is a service which updates ```~/public/autopkgtest.db``` from ```~/autopkgtest.db```. It runs minutely.
* ``cache-amqp`` is used to cache the contents of the queues, so each hit to
  ``/running`` doesn't need to figure this out from AMQP synchronously.
* ``amqp-status-collector`` listens to the teststatus.fanout AMQP queue and
  updates ``/tmp/running.json`` with the currently running tests and their
  logtails. This is used for the ``/running`` page.
* ``browse.cgi`` is a simple Flask app that renders results and statistics from
  the above database, currently running tests from ``/tmp/running.json``, and
  queued tests from the file written by ``cache-amqp``.
* ``request.cgi`` provides a Launchpad SSO authenticated CGI API for
  (re)triggering test requests for Ubuntu and github. Pages like britney's
  ``excuses.html`` link to it for retrying regressions.
