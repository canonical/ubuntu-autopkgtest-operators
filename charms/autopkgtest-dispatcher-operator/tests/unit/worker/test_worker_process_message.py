"""Tests for Worker.process_message, the per-request orchestration entrypoint."""

from __future__ import annotations

import pytest
from worker.adapters import TEST_STATUS_EXCHANGE, TESTBED_FAILURE_CODE


@pytest.fixture
def worker_pm(make_worker):
    """Return a Worker whose _handle_result is stubbed to record handled results.

    _handle_result has its own dedicated tests; here we isolate the
    orchestration logic in process_message from result publishing.
    """
    w = make_worker()
    w._handled = []
    w._handle_result = lambda result: w._handled.append(result)
    return w


# --------------------------------------------------------------------------- #
# message rejection (no ack, no work performed)
# --------------------------------------------------------------------------- #
def test_rejects_undecodable_body(worker_pm, fake_channel, fake_method):
    worker_pm.process_message(fake_channel, fake_method(), b"\xff\xfe")
    assert fake_channel.rejected == [{"delivery_tag": 1, "requeue": False}]
    assert fake_channel.acked == []
    worker_pm._artifact_writer.create_out_dir.assert_not_called()
    assert worker_pm._handled == []


def test_rejects_invalid_json_params(worker_pm, fake_channel, fake_method):
    worker_pm.process_message(fake_channel, fake_method(), b"hello\n{not valid json}")
    assert fake_channel.rejected == [{"delivery_tag": 1, "requeue": False}]
    worker_pm._artifact_writer.create_out_dir.assert_not_called()


def test_rejects_invalid_package_name(worker_pm, fake_channel, fake_method):
    worker_pm.process_message(fake_channel, fake_method(), b"bad@@name\n{}")
    assert fake_channel.rejected == [{"delivery_tag": 1, "requeue": False}]
    assert worker_pm._handled == []


# --------------------------------------------------------------------------- #
# release parsing
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "routing_key,release",
    [
        ("debci-noble-amd64", "noble"),
        ("debci-huge-jammy-arm64", "jammy"),
        ("debci-ppa-focal-s390x", "focal"),
        ("debci-upstream-noble-riscv64", "noble"),
    ],
)
def test_parses_release_from_routing_key(
    worker_pm, fake_channel, fake_method, routing_key, release
):
    worker_pm._autopkgtest_runner.run.return_value = (0, 5)
    worker_pm.process_message(
        fake_channel, fake_method(routing_key=routing_key), b"hello"
    )
    assert worker_pm._handled[0].request.release == release


# --------------------------------------------------------------------------- #
# happy path: real run
# --------------------------------------------------------------------------- #
def test_acks_creates_outdir_runs_and_cleans_up(worker_pm, fake_channel, fake_method):
    worker_pm._autopkgtest_runner.run.return_value = (0, 42)
    worker_pm.process_message(fake_channel, fake_method(), b"hello")

    assert fake_channel.acked == [1]
    worker_pm._artifact_writer.create_out_dir.assert_called_once()
    worker_pm._autopkgtest_runner.run.assert_called_once()
    worker_pm._artifact_writer.cleanup.assert_called_once()
    result = worker_pm._handled[0]
    assert result.exitcode == 0
    assert result.duration == 42


def test_declares_status_exchange_and_closes_test_channel(
    worker_pm, fake_channel, fake_method
):
    worker_pm._autopkgtest_runner.run.return_value = (0, 1)
    worker_pm.process_message(fake_channel, fake_method(), b"hello")
    # _handle_result is stubbed, so the only channel opened is the test channel
    test_channel = worker_pm._amqp_connection.channels[-1]
    assert {
        "exchange": TEST_STATUS_EXCHANGE,
        "exchange_type": "fanout",
        "durable": False,
    } in test_channel.declared_exchanges
    assert test_channel.closed


def test_passes_on_status_callback_to_runner(worker_pm, fake_channel, fake_method):
    worker_pm._autopkgtest_runner.run.return_value = (0, 1)
    worker_pm.process_message(fake_channel, fake_method(), b"hello")
    args, kwargs = worker_pm._autopkgtest_runner.run.call_args
    assert isinstance(args[0], list)  # the built autopkgtest args
    assert callable(kwargs["on_status"])


def test_does_not_double_close_already_closed_test_channel(
    worker_pm, fake_channel, fake_method
):
    # if the test channel is already closed by the time the finally runs,
    # process_message must not call close() on it again
    def close_then_return(*args, **kwargs):
        test_channel = worker_pm._amqp_connection.channels[-1]
        test_channel.close()
        test_channel.closed = False  # observe whether close() is called again
        return (0, 1)

    worker_pm._autopkgtest_runner.run.side_effect = close_then_return
    worker_pm.process_message(fake_channel, fake_method(), b"hello")
    # close() flips .closed back to True; staying False proves it was not re-closed
    assert worker_pm._amqp_connection.channels[-1].closed is False


def test_refreshes_per_package_configs_first(
    worker_pm, fake_channel, fake_method, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        worker_pm, "_refresh_per_package_configs", lambda: calls.append(True)
    )
    worker_pm._autopkgtest_runner.run.return_value = (0, 1)
    worker_pm.process_message(fake_channel, fake_method(), b"hello")
    assert calls == [True]


# --------------------------------------------------------------------------- #
# per-package classification feeds the Request
# --------------------------------------------------------------------------- #
def test_per_package_flags_applied_to_request(
    make_worker, make_worker_config, fake_channel, fake_method
):
    cfg = make_worker_config(
        ppc={
            "big_packages": ["hello"],
            "long_tests": ["hello"],
            "vm_packages": ["hello"],
        }
    )
    w = make_worker(config=cfg)
    w._handled = []
    w._handle_result = lambda r: w._handled.append(r)
    w._autopkgtest_runner.run.return_value = (0, 1)

    w.process_message(fake_channel, fake_method(), b"hello")

    req = w._handled[0].request
    assert req.is_big_pkg
    assert req.is_long_test
    assert req.is_vm_pkg


# --------------------------------------------------------------------------- #
# skip path (never-run)
# --------------------------------------------------------------------------- #
def test_never_run_package_is_skipped(
    make_worker, make_worker_config, fake_channel, fake_method
):
    cfg = make_worker_config(ppc={"never_run": ["hello"]})
    w = make_worker(config=cfg)
    w._handled = []
    w._handle_result = lambda r: w._handled.append(r)

    w.process_message(fake_channel, fake_method(), b"hello")

    w._autopkgtest_runner.run.assert_not_called()
    assert fake_channel.acked == [1]
    result = w._handled[0]
    assert result.request.is_never_run is True
    assert result.exitcode == 99  # mock result exit code
    w._artifact_writer.cleanup.assert_called_once()


# --------------------------------------------------------------------------- #
# error handling
# --------------------------------------------------------------------------- #
def test_testbed_failure_requests_exit(worker_pm, fake_channel, fake_method):
    worker_pm._autopkgtest_runner.run.return_value = (TESTBED_FAILURE_CODE, 7)
    worker_pm.process_message(fake_channel, fake_method(), b"hello")
    assert worker_pm.exit_requested == TESTBED_FAILURE_CODE
    assert worker_pm._handled[0].exitcode == TESTBED_FAILURE_CODE


@pytest.mark.parametrize("rc", [2, 4, 99])
def test_normal_failure_does_not_request_exit(worker_pm, fake_channel, fake_method, rc):
    # only the testbed-failure code triggers a graceful exit; ordinary non-zero
    # exit codes leave exit_requested untouched so the worker keeps consuming
    worker_pm._autopkgtest_runner.run.return_value = (rc, 1)
    worker_pm.process_message(fake_channel, fake_method(), b"hello")
    assert worker_pm.exit_requested is None
    assert worker_pm._handled[0].exitcode == rc


def test_runtimeerror_from_runner_propagates(worker_pm, fake_channel, fake_method):
    worker_pm._autopkgtest_runner.run.side_effect = RuntimeError("boom")
    with pytest.raises(RuntimeError, match="boom"):
        worker_pm.process_message(fake_channel, fake_method(), b"hello")
    # the message was acked before running, cleanup still happens, no result handled
    assert fake_channel.acked == [1]
    worker_pm._artifact_writer.cleanup.assert_called_once()
    assert worker_pm._handled == []


def test_generic_error_produces_mock_result(worker_pm, fake_channel, fake_method):
    worker_pm._autopkgtest_runner.run.side_effect = OSError("disk gone")
    worker_pm.process_message(fake_channel, fake_method(), b"hello")
    result = worker_pm._handled[0]
    assert result.exitcode == 99
    assert "internal error" in result.logs
    assert "disk gone" in result.logs
    worker_pm._artifact_writer.cleanup.assert_called_once()
