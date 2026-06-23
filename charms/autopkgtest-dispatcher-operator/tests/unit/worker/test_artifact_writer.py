"""Tests for worker.adapters.ArtifactWriter."""

from __future__ import annotations

import json
import subprocess
import tarfile

import pytest
import swiftclient
from worker.adapters import ArtifactWriter
from worker.models import Result


@pytest.fixture
def out_dir(tmp_path):
    d = tmp_path / "out"
    d.mkdir()
    return d


def make_writer(swift_conn=None, out_dir=None):
    writer = ArtifactWriter(swift_conn)
    if out_dir is not None:
        writer._out_dir = out_dir
    return writer


@pytest.fixture
def make_result(make_request):
    def _make(**overrides):
        req = overrides.pop("request", None) or make_request(
            **overrides.pop("request_kwargs", {})
        )
        params = {
            "request": req,
            "container": req.get_container_name(),
            "run_id": req.run_id,
            "duration": 12,
            "exitcode": 0,
        }
        params.update(overrides)
        return Result(**params)

    return _make


@pytest.fixture
def put_object_recorder(monkeypatch):
    calls = []

    def _put(url, **kwargs):
        calls.append({"url": url, **kwargs})

    monkeypatch.setattr("worker.adapters.swiftclient.put_object", _put)
    return calls


# --------------------------------------------------------------------------- #
# constructor / out dir lifecycle
# --------------------------------------------------------------------------- #
def test_constructor_without_swift():
    writer = ArtifactWriter(None)
    assert writer.swift_conn is None
    assert writer._out_dir is None


def test_constructor_with_swift(fake_swift_conn):
    assert ArtifactWriter(fake_swift_conn).swift_conn is fake_swift_conn


def test_create_and_get_out_dir():
    writer = ArtifactWriter(None)
    writer.create_out_dir()
    assert writer._out_dir.exists()
    assert writer.get_out_dir() == writer._out_dir


def test_get_out_dir_creates_when_absent():
    writer = ArtifactWriter(None)
    out = writer.get_out_dir()
    assert out.exists()


def test_cleanup_removes_dir_when_swift_present(fake_swift_conn, out_dir):
    writer = make_writer(fake_swift_conn, out_dir)
    writer.cleanup()
    assert not out_dir.exists()
    assert writer._out_dir is None


def test_cleanup_noop_without_swift(out_dir):
    writer = make_writer(None, out_dir)
    writer.cleanup()
    assert out_dir.exists()


def test_cleanup_noop_without_out_dir(fake_swift_conn):
    # _out_dir is None -> nothing happens, no error
    make_writer(fake_swift_conn).cleanup()


# --------------------------------------------------------------------------- #
# _try_with_timeout
# --------------------------------------------------------------------------- #
def test_try_with_timeout_success_first_call():
    writer = ArtifactWriter(None)
    assert writer._try_with_timeout(lambda: 42) == 42


def test_try_with_timeout_retries_then_succeeds(no_sleep, client_exception):
    writer = ArtifactWriter(None)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] == 1:
            raise client_exception(503)
        return "ok"

    assert writer._try_with_timeout(flaky) == "ok"
    assert calls["n"] == 2
    assert no_sleep.adapters == [5]


def test_try_with_timeout_exhaustion_raises(no_sleep, client_exception):
    writer = ArtifactWriter(None)

    def always_fail():
        raise client_exception(503)

    with pytest.raises(TimeoutError):
        writer._try_with_timeout(always_fail)
    # 60s budget / 5s per retry == 12 sleeps
    assert no_sleep.adapters == [5] * 12


# --------------------------------------------------------------------------- #
# _ensure_container
# --------------------------------------------------------------------------- #
def test_ensure_container_exists(fake_swift_conn):
    writer = ArtifactWriter(fake_swift_conn)
    writer._ensure_container("autopkgtest-noble")
    assert fake_swift_conn.headed == ["autopkgtest-noble"]
    assert fake_swift_conn.put_containers == []


def test_ensure_container_creates_public_on_404(
    fake_swift_conn, no_sleep, client_exception
):
    fake_swift_conn.head_container_exc = client_exception(404)
    writer = ArtifactWriter(fake_swift_conn)
    writer._ensure_container("c")
    assert fake_swift_conn.put_containers[0]["headers"] == {
        "X-Container-Read": ".rlistings,.r:*"
    }


def test_ensure_container_creates_private_on_404(
    fake_swift_conn, no_sleep, client_exception
):
    fake_swift_conn.head_container_exc = client_exception(404)
    writer = ArtifactWriter(fake_swift_conn)
    writer._ensure_container("c", swiftuser="alice")
    assert fake_swift_conn.put_containers[0]["headers"] == {
        "X-Container-Read": "*:alice"
    }


def test_ensure_container_reraises_non_404(fake_swift_conn, client_exception):
    fake_swift_conn.head_container_exc = client_exception(500)
    writer = ArtifactWriter(fake_swift_conn)
    with pytest.raises(swiftclient.exceptions.ClientException):
        writer._ensure_container("c")
    assert fake_swift_conn.put_containers == []


# --------------------------------------------------------------------------- #
# _generate_artifacts_from_result
# --------------------------------------------------------------------------- #
def test_generate_writes_exitcode_and_duration(make_result, out_dir):
    writer = make_writer(None, out_dir)
    writer._generate_artifacts_from_result(make_result(exitcode=7, duration=33))
    assert (out_dir / "exitcode").read_text() == "7\n"
    assert (out_dir / "duration").read_text() == "33\n"


def test_generate_writes_optional_files_when_present(make_result, out_dir):
    writer = make_writer(None, out_dir)
    result = make_result(
        request_kwargs={"requester": "bob", "readable-by": ["team-a", "team-b"]},
        testinfo={"k": "v"},
        testpkg_version="pkg 1.0",
        logs="hello log",
    )
    writer._generate_artifacts_from_result(result)
    assert (out_dir / "requester").read_text() == "bob\n"
    assert (out_dir / "readable-by").read_text() == "team-a,team-b\n"
    assert json.loads((out_dir / "testinfo.json").read_text()) == {"k": "v"}
    assert (out_dir / "testpkg-version").read_text() == "pkg 1.0\n"
    assert (out_dir / "log").read_text() == "hello log"


def test_generate_skips_optional_files_when_absent(make_result, out_dir):
    writer = make_writer(None, out_dir)
    # no requester/readable_by/testinfo/logs; testpkg-version file pre-exists
    (out_dir / "testpkg-version").write_text("pkg 2.0\n")
    writer._generate_artifacts_from_result(make_result())
    assert not (out_dir / "requester").exists()
    assert not (out_dir / "readable-by").exists()
    assert not (out_dir / "testinfo.json").exists()
    assert not (out_dir / "log").exists()


def test_generate_testpkg_version_fallback_from_file(make_result, out_dir):
    writer = make_writer(None, out_dir)
    (out_dir / "testpkg-version").write_text("foo 3.1\n")
    result = make_result(testpkg_version=None)
    writer._generate_artifacts_from_result(result)
    assert result.testpkg_version == "foo 3.1"


def test_generate_testpkg_version_fallback_synthesized(make_result, out_dir):
    writer = make_writer(None, out_dir)
    result = make_result(request_kwargs={"package": "foo"}, testpkg_version=None)
    writer._generate_artifacts_from_result(result)
    assert result.testpkg_version == "foo unknown"


# --------------------------------------------------------------------------- #
# _pack_artifacts_for_upload
#
# NOTE: these tests shell out to the real ``tar`` and ``gzip`` binaries rather
# than mocking subprocess; the session-scoped require_tar_and_gzip fixture in
# conftest fails the suite up front if either binary is missing from PATH.
# --------------------------------------------------------------------------- #
def test_pack_creates_result_tar_log_gz_and_artifacts(out_dir):
    writer = make_writer(None, out_dir)
    (out_dir / "exitcode").write_text("0\n")
    (out_dir / "duration").write_text("1\n")
    (out_dir / "testpkg-version").write_text("p 1\n")
    (out_dir / "log").write_text("log line\n")
    (out_dir / "extra").write_text("loose\n")

    writer._pack_artifacts_for_upload()

    assert (out_dir / "result.tar").exists()
    assert (out_dir / "log.gz").exists()
    assert not (out_dir / "log").exists()
    assert (out_dir / "artifacts.tar.gz").exists()
    # loose files were removed after being packed
    assert not (out_dir / "exitcode").exists()
    assert not (out_dir / "extra").exists()
    # result.tar carries the result-tarball files
    with tarfile.open(out_dir / "result.tar") as tar:
        assert "exitcode" in tar.getnames()


def test_pack_preserves_log_gz_and_readable_by(out_dir):
    writer = make_writer(None, out_dir)
    # a result-tarball file is always present in practice (autopkgtest emits exitcode)
    (out_dir / "exitcode").write_text("0\n")
    (out_dir / "log").write_text("x\n")
    (out_dir / "readable-by").write_text("team\n")
    writer._pack_artifacts_for_upload()
    # log is gzipped and uploaded separately, readable-by is uploaded separately:
    # neither may be swept into artifacts.tar.gz
    assert (out_dir / "log.gz").exists()
    assert not (out_dir / "log").exists()
    assert (out_dir / "readable-by").exists()


def test_pack_raises_without_result_tarball_files(out_dir):
    # real `tar -cf` refuses to build an empty archive; the method has no
    # result-tarball files to pack, so it surfaces a CalledProcessError.
    writer = make_writer(None, out_dir)
    (out_dir / "log").write_text("x\n")
    (out_dir / "readable-by").write_text("team\n")
    with pytest.raises(subprocess.CalledProcessError):
        writer._pack_artifacts_for_upload()


def test_pack_removes_leftover_directories(out_dir):
    writer = make_writer(None, out_dir)
    (out_dir / "exitcode").write_text("0\n")
    (out_dir / "log").write_text("x\n")
    subdir = out_dir / "somedir"
    subdir.mkdir()
    (subdir / "f").write_text("y\n")
    writer._pack_artifacts_for_upload()
    # leftover directories are rmtree'd after being packed into artifacts.tar.gz
    assert (out_dir / "artifacts.tar.gz").exists()
    assert not subdir.exists()


# --------------------------------------------------------------------------- #
# get_logtail / get_testpkg_version
# --------------------------------------------------------------------------- #
def test_logtail_missing_file(out_dir):
    writer = make_writer(None, out_dir)
    assert writer.get_logtail() == "Log file not found.\n"


def test_logtail_small_file(out_dir):
    writer = make_writer(None, out_dir)
    (out_dir / "log").write_text("line1\nline2\nline3\n")
    logtail = writer.get_logtail()
    assert "line1" in logtail
    assert "🠉 HEAD" in logtail


def test_logtail_empty_file(out_dir):
    writer = make_writer(None, out_dir)
    (out_dir / "log").write_bytes(b"")
    assert writer.get_logtail() == "[... 🠉 HEAD 🠉 ... 🠋 TAIL 🠋 ...]\n"


def test_logtail_large_file(out_dir):
    writer = make_writer(None, out_dir)
    body = "".join(f"line{i}\n" for i in range(1000))  # >> 2000 bytes
    (out_dir / "log").write_text(body)
    logtail = writer.get_logtail()
    assert "line0" in logtail  # head
    assert "🠉 HEAD" in logtail
    assert "line999" in logtail  # tail
    assert len(logtail) < len(body)


def test_get_testpkg_version_present(out_dir):
    writer = make_writer(None, out_dir)
    (out_dir / "testpkg-version").write_text("  pkg 1.2  \n")
    assert writer.get_testpkg_version() == "pkg 1.2"


def test_get_testpkg_version_missing(out_dir):
    writer = make_writer(None, out_dir)
    with pytest.raises(FileNotFoundError, match="testpkg-version file not found"):
        writer.get_testpkg_version()


# --------------------------------------------------------------------------- #
# write_artifacts
# --------------------------------------------------------------------------- #
def test_write_artifacts_no_swift_skips_upload(
    make_result, out_dir, put_object_recorder
):
    writer = make_writer(None, out_dir)
    writer.write_artifacts(make_result(logs="some log\n"))
    assert put_object_recorder == []


def test_write_artifacts_uploads_expected(
    make_result, fake_swift_conn, out_dir, put_object_recorder
):
    writer = make_writer(fake_swift_conn, out_dir)
    writer.write_artifacts(make_result(logs="some log\n"))

    names = [c["name"].split("/")[-1] for c in put_object_recorder]
    assert "result.tar" in names
    assert "log.gz" in names
    assert "artifacts.tar.gz" in names
    # readable-by absent in this result -> not uploaded (else-branch)
    assert "readable-by" not in names
    # container was ensured (head_container called during _ensure_container)
    assert fake_swift_conn.headed


def test_write_artifacts_uploads_readable_by_when_present(
    make_result, fake_swift_conn, out_dir, put_object_recorder
):
    writer = make_writer(fake_swift_conn, out_dir)
    result = make_result(
        request_kwargs={"readable-by": ["team-a", "team-b"]}, logs="some log\n"
    )
    writer.write_artifacts(result)

    names = [c["name"].split("/")[-1] for c in put_object_recorder]
    # readable-by is populated on the request -> uploaded separately (if-branch)
    assert "readable-by" in names


def test_write_artifacts_log_gz_special_headers(
    make_result, fake_swift_conn, out_dir, put_object_recorder
):
    writer = make_writer(fake_swift_conn, out_dir)
    writer.write_artifacts(make_result(logs="some log\n"))
    log_call = next(c for c in put_object_recorder if c["name"].endswith("log.gz"))
    assert log_call["headers"] == {"Content-Encoding": "gzip"}
    assert log_call["content_type"] == "text/plain; charset=UTF-8"


def test_write_artifacts_non_log_no_special_headers(
    make_result, fake_swift_conn, out_dir, put_object_recorder
):
    writer = make_writer(fake_swift_conn, out_dir)
    writer.write_artifacts(make_result(logs="some log\n"))
    tar_call = next(c for c in put_object_recorder if c["name"].endswith("result.tar"))
    assert tar_call["headers"] is None
    assert tar_call["content_type"] is None
