"""Tests for the Worker helper methods: argument building, status updates, results."""

from __future__ import annotations

import json

import pytest
from worker.adapters import TEST_COMPLETE_EXCHANGE, TEST_STATUS_EXCHANGE
from worker.models import Result


# --------------------------------------------------------------------------- #
# _build_autopkgtest_args
# --------------------------------------------------------------------------- #
@pytest.fixture
def build_args():
    """Return a helper that runs _build_autopkgtest_args and stringifies the result.

    Provided as a fixture-factory (rather than a module-level function) for
    consistency with the conftest helpers and to make each test's dependency
    on it explicit in its signature.
    """

    def _build(worker, request):
        worker._artifact_writer.get_out_dir.return_value = "/work/out"
        return [str(a) for a in worker._build_autopkgtest_args(request)]

    return _build


def test_build_args_default_binary_when_no_checkout_dir(
    make_worker, make_request, build_args
):
    w = make_worker()
    args = build_args(w, make_request())
    assert args[0] == "autopkgtest"


def test_build_args_uses_checkout_dir_runner(
    make_worker, make_worker_config, make_request, build_args
):
    cfg = make_worker_config(checkout_dir="/srv/checkout")
    w = make_worker(config=cfg)
    args = build_args(w, make_request())
    assert args[0] == "/srv/checkout/runner/autopkgtest"


def test_build_args_includes_output_dir_and_request_args(
    make_worker, make_request, build_args
):
    w = make_worker()
    args = build_args(w, make_request())
    assert "--output-dir=/work/out" in args
    # tokens that always come from Request.build_args()
    assert "--apt-upgrade" in args


def test_build_args_appends_extra_args(
    make_worker, make_worker_config, make_request, build_args
):
    cfg = make_worker_config(extra_args="--foo --bar")
    w = make_worker(config=cfg)
    args = build_args(w, make_request())
    assert "--foo" in args
    assert "--bar" in args


def test_build_args_debug_flag_present_only_when_enabled(
    make_worker, make_request, build_args
):
    assert "--debug" in build_args(make_worker(debug=True), make_request())
    assert "--debug" not in build_args(make_worker(debug=False), make_request())


def test_build_args_lxd_invocation_structure(make_worker, make_request, build_args):
    w = make_worker(name="my-container", remote="my-remote")
    args = build_args(w, make_request())
    sep = args.index("--")
    assert args[sep : sep + 6] == [
        "--",
        "lxd",
        "--delete-existing",
        "--name",
        "my-container",
        "-r",
    ]
    assert args[sep + 6] == "my-remote"


def test_build_args_image_reference_for_normal_arch(
    make_worker, make_request, build_args
):
    w = make_worker(remote="my-remote")
    args = build_args(w, make_request(arch="arm64", release="jammy"))
    assert "my-remote:autopkgtest/ubuntu/jammy/arm64" in args
    assert "--vm" not in args


def test_build_args_vm_pkg_adds_vm_flag_and_suffix(
    make_worker, make_request, build_args
):
    w = make_worker(remote="my-remote")
    args = build_args(w, make_request(is_vm_pkg=True, release="noble", arch="amd64"))
    assert "--vm" in args
    assert "my-remote:autopkgtest/ubuntu/noble/amd64/vm" in args


def test_build_args_i386_maps_image_to_amd64(make_worker, make_request, build_args):
    w = make_worker(remote="my-remote")
    args = build_args(w, make_request(arch="i386", release="noble"))
    assert "my-remote:autopkgtest/ubuntu/noble/amd64" in args


def test_build_args_big_pkg_limits(make_worker, make_request, build_args):
    w = make_worker()
    args = build_args(w, make_request(is_big_pkg=True))
    assert "limits.cpu=4" in args
    assert "limits.memory=8GiB" in args
    assert "root,size=100GiB" in args


def test_build_args_default_limits(make_worker, make_request, build_args):
    w = make_worker()
    args = build_args(w, make_request(is_big_pkg=False))
    assert "limits.cpu=2" in args
    assert "limits.memory=4GiB" in args
    assert "root,size=20GiB" in args


# --------------------------------------------------------------------------- #
# send_status_update
# --------------------------------------------------------------------------- #
def test_status_update_public_request(make_worker, make_request, fake_channel):
    w = make_worker()
    w._artifact_writer.get_logtail.return_value = "the-logtail"
    req = make_request(
        package="hello", requester="someone", release="noble", arch="amd64"
    )

    w.send_status_update(fake_channel, req, duration=42, running=True)

    assert len(fake_channel.published) == 1
    pub = fake_channel.published[0]
    assert pub["exchange"] == TEST_STATUS_EXCHANGE
    assert pub["routing_key"] == ""
    assert pub["properties"] is w._publish_properties
    body = json.loads(pub["body"])
    assert body["package"] == "hello"
    assert body["release"] == "noble"
    assert body["architecture"] == "amd64"
    assert body["duration"] == 42
    assert body["running"] is True
    assert body["logtail"] == "the-logtail"
    assert body["params"] == req.get_params()


def test_status_update_private_request_is_redacted(
    make_worker, make_request, fake_channel
):
    w = make_worker()
    req = make_request(package="secret", swiftuser="user")

    w.send_status_update(fake_channel, req, duration=0, running=False)

    body = json.loads(fake_channel.published[0]["body"])
    assert body["package"] == "private-test"
    assert body["params"] == {}
    assert body["logtail"] == "Running private test"
    # the real logtail must not be read for private tests
    w._artifact_writer.get_logtail.assert_not_called()


# --------------------------------------------------------------------------- #
# _handle_result
# --------------------------------------------------------------------------- #
def _result(request, **overrides):
    base = {
        "request": request,
        "container": request.get_container_name(),
        "run_id": request.run_id,
        "duration": 12,
        "exitcode": 0,
        "testpkg_version": "hello 1.0",
    }
    base.update(overrides)
    return Result(**base)


def test_handle_result_writes_artifacts_first(make_worker, make_request):
    w = make_worker()
    result = _result(make_request())
    w._handle_result(result)
    w._artifact_writer.write_artifacts.assert_called_once_with(result)


def test_handle_result_publishes_complete_message(make_worker, make_request):
    w = make_worker()
    req = make_request(
        package="hello",
        requester="someone",
        triggers=["trig/1", "trig/2"],
        release="noble",
        arch="amd64",
    )
    result = _result(req, duration=34, exitcode=0, testpkg_version="hello 2.0")

    w._handle_result(result)

    channel = w._amqp_connection.channels[-1]
    assert {
        "exchange": TEST_COMPLETE_EXCHANGE,
        "exchange_type": "fanout",
        "durable": True,
    } in channel.declared_exchanges
    assert channel.closed

    pub = channel.published[-1]
    assert pub["exchange"] == TEST_COMPLETE_EXCHANGE
    assert pub["routing_key"] == ""
    assert pub["properties"] is w._publish_properties
    body = json.loads(pub["body"])
    assert body["architecture"] == "amd64"
    assert body["container"] == req.get_container_name()
    assert body["duration"] == 34
    assert body["exitcode"] == 0
    assert body["package"] == "hello"
    # testpkg_version is reduced to just the version string
    assert body["testpkg_version"] == "2.0"
    assert body["release"] == "noble"
    assert body["requester"] == "someone"
    assert body["swift_dir"] == req.get_swift_dir()
    assert body["triggers"] == "trig/1 trig/2"
    assert body["env"] == ""


def test_handle_result_triggers_none_when_absent(make_worker, make_request):
    w = make_worker()
    result = _result(make_request(triggers=[]))
    w._handle_result(result)
    body = json.loads(w._amqp_connection.channels[-1].published[-1]["body"])
    assert body["triggers"] is None


def test_handle_result_env_includes_all_proposed_and_testname(
    make_worker, make_request
):
    w = make_worker()
    req = make_request(**{"all-proposed": "1", "testname": "smoke"})
    w._handle_result(_result(req))
    body = json.loads(w._amqp_connection.channels[-1].published[-1]["body"])
    assert body["env"] == "all-proposed=1,testname=smoke"


def test_handle_result_env_testname_only(make_worker, make_request):
    w = make_worker()
    req = make_request(testname="smoke")
    w._handle_result(_result(req))
    body = json.loads(w._amqp_connection.channels[-1].published[-1]["body"])
    assert body["env"] == "testname=smoke"
