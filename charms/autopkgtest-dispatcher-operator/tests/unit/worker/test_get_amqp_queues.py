"""Tests for worker.adapters.get_amqp_queues."""

from __future__ import annotations

from worker.adapters import get_amqp_queues

CONTEXTS = ["", "huge-", "ppa-", "upstream-"]


def test_single_release_yields_one_queue_per_context(fake_connection, identity_shuffle):
    channel, queues = get_amqp_queues(fake_connection, ["noble"], "amd64")
    assert queues == [
        "debci-noble-amd64",
        "debci-huge-noble-amd64",
        "debci-ppa-noble-amd64",
        "debci-upstream-noble-amd64",
    ]
    assert channel is fake_connection.channels[0]


def test_multiple_releases_cartesian(fake_connection, identity_shuffle):
    _, queues = get_amqp_queues(fake_connection, ["noble", "jammy", "focal"], "arm64")
    assert len(queues) == 3 * len(CONTEXTS)
    assert "debci-noble-arm64" in queues
    assert "debci-upstream-focal-arm64" in queues


def test_queue_naming_format(fake_connection, identity_shuffle):
    _, queues = get_amqp_queues(fake_connection, ["noble"], "s390x")
    for ctx in CONTEXTS:
        assert f"debci-{ctx}noble-s390x" in queues


def test_queues_declared_durable_not_autodelete(fake_connection, identity_shuffle):
    channel, queues = get_amqp_queues(fake_connection, ["noble"], "amd64")
    assert len(channel.declared_queues) == len(queues)
    for decl in channel.declared_queues:
        assert decl["durable"] is True
        assert decl["auto_delete"] is False


def test_basic_qos_set_once(fake_connection, identity_shuffle):
    channel, _ = get_amqp_queues(fake_connection, ["noble"], "amd64")
    assert channel.qos == {"prefetch_count": 1, "global_qos": True}


def test_shuffle_is_called(fake_connection, monkeypatch):
    seen = []
    monkeypatch.setattr(
        "worker.adapters.random.shuffle", lambda seq: seen.append(list(seq))
    )
    get_amqp_queues(fake_connection, ["noble"], "amd64")
    assert len(seen) == 1


def test_declared_queue_order_matches_returned(fake_connection, identity_shuffle):
    channel, queues = get_amqp_queues(fake_connection, ["noble", "jammy"], "amd64")
    declared = [d["queue"] for d in channel.declared_queues]
    assert declared == queues
