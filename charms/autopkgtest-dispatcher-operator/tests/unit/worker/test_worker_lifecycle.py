"""Tests for Worker construction, signal handling and the main run loop."""

from __future__ import annotations

import signal

import pytest
from worker.runner import PER_PACKAGE_CONFIG_FILES, PerPackageConfig


# --------------------------------------------------------------------------- #
# __init__
# --------------------------------------------------------------------------- #
def test_init_stores_attributes_and_defaults(make_worker):
    w = make_worker(architecture="arm64", remote="my-remote", name="container-1")
    assert w.architecture == "arm64"
    assert w.remote == "my-remote"
    assert w.name == "container-1"
    assert w.debug is False
    # none means "no exit requested"
    assert w.exit_requested is None


def test_init_loads_config(make_worker):
    w = make_worker()
    assert w.config["autopkgtest"]["releases"] == "noble jammy"


def test_init_creates_a_per_package_config_for_each_file(make_worker):
    w = make_worker()
    for name in PER_PACKAGE_CONFIG_FILES:
        assert isinstance(getattr(w, f"_{name}"), PerPackageConfig)


def test_init_does_not_install_signal_handlers(make_worker, monkeypatch):
    # constructing a Worker must not mutate process-wide signal state
    calls = []
    monkeypatch.setattr("worker.runner.signal.signal", lambda *a: calls.append(a))
    make_worker()
    assert calls == []


# --------------------------------------------------------------------------- #
# install_signal_handlers
# --------------------------------------------------------------------------- #
def test_install_signal_handlers_registers_term_and_hup(make_worker, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "worker.runner.signal.signal",
        lambda sig, handler: calls.append((sig, handler)),
    )
    w = make_worker()
    w.install_signal_handlers()
    assert len(calls) == 2
    assert (signal.SIGTERM, w.request_exit) in calls
    assert (signal.SIGHUP, w.request_exit) in calls


# --------------------------------------------------------------------------- #
# request_exit
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("sig,code", [(signal.SIGTERM, 0), (signal.SIGHUP, 10)])
def test_request_exit_maps_signal_to_exit_code(make_worker, sig, code):
    w = make_worker()
    w.request_exit(sig, None)
    assert w.exit_requested == code


def test_request_exit_unknown_signal_raises_keyerror(make_worker):
    w = make_worker()
    with pytest.raises(KeyError):
        w.request_exit(signal.SIGINT, None)


# --------------------------------------------------------------------------- #
# run
# --------------------------------------------------------------------------- #
def _patch_queues(monkeypatch, channel, queues=("debci-noble-amd64",)):
    """Patch get_amqp_queues to return the given channel/queues and capture args."""
    captured = {}

    def fake_get_amqp_queues(conn, releases, arch):
        captured["conn"] = conn
        captured["releases"] = releases
        captured["arch"] = arch
        return channel, list(queues)

    monkeypatch.setattr("worker.runner.get_amqp_queues", fake_get_amqp_queues)
    return captured


def test_run_requests_queues_with_config_releases_and_arch(
    make_worker, fake_channel, monkeypatch
):
    w = make_worker(architecture="amd64")  # config releases = "noble jammy"
    captured = _patch_queues(monkeypatch, fake_channel)
    # exit on the first idle sleep so the loop terminates
    monkeypatch.setattr(
        "worker.runner.time.sleep",
        lambda s: setattr(w, "exit_requested", 0),
    )
    w.run()
    assert captured["conn"] is w._amqp_connection
    assert captured["releases"] == ["noble", "jammy"]
    assert captured["arch"] == "amd64"


def test_run_processes_a_message_then_returns_exit_code(
    make_worker, fake_channel, fake_method, monkeypatch, no_sleep
):
    w = make_worker()
    fake_channel.get_results.append((fake_method(), None, b"x"))
    _patch_queues(monkeypatch, fake_channel)

    processed = []

    def fake_process(channel, method, body):
        processed.append((channel, method, body))
        w.exit_requested = 0

    monkeypatch.setattr(w, "process_message", fake_process)
    rc = w.run()

    assert rc == 0
    assert len(processed) == 1
    assert processed[0][0] is fake_channel
    assert processed[0][2] == b"x"
    assert fake_channel.closed
    # exit was requested right after processing, before the idle sleep
    assert no_sleep.runner == []


def test_run_skips_processing_when_no_message_then_sleeps(
    make_worker, fake_channel, monkeypatch
):
    w = make_worker()
    _patch_queues(monkeypatch, fake_channel)  # basic_get returns (None, None, None)

    sleeps = []

    def fake_sleep(seconds):
        sleeps.append(seconds)
        w.exit_requested = 7

    monkeypatch.setattr("worker.runner.time.sleep", fake_sleep)
    process_calls = []
    monkeypatch.setattr(w, "process_message", lambda *a: process_calls.append(a))

    rc = w.run()

    assert rc == 7
    assert process_calls == []  # no message -> process_message not called
    assert sleeps == [2]  # idle poll sleeps for 2s
    assert fake_channel.closed


def test_run_propagates_nonzero_exit_code(
    make_worker, fake_channel, fake_method, monkeypatch
):
    w = make_worker()
    fake_channel.get_results.append((fake_method(), None, b"x"))
    _patch_queues(monkeypatch, fake_channel)
    monkeypatch.setattr(
        w, "process_message", lambda *a: setattr(w, "exit_requested", 10)
    )
    assert w.run() == 10
