# autopkgtest-cloud web frontend

## Developing locally

Most of the scripts in this folder can be run locally for easier development.

The first thing to do is to provide an `autopkgtest-cloud.conf` file.
In this current folder:
`cp autopkgtest-cloud.conf.example autopkgtest-cloud.conf`

Install the main dependencies (Others are usually less important. Have a look at
the charm definition for an exhaustive list.):
`sudo apt install python3-pika python3-flask python3-distro-info libjs-jquery libjs-bootstrap`

Then you can start each script individually, without argument.
Here is a quick non exhaustive list of the main ones:

* sqlite-writer:
  probably one of the most important one: it's the only script that will
  actually write to the `autopkgtest.db` database (because of lack of concurrent
  write support in sqlite).
* download-results:
  will listen to finished results from the worker, and will push DB write
  through AMQP to sqlite-writer.
* amqp-status-collector:
  this is the script monitoring the ongoing jobs processed by the workers, and
  dumping that information into `running.json`, mostly displayed on the
  `/running` page.
* cache-amqp:
  this script is basically dumping the AMQP test requests queues into a JSON,
  used throughout the web UI.
* publish-db:
  this is taking the rw database, and copying it to the ro one, actually used
  by many scripts in production, and adding a bit more information like the
  `current_version` table.

Please note that the default configuration is compatible with a local run of the
`worker` part, meaning you can have the whole stack running on your laptop.

## Notes on developing browse.cgi locally

*Optional*: `python3 -m pip install --user --break-system-packages flask-debugtoolbar`
This will automatically activate the Flask DebugToolbar that brings valuable
information for developers.

Simply run `./browse-test-py`, it will launch the flask application locally
with some mocked data.
As the import of `browse.cgi` is done through `importlib`, changes in that file
will not be reloaded automatically, so you'll still need to restart the app
manually.


## Override dev data with some specific files

`browse-test.py` support various options that allow you to override the
procedural data with real files that you may have grabbed somewhere, or
specially crafted. See the `--help` for an up-to-date and exhaustive list of
options.


## Debug issue with production dataset

From this current folder, you can do something like this:
```
mkdir -p ../../prod-data
pushd ../../prod-data
wget autopkgtest.ubuntu.com/queued.json
wget autopkgtest.ubuntu.com/static/running.json
wget autopkgtest.ubuntu.com/static/autopkgtest.db
popd
./browse-test.py --data-dir ../../prod-data
```

Note: Having everything in a folder that is not in the charm folder avoids
having issue next time you want to build the charm.


## Analyzing production log files in mass to gather stats

There is a self-documented Jupyter notebook in this folder, called
`stats.ipynb`. You can play with this with the following:
```
sudo apt install jupyter-notebook
# From the current folder:
jupyter-notebook
```
