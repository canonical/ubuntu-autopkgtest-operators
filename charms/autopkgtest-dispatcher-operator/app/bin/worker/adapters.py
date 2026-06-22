from __future__ import annotations

import itertools
import json
import logging
import os
import random
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

import swiftclient

from .models import Result

TESTBED_FAILURE_CODE = 16
TEST_COMPLETE_EXCHANGE = "testcomplete.fanout"
TEST_STATUS_EXCHANGE = "teststatus.fanout"


class AMQPMethod(Protocol):
    """The delivery metadata object returned by ``basic_get`` / passed to consumers."""

    routing_key: str
    delivery_tag: int


class AMQPChannel(Protocol):
    """The subset of the pika channel interface the worker relies on."""

    @property
    def is_open(self) -> bool: ...

    def basic_qos(self, prefetch_count: int = 0, global_qos: bool = True) -> None: ...

    def queue_declare(
        self, queue: str, durable: bool = False, auto_delete: bool = True
    ) -> None: ...

    def exchange_declare(
        self, exchange: str, exchange_type: str = "direct", durable: bool = False
    ) -> None: ...

    def basic_publish(
        self,
        exchange: str,
        routing_key: str,
        body: bytes | str,
        properties: Any = None,
    ) -> None: ...

    def basic_ack(self, delivery_tag: int) -> None: ...

    def basic_reject(self, delivery_tag: int, requeue: bool = True) -> None: ...

    def basic_get(self, queue: str) -> tuple[AMQPMethod | None, Any, bytes | None]: ...

    def close(self) -> None: ...


class AMQPConnection(Protocol):
    """The subset of the pika connection interface the worker relies on."""

    def channel(self) -> AMQPChannel: ...


def get_amqp_queues(
    connection: AMQPConnection, releases: list[str], arch: str
) -> tuple[AMQPChannel, list[str]]:
    """Declare and return the AMQP queues to consume from based on the given releases and architecture."""
    channel = connection.channel()
    channel.basic_qos(prefetch_count=1, global_qos=True)

    queues = []
    contexts = ["", "huge-", "ppa-", "upstream-"]

    for release, context in itertools.product(releases, contexts):
        queues.append(f"debci-{context}{release}-{arch}")

    random.shuffle(queues)

    for queue in queues:
        logging.info(f"declaring queue {queue}")
        channel.queue_declare(queue=queue, durable=True, auto_delete=False)

    return (channel, queues)


class AutopkgtestRunner:
    """Runs the autopkgtest binary as a subprocess."""

    POLL_INTERVAL_SECONDS = 5

    def __init__(
        self,
        *,
        popen=subprocess.Popen,
        sleep=time.sleep,
        now=time.time,
    ):
        # injection hooks for tests; production uses the stdlib defaults
        self._popen = popen
        self._sleep = sleep
        self._now = now

    def run(
        self,
        args: list[str],
        on_status: Callable[[int, bool], None] | None = None,
    ) -> tuple[int, int]:
        """Run autopkgtest with ``args`` and return ``(exitcode, duration_seconds)``.

        ``on_status`` is invoked with ``(duration, running=True)`` every
        ``POLL_INTERVAL_SECONDS`` while the subprocess is alive, and once with
        ``(duration, running=False)`` after it exits.

        Raises :class:`RuntimeError` if autopkgtest exits with code 1, which
        indicates an unexpected failure that must not be recovered from.
        """
        start_time = self._now()
        logging.info(f"running autopkgtest with args: {' '.join(map(str, args))}")

        autopkgtest = self._popen(
            args, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT
        )
        while autopkgtest.poll() is None:
            if on_status:
                on_status(int(self._now() - start_time), True)
            self._sleep(self.POLL_INTERVAL_SECONDS)

        duration = int(self._now() - start_time)
        if on_status:
            on_status(duration, False)

        exitcode = autopkgtest.returncode
        logging.info(
            f"autopkgtest finished with exit code {exitcode} after {duration} seconds"
        )
        if exitcode == 1:
            # exit code 1 indicates a catastrophic failure, do not try to recover
            raise RuntimeError(
                f"autopkgtest failed with unexpected exit code {exitcode}"
            )

        return (exitcode, duration)


class ArtifactWriter:
    """Abstraction for writing test artifacts to swift or local disk.

    Separated out for testability and to avoid coupling the worker logic to swift.
    """

    def __init__(self, swift_conn: swiftclient.Connection | None):
        self.swift_conn = swift_conn
        self._out_dir = None

    def _try_with_timeout(self, func, *args, **kwargs):
        """Try to execute a function with a timeout, retrying on swift client exceptions."""
        timeout = 60
        while timeout > 0:
            try:
                return func(*args, **kwargs)
            except swiftclient.exceptions.ClientException:
                logging.warning("retrying...")
                time.sleep(5)
                timeout -= 5
        logging.error("operation failed after multiple retries, giving up")
        raise TimeoutError("")

    def _ensure_container(self, container: str, swiftuser: str | None = None):
        """Ensure that the specified swift container exists, creating it if necessary."""
        try:
            self.swift_conn.head_container(container)
        except swiftclient.exceptions.ClientException as e:
            if e.http_status == 404:
                logging.info(f"creating swift container {container}")
                container_headers = {
                    "X-Container-Read": f"*:{swiftuser}"
                    if swiftuser
                    else ".rlistings,.r:*",
                }
                self.swift_conn.put_container(container, headers=container_headers)
            else:
                raise
            self._try_with_timeout(self.swift_conn.head_container, container)

    def _generate_artifacts_from_result(self, result: Result):
        """Generate artifact files from the given test result."""
        with open(self._out_dir / "exitcode", "w") as f:
            f.write(f"{result.exitcode}\n")
        with open(self._out_dir / "duration", "w") as f:
            f.write(f"{result.duration}\n")

        if result.request.requester:
            with open(self._out_dir / "requester", "w") as f:
                f.write(f"{result.request.requester}\n")

        if result.request.readable_by:
            with open(self._out_dir / "readable-by", "w") as f:
                f.write(f"{','.join(result.request.readable_by)}\n")

        # if the result is mocked, we need to manually write testinfo.json and testpkg_version
        if result.testinfo:
            with open(self._out_dir / "testinfo.json", "w") as f:
                json.dump(result.testinfo, f)
        if result.testpkg_version:
            with open(self._out_dir / "testpkg-version", "w") as f:
                f.write(f"{result.testpkg_version}\n")
        else:
            # the result object should have a testpkg-version for the test complete message
            # so if it isn't a mock result we have to populate it from the artifact file
            # if autopkgtest crashed at a point where the testpkg-version file wasn't written,
            # we will need to synthesize a value here to avoid breaking the test complete message parsing
            try:
                result.testpkg_version = self.get_testpkg_version()
            except FileNotFoundError:
                logging.warning("testpkg-version file not found, using fallback value")
                result.testpkg_version = f"{result.request.package} unknown"

        if result.logs:
            with open(self._out_dir / "log", "w") as f:
                f.write(result.logs)

    def _pack_artifacts_for_upload(self):
        """Pack the generated artifacts into tarballs for upload."""
        # these files, if exist, should be put into a results tarball
        RESULT_TARBALL_FILES = [
            "exitcode",
            "testbed-packages",
            "testpkg-version",
            "duration",
            "testinfo.json",
            "requester",
            "summary",
        ]

        artifacts = set(f.name for f in self._out_dir.iterdir())

        to_pack = []
        for f in RESULT_TARBALL_FILES:
            if (self._out_dir / f).exists():
                to_pack.append(f)

        subprocess.check_call(["tar", "-cf", "result.tar"] + to_pack, cwd=self._out_dir)

        # gzip the log file
        subprocess.check_call(["gzip", "-9", "log"], cwd=self._out_dir)

        # we want to upload the log separately
        artifacts.discard("log")
        # readable-by is also special, it must be uploaded separately to set the correct container read permissions
        artifacts.discard("readable-by")

        # compress everything else into a tarball
        if artifacts:
            subprocess.check_call(
                ["tar", "-czf", "artifacts.tar.gz"] + list(artifacts), cwd=self._out_dir
            )
            for f in artifacts:
                artifact_path = self._out_dir / f
                if artifact_path.is_file():
                    artifact_path.unlink()
                else:
                    shutil.rmtree(artifact_path)

    def get_out_dir(self) -> Path:
        """Get the output directory for artifacts, creating it if necessary."""
        if not self._out_dir:
            self.create_out_dir()
        return self._out_dir

    def create_out_dir(self):
        """Create a temporary output directory for artifacts."""
        self._out_dir = Path(tempfile.mkdtemp(prefix="autopkgtest-artifacts-")) / "out"
        # the parent directory should be created by tempfile
        # but we need to create the out directory ourselves
        self._out_dir.mkdir()

    def cleanup(self):
        """Clean up the temporary output directory."""
        # only delete the work dir if we are using swift
        if self._out_dir and self._out_dir.exists() and self.swift_conn:
            shutil.rmtree(self._out_dir)
            self._out_dir = None

    def get_log_path(self) -> Path:
        return self._out_dir / "log"

    def get_logtail(self) -> str:
        """Get the logtail, which is a combination of the head and tail of the log file, for status messages."""
        logtail = ""
        log_path = self.get_log_path()
        try:
            with open(log_path, "rb") as f:
                for _ in range(5):
                    # get the first 5 lines of the log, which contain relevant info for debugging
                    logtail += f.readline().decode("UTF-8", errors="replace")
                logtail += "[... 🠉 HEAD 🠉 ... 🠋 TAIL 🠋 ...]\n"

                # now get the tail of the log
                try:
                    f.seek(-2000, os.SEEK_END)
                    # discard partial line
                    f.readline()
                except OSError:
                    # log is smaller than 2000 lines
                    pass
                logtail += f.read().decode("UTF-8", errors="replace")
        except FileNotFoundError:
            logtail += "Log file not found.\n"
        return logtail

    def get_testpkg_version(self) -> str:
        testpkg_version_path = self._out_dir / "testpkg-version"
        if testpkg_version_path.exists():
            return testpkg_version_path.read_text().strip()
        raise FileNotFoundError("testpkg-version file not found")

    def write_artifacts(self, result: Result):
        """Write artifacts for the given test result, including uploading to swift if available."""
        self._generate_artifacts_from_result(result)
        # we want to pack the artifacts, even if we don't upload them
        self._pack_artifacts_for_upload()
        if not self.swift_conn:
            logging.warning("no swift connection, skipping artifact upload")
            return

        # we only upload a specific set of artifacts
        ARTIFACTS_TO_UPLOAD = [
            "artifacts.tar.gz",
            "result.tar",
            "log.gz",
            "readable-by",
        ]

        self._ensure_container(
            result.request.get_container_name(), result.request.swiftuser
        )

        for artifact in ARTIFACTS_TO_UPLOAD:
            artifact_path = self._out_dir / artifact
            if artifact_path.exists():
                with open(artifact_path, "rb") as f:
                    if artifact == "log.gz":
                        # logs get special headers and content-type
                        headers = {
                            "Content-Encoding": "gzip",
                        }
                        content_type = "text/plain; charset=UTF-8"
                    else:
                        headers = None
                        content_type = None
                    self._try_with_timeout(
                        swiftclient.put_object,
                        self.swift_conn.url,
                        token=self.swift_conn.token,
                        container=result.request.get_container_name(),
                        name=result.request.get_swift_dir() + "/" + artifact,
                        contents=f,
                        content_type=content_type,
                        headers=headers,
                        content_length=artifact_path.stat().st_size,
                    )
            else:
                logging.warning(f"artifact {artifact} not found, skipping upload")
