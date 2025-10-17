#!/usr/bin/env python3
"""Run browse app in local debug mode for testing."""

import argparse
import importlib
from pathlib import Path

try:
    from flask_debugtoolbar import DebugToolbarExtension

    activate_debugtoolbar = True
except ImportError:
    activate_debugtoolbar = False

from helpers import tests, utils

# import browse.cgi
browse_path = str(Path(__file__).parent / "browse.cgi")
loader = importlib.machinery.SourceFileLoader("browse", browse_path)
spec = importlib.util.spec_from_loader("browse", loader)
browse = importlib.util.module_from_spec(spec)
loader.exec_module(browse)


def parse_args():
    parser = argparse.ArgumentParser(description="Run a local browse.cgi")
    parser.add_argument(
        "--database",
        dest="database",
        type=str,
        help="Provide a specific 'autopkgtest.db' file",
    )
    parser.add_argument(
        "--running",
        dest="running",
        type=str,
        help="Provide a specific 'running.json' file",
    )
    parser.add_argument(
        "--queue",
        dest="queue",
        type=str,
        help="Provide a specific 'queued.json' file",
    )
    parser.add_argument(
        "--data-dir",
        dest="data_dir",
        type=str,
        help="This allows specifying a single folder containing 'running.json', "
        "'queued.json', and 'autopkgtest.db'. This is incompatible with other "
        "file-specific options.",
    )

    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    browse.init_config()
    config = utils.get_autopkgtest_cloud_conf()

    if args.data_dir:
        browse.CONFIG["amqp_queue_cache"] = Path(args.data_dir) / "queued.json"
        browse.CONFIG["running_cache"] = Path(args.data_dir) / "running.json"
        browse.CONFIG["database"] = args.data_dir + "/autopkgtest.db"
    else:
        if args.database:
            browse.CONFIG["database"] = args.database
        else:
            # For convenience, the development Flask app uses database instead
            # of database_ro.
            # This is different from production deployment, where `publish-db`
            # produces database_ro, that browse.cgi uses.
            browse.CONFIG["database"] = config["web"]["database"]

        if args.queue:
            browse.CONFIG["amqp_queue_cache"] = Path(args.queue)
        else:
            tests.populate_dummy_amqp_cache(browse.CONFIG["amqp_queue_cache"])

        if args.running:
            browse.CONFIG["running_cache"] = Path(args.running)
        else:
            tests.populate_dummy_running_cache(browse.CONFIG["running_cache"])

    utils.init_db(browse.CONFIG["database"])
    browse.connect_db("file:%s?mode=ro" % browse.CONFIG["database"])

    if activate_debugtoolbar:
        browse.app.debug = True
        browse.app.config["SECRET_KEY"] = "AutopkgtestCloudSecretK3y"
        toolbar = DebugToolbarExtension(browse.app)
    browse.app.run(host="0.0.0.0", debug=True)
