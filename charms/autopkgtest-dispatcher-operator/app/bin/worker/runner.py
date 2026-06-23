from __future__ import annotations

import configparser
import fnmatch
import itertools
import json
import logging
import signal
import time
from functools import partial
from pathlib import Path

from .adapters import (
    TEST_COMPLETE_EXCHANGE,
    TEST_STATUS_EXCHANGE,
    TESTBED_FAILURE_CODE,
    AMQPConnection,
    ArtifactWriter,
    AutopkgtestRunner,
    get_amqp_queues,
)
from .models import Request, Result

PER_PACKAGE_CONFIG_FILES = [
    "big_packages",
    "long_tests",
    "vm_packages",
    "never_run",
]


class PerPackageConfig:
    """Represents a per-package configuration file, which contains patterns for classifying packages for special handling."""

    def __init__(self, config_path: Path):
        """Initialize the per-package config by reading the given config file."""
        self._path = config_path
        self._mdate = self._path.stat().st_mtime
        self.contents = self._parse_per_package_config()

    def _parse_per_package_config(self):
        """Parse the per-package config file and return a set of patterns."""

        def _parse_entry(raw: str) -> str:
            entry = raw.strip()
            parts = entry.split("/")
            if len(parts) == 3:
                package, arch, release = parts
            elif len(parts) == 2:
                package, arch, release = parts[0], parts[1], "all"
            else:
                package, arch, release = entry, "all", "all"

            return f"{package}/{arch}/{release}"

        out = set()

        for line in self._path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            out.add(_parse_entry(line))

        return out

    def refresh(self):
        """Refresh the per-package config if the file has been modified."""
        mdate = self._path.stat().st_mtime
        if mdate != self._mdate:
            self._mdate = mdate
            self.contents = self._parse_per_package_config()

    def matches(self, package: str, arch: str, release: str):
        """Check if the given package, architecture, and release match any pattern in the config."""
        candidates = (
            f"{package}/{arch}/{release}",
            f"{package}/{arch}/all",
            f"{package}/all/{release}",
            f"{package}/all/all",
        )

        return any(
            fnmatch.fnmatchcase(candidate, pattern)
            for candidate, pattern in itertools.product(candidates, self.contents)
        )


class Worker:
    """Represents a worker that executes test requests."""

    def __init__(
        self,
        amqp_connection: AMQPConnection,
        artifact_writer: ArtifactWriter,
        autopkgtest_runner: AutopkgtestRunner,
        publish_properties: object,
        architecture: str,
        remote: str,
        name: str,
        config_path: Path,
        debug: bool = False,
    ):
        self._amqp_connection = amqp_connection
        self._artifact_writer = artifact_writer
        self._autopkgtest_runner = autopkgtest_runner
        self._publish_properties = publish_properties
        self.architecture = architecture
        self.remote = remote
        self.name = name
        self.debug = debug
        self.config = self._load_config(config_path)

        # none means no exit requested, any int is the exitcode to propagate on exit
        self.exit_requested: int | None = None
        # signal handlers are installed separately via install_signal_handlers()
        # so that tests can construct a Worker without mutating process state

        self._read_per_package_configs()

    def install_signal_handlers(self) -> None:
        """Install SIGTERM and SIGHUP handlers that trigger a graceful exit.

        Must be called from the main thread; mutates process-wide signal state.
        Kept out of ``__init__`` so tests can construct a :class:`Worker` without
        registering signal handlers.
        """
        signal.signal(signal.SIGTERM, self.request_exit)
        signal.signal(signal.SIGHUP, self.request_exit)

    def request_exit(self, signum, _):
        """Handle exit requests by setting the appropriate exit code."""
        # convert the signal number to an exit code expected by the service unit
        signum_conv = {
            signal.SIGTERM: 0,
            signal.SIGHUP: 10,
        }
        logging.info(f"signal {signum} received, requesting exit")
        self.exit_requested = signum_conv[signum]

    def _load_config(self, path: Path) -> configparser.ConfigParser:
        config = configparser.ConfigParser(allow_no_value=True)
        config.read(path)
        return config

    def _read_per_package_configs(self):
        for file in PER_PACKAGE_CONFIG_FILES:
            config_dir = Path(self.config["autopkgtest"]["per_package_config_dir"])
            setattr(self, f"_{file}", PerPackageConfig(config_dir / file))

    def _refresh_per_package_configs(self):
        for file in PER_PACKAGE_CONFIG_FILES:
            getattr(self, f"_{file}").refresh()

    def _build_autopkgtest_args(self, request: Request) -> list[str]:
        """Build the command line arguments to invoke autopkgtest with for the given request."""
        checkout_dir = self.config["autopkgtest"]["checkout_dir"].strip()
        if checkout_dir:
            args = [Path(checkout_dir) / "runner" / "autopkgtest"]
        else:
            args = ["autopkgtest"]

        args += [f"--output-dir={self._artifact_writer.get_out_dir()}"]

        args += request.build_args()

        extra_args = self.config["autopkgtest"]["extra_args"].split()
        if extra_args:
            args += extra_args

        if self.debug:
            args += ["--debug"]

        args += ["--", "lxd"]
        args += ["--delete-existing", "--name", self.name]
        args += ["-r", self.remote]
        if request.is_vm_pkg:
            args += ["--vm"]
            vm_suffix = "/vm"
        else:
            vm_suffix = ""
        image_arch = "amd64" if request.arch == "i386" else request.arch
        args += [
            f"{self.remote}:autopkgtest/ubuntu/{request.release}/{image_arch}{vm_suffix}"
        ]

        if request.is_big_pkg:
            args += [
                "-c",
                "limits.cpu=4",
                "-c",
                "limits.memory=8GiB",
                "-d",
                "root,size=100GiB",
            ]
        else:
            args += [
                "-c",
                "limits.cpu=2",
                "-c",
                "limits.memory=4GiB",
                "-d",
                "root,size=20GiB",
            ]

        return args

    def send_status_update(
        self, channel, request: Request, duration: int, running: bool
    ):
        """Send a status update for the given request."""
        if request.is_private():
            package = "private-test"
            params = {}
            logtail = "Running private test"
        else:
            package = request.package
            params = request.get_params()
            logtail = self._artifact_writer.get_logtail()

        body = json.dumps(
            {
                "package": package,
                "release": request.release,
                "architecture": request.arch,
                "params": params,
                "duration": duration,
                "running": running,
                "logtail": logtail,
            }
        )

        channel.basic_publish(
            exchange=TEST_STATUS_EXCHANGE,
            routing_key="",
            body=body,
            properties=self._publish_properties,
        )

    def _handle_result(self, result: Result):
        """Handle the result of a test request by writing artifacts and sending a completion message."""
        self._artifact_writer.write_artifacts(result)
        channel = self._amqp_connection.channel()
        channel.exchange_declare(
            exchange=TEST_COMPLETE_EXCHANGE, exchange_type="fanout", durable=True
        )

        # construct the env in a backwards compatible way
        env = {}
        if result.request.all_proposed:
            env["all-proposed"] = "1"
        if result.request.testname:
            env["testname"] = result.request.testname
        env = ",".join(f"{k}={v}" for k, v in env.items())

        triggers = (
            " ".join(result.request.triggers) if result.request.triggers else None
        )

        # for the test complete message, the testpkg_version field is expected to contain just the version string
        testpkg_version = result.testpkg_version.split()[1]

        complete_msg = json.dumps(
            {
                "architecture": result.request.arch,
                "container": result.container,
                "duration": result.duration,
                "exitcode": result.exitcode,
                "package": result.request.package,
                "testpkg_version": testpkg_version,
                "release": result.request.release,
                "requester": result.request.requester,
                "swift_dir": result.request.get_swift_dir(),
                "triggers": triggers,
                "env": env,
            }
        )
        channel.basic_publish(
            exchange=TEST_COMPLETE_EXCHANGE,
            routing_key="",
            body=complete_msg,
            properties=self._publish_properties,
        )
        channel.close()

    def process_message(self, channel, method, body):
        """Process a single test request message from the AMQP queue."""
        self._refresh_per_package_configs()
        release = method.routing_key.split("-")[-2]

        try:
            body = body.decode()
        except UnicodeDecodeError as e:
            logging.error(f"failed to decode message body: {e}")
            channel.basic_reject(method.delivery_tag, requeue=False)
            return

        try:
            # requester is either a bare package name or "pkgname\n{json_params}"
            req = body.split("\n", 1)
            package = req[0].strip()
            params = json.loads(req[1]) if len(req) > 1 else {}
            # fill in the request params
            params["package"] = package
            params["arch"] = self.architecture
            params["release"] = release
            params["is_big_pkg"] = self._big_packages.matches(
                package, self.architecture, release
            )
            params["is_long_test"] = self._long_tests.matches(
                package, self.architecture, release
            )
            params["is_vm_pkg"] = self._vm_packages.matches(
                package, self.architecture, release
            )
            params["is_never_run"] = self._never_run.matches(
                package, self.architecture, release
            )

            request = Request(**params)
        except (ValueError, json.JSONDecodeError) as e:
            logging.error(f"failed to parse message body: {e}")
            channel.basic_reject(method.delivery_tag, requeue=False)
            return

        logging.info(f"received request: {request}")

        self._artifact_writer.create_out_dir()
        result = None
        try:
            channel.basic_ack(method.delivery_tag)
            skip_reason = request.get_skip_reason()
            if skip_reason:
                result = Result.mock_result(request, skip_reason)
            else:
                autopkgtest_args = self._build_autopkgtest_args(request)
                test_channel = self._amqp_connection.channel()
                try:
                    test_channel.exchange_declare(
                        exchange=TEST_STATUS_EXCHANGE,
                        exchange_type="fanout",
                    )
                    # bind channel/request; the runner supplies (duration, running)
                    on_status = partial(self.send_status_update, test_channel, request)
                    exitcode, duration = self._autopkgtest_runner.run(
                        autopkgtest_args, on_status=on_status
                    )
                    if exitcode == TESTBED_FAILURE_CODE:
                        logging.info("tmpfail detected, requesting exit")
                        self.exit_requested = TESTBED_FAILURE_CODE
                    result = Result(
                        request=request,
                        container=request.get_container_name(),
                        run_id=request.run_id,
                        duration=duration,
                        exitcode=exitcode,
                        testpkg_version=None,
                        logs=None,
                    )
                finally:
                    if test_channel.is_open:
                        test_channel.close()
        except RuntimeError:
            # the autopkgtest runner raises RuntimeError on an unexpected exit
            # code; don't swallow it into a mock result, let the worker exit
            raise
        except Exception as e:
            logging.error(f"failed to process request: {e}")
            result = Result.mock_result(
                request, f"test failed to run due to an internal error: {e}"
            )
        finally:
            if result:
                self._handle_result(result)
            self._artifact_writer.cleanup()

    def run(self) -> int:
        releases = self.config["autopkgtest"]["releases"].split()
        channel, queues = get_amqp_queues(
            self._amqp_connection, releases, self.architecture
        )
        for queue in itertools.cycle(queues):
            method, _, body = channel.basic_get(queue)
            if method:
                self.process_message(channel, method, body)
            if self.exit_requested is not None:
                channel.close()
                return self.exit_requested
            time.sleep(2)
