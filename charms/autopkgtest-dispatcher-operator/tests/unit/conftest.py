"""Shared fixtures for worker unit tests.

The path bootstrap below is the single source of truth for making
``import worker`` work: it locates ``app/bin`` relative to this file, so the
suite runs regardless of the pytest rootdir or the current working directory
(there is intentionally no ``pythonpath`` setting in ``tests/pyproject.toml``).
"""

from __future__ import annotations

import shutil
import sys
import time
from collections import deque
from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_APP_BIN = Path(__file__).resolve().parent.parent.parent / "app" / "bin"
if str(_APP_BIN) not in sys.path:
    sys.path.insert(0, str(_APP_BIN))

import swiftclient  # noqa: E402
from worker.adapters import ArtifactWriter, AutopkgtestRunner  # noqa: E402
from worker.models import Request  # noqa: E402
from worker.runner import PER_PACKAGE_CONFIG_FILES, Worker  # noqa: E402

# fixed wall clock so Request.run_id is deterministic across the suite
FROZEN_STRUCT = time.struct_time((2026, 1, 1, 0, 0, 0, 2, 1, 0))
FROZEN_TIMESTAMP = "20260101_000000"


@pytest.fixture(scope="session", autouse=True)
def require_tar_and_gzip():
    """Fail the suite up front if the real tar/gzip binaries are unavailable.

    The ArtifactWriter packing tests deliberately shell out to the real
    ``tar`` and ``gzip`` rather than mocking subprocess, so their absence is a
    hard error (a deterministic failure is preferred over silent degradation).
    """
    missing = [tool for tool in ("tar", "gzip") if shutil.which(tool) is None]
    if missing:
        pytest.fail(f"required binaries not found on PATH: {', '.join(missing)}")


# --------------------------------------------------------------------------- #
# Fake AMQP doubles (implement the adapters.AMQP* Protocols structurally)
# --------------------------------------------------------------------------- #
class FakeMethod:
    """Stand-in for the pika delivery metadata passed to consumers."""

    def __init__(self, routing_key: str = "debci-noble-amd64", delivery_tag: int = 1):
        self.routing_key = routing_key
        self.delivery_tag = delivery_tag


@pytest.fixture
def fake_method():
    """Return a factory building FakeMethod delivery metadata."""

    def _make(routing_key="debci-noble-amd64", delivery_tag=1):
        return FakeMethod(routing_key=routing_key, delivery_tag=delivery_tag)

    return _make


class FakeChannel:
    """Records every call the worker makes against an AMQP channel."""

    def __init__(self):
        self.is_open = True
        self.qos = None
        self.declared_queues = []
        self.declared_exchanges = []
        self.published = []
        self.acked = []
        self.rejected = []
        self.closed = False
        # preset (method, properties, body) tuples returned by basic_get
        self.get_results = deque()

    def basic_qos(self, prefetch_count=0, global_qos=True):
        self.qos = {"prefetch_count": prefetch_count, "global_qos": global_qos}

    def queue_declare(self, queue, durable=False, auto_delete=True):
        self.declared_queues.append(
            {"queue": queue, "durable": durable, "auto_delete": auto_delete}
        )

    def exchange_declare(self, exchange, exchange_type="direct", durable=False):
        self.declared_exchanges.append(
            {"exchange": exchange, "exchange_type": exchange_type, "durable": durable}
        )

    def basic_publish(self, exchange, routing_key, body, properties=None):
        self.published.append(
            {
                "exchange": exchange,
                "routing_key": routing_key,
                "body": body,
                "properties": properties,
            }
        )

    def basic_ack(self, delivery_tag):
        self.acked.append(delivery_tag)

    def basic_reject(self, delivery_tag, requeue=True):
        self.rejected.append({"delivery_tag": delivery_tag, "requeue": requeue})

    def basic_get(self, queue):
        if self.get_results:
            return self.get_results.popleft()
        return (None, None, None)

    def close(self):
        self.closed = True
        self.is_open = False


class FakeConnection:
    """Hands out FakeChannels and remembers them for assertions."""

    def __init__(self):
        self.channels = []

    def channel(self):
        ch = FakeChannel()
        self.channels.append(ch)
        return ch


@pytest.fixture
def fake_channel():
    return FakeChannel()


@pytest.fixture
def fake_connection():
    return FakeConnection()


# --------------------------------------------------------------------------- #
# Fake swift connection
# --------------------------------------------------------------------------- #
class FakeSwiftConn:
    """Minimal swift connection double for ArtifactWriter."""

    def __init__(self):
        self.url = "https://swift.example/v1/AUTH_test"
        self.token = "tok-12345"  # noqa: S105 - dummy test token, not a secret
        self.headed = []
        self.put_containers = []
        # optional exceptions to raise, keyed by method name
        self.head_container_exc = None
        self.put_container_exc = None

    def head_container(self, container):
        self.headed.append(container)
        if self.head_container_exc is not None:
            exc = self.head_container_exc
            # only raise on the first call unless it's persistent
            self.head_container_exc = None
            raise exc

    def put_container(self, container, headers=None):
        self.put_containers.append({"container": container, "headers": headers})
        if self.put_container_exc is not None:
            raise self.put_container_exc


@pytest.fixture
def fake_swift_conn():
    return FakeSwiftConn()


@pytest.fixture
def client_exception():
    """Return a factory building a real swiftclient ClientException by status."""

    def _make(http_status):
        return swiftclient.exceptions.ClientException("boom", http_status=http_status)

    return _make


# --------------------------------------------------------------------------- #
# Fake subprocess.Popen for AutopkgtestRunner
# --------------------------------------------------------------------------- #
class FakePopen:
    """Popen double whose poll() returns None a fixed number of times."""

    def __init__(self, returncode=0, poll_none_times=0):
        self._returncode = returncode
        self._poll_none_times = poll_none_times
        self._polls = 0
        self.returncode = None

    def poll(self):
        if self._polls < self._poll_none_times:
            self._polls += 1
            return None
        self.returncode = self._returncode
        return self._returncode


@pytest.fixture
def make_popen():
    """Return a popen-callable factory that records how it was invoked."""

    def _make(returncode=0, poll_none_times=0):
        calls = SimpleNamespace(args=None, kwargs=None, proc=None)

        def _popen(args, **kwargs):
            proc = FakePopen(returncode=returncode, poll_none_times=poll_none_times)
            calls.args = args
            calls.kwargs = kwargs
            calls.proc = proc
            return proc

        _popen.calls = calls
        return _popen

    return _make


# --------------------------------------------------------------------------- #
# Clock / sleep / shuffle control
# --------------------------------------------------------------------------- #
@pytest.fixture
def frozen_clock(monkeypatch):
    """Freeze the clock used by Request.run_id generation."""
    monkeypatch.setattr("worker.models.time.gmtime", lambda *a, **k: FROZEN_STRUCT)
    return FROZEN_TIMESTAMP


@pytest.fixture
def no_sleep(monkeypatch):
    """Replace time.sleep in runner and adapters with a recording no-op."""
    calls = SimpleNamespace(runner=[], adapters=[])
    monkeypatch.setattr("worker.runner.time.sleep", lambda s: calls.runner.append(s))
    monkeypatch.setattr(
        "worker.adapters.time.sleep", lambda s: calls.adapters.append(s)
    )
    return calls


@pytest.fixture
def identity_shuffle(monkeypatch):
    """Make random.shuffle in adapters a no-op so queue order is deterministic."""
    monkeypatch.setattr("worker.adapters.random.shuffle", lambda seq: None)


# --------------------------------------------------------------------------- #
# Request factory
# --------------------------------------------------------------------------- #
@pytest.fixture
def make_request(frozen_clock):
    """Return a factory producing a valid Request with deterministic run_id.

    Defaults yield a minimal public request; pass kwargs to override. Keys with
    dashes (e.g. ``test-git``) are passed through to mirror the wire format.
    """

    def _make(**overrides):
        params = {"package": "testpkg", "arch": "amd64", "release": "noble"}
        params.update(overrides)
        return Request(**params)

    return _make


# --------------------------------------------------------------------------- #
# Worker config on disk + Worker factory
# --------------------------------------------------------------------------- #
@pytest.fixture
def make_worker_config(tmp_path):
    """Return a factory that writes a worker.cfg plus the 5 per-package config files."""

    def _make(releases="noble jammy", checkout_dir="", extra_args="", ppc=None):
        ppc_dir = tmp_path / "ppc"
        ppc_dir.mkdir(exist_ok=True)
        ppc = ppc or {}
        for name in PER_PACKAGE_CONFIG_FILES:
            entries = ppc.get(name, [])
            text = "\n".join(entries)
            (ppc_dir / name).write_text(text + ("\n" if text else ""))
        cfg = tmp_path / "worker.cfg"
        cfg.write_text(
            dedent(f"""\
                [autopkgtest]
                releases = {releases}
                per_package_config_dir = {ppc_dir}
                checkout_dir = {checkout_dir}
                extra_args = {extra_args}
            """)
        )
        return SimpleNamespace(path=cfg, ppc_dir=ppc_dir)

    return _make


@pytest.fixture
def make_worker(make_worker_config, fake_connection):
    """Return a factory building a Worker with fakes; signal handlers NOT installed."""

    def _make(**overrides):
        config = overrides.pop("config", None) or make_worker_config()
        kwargs = {
            "amqp_connection": overrides.pop("amqp_connection", fake_connection),
            "artifact_writer": overrides.pop(
                "artifact_writer", MagicMock(spec=ArtifactWriter)
            ),
            "autopkgtest_runner": overrides.pop(
                "autopkgtest_runner", MagicMock(spec=AutopkgtestRunner)
            ),
            "publish_properties": overrides.pop("publish_properties", object()),
            "architecture": overrides.pop("architecture", "amd64"),
            "remote": overrides.pop("remote", "lxd-remote"),
            "name": overrides.pop("name", "test-container"),
            "config_path": config.path,
            "debug": overrides.pop("debug", False),
        }
        kwargs.update(overrides)
        worker = Worker(**kwargs)
        worker._test_config = config  # expose for tests that need the paths
        return worker

    return _make
