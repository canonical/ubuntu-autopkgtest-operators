from __future__ import annotations

import argparse
import configparser
import logging
import os
import sys
from pathlib import Path

import pika
import swiftclient

from .adapters import ArtifactWriter, AutopkgtestRunner
from .runner import Worker


def parse_args():
    parser = argparse.ArgumentParser(description="autopkgtest worker")
    parser.add_argument(
        "-c", "--config", type=Path, help="path to worker config file", required=True
    )
    parser.add_argument(
        "-a", "--architecture", type=str, help="architecture to test for", required=True
    )
    parser.add_argument(
        "-r", "--remote", type=str, help="LXD remote to use for testing", required=True
    )
    parser.add_argument(
        "-n", "--name", type=str, help="container name to use", required=True
    )
    parser.add_argument("--debug", action="store_true", help="enable debug logging")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    debug = args.debug
    config = configparser.ConfigParser()
    config.read(args.config)
    logging.basicConfig(
        level=logging.DEBUG if debug else logging.INFO,
        format="%(levelname)s: %(message)s",
    )
    logging.info(f"connecting to amqp server {os.environ['RABBIT_HOST']}")
    amqp_connection = pika.BlockingConnection(
        parameters=pika.ConnectionParameters(
            host=os.environ["RABBIT_HOST"],
            credentials=pika.PlainCredentials(
                os.environ["RABBIT_USER"],
                os.environ["RABBIT_PASSWORD"],
            ),
            heartbeat=0,
        )
    )
    swift_connection = None
    if os.environ.get("SWIFT_AUTH_URL"):
        logging.info(f"connecting to swift server {os.environ['SWIFT_AUTH_URL']}")
        swift_connection = swiftclient.Connection(
            authurl=os.environ["SWIFT_AUTH_URL"],
            user=os.environ["SWIFT_USERNAME"],
            key=os.environ["SWIFT_PASSWORD"],
            os_options={
                "project_domain_name": os.environ["SWIFT_PROJECT_DOMAIN_NAME"],
                "project_name": os.environ["SWIFT_PROJECT_NAME"],
                "user_domain_name": os.environ["SWIFT_USER_DOMAIN_NAME"],
            },
            auth_version="3",
        )
    else:
        logging.warning("no swift credentials provided, artifact upload disabled")
    publish_properties = pika.BasicProperties(
        delivery_mode=pika.spec.PERSISTENT_DELIVERY_MODE,
    )
    artifact_writer = ArtifactWriter(swift_connection)
    autopkgtest_runner = AutopkgtestRunner()
    worker = Worker(
        amqp_connection=amqp_connection,
        artifact_writer=artifact_writer,
        autopkgtest_runner=autopkgtest_runner,
        publish_properties=publish_properties,
        architecture=args.architecture,
        remote=args.remote,
        name=args.name,
        config_path=args.config,
        debug=args.debug,
    )
    worker.install_signal_handlers()
    try:
        rc = worker.run()
    except RuntimeError as e:
        logging.error(str(e))
        rc = 1
    finally:
        amqp_connection.close()
        if swift_connection:
            swift_connection.close()
    logging.info(f"worker exiting with code {rc}")
    sys.exit(rc)
