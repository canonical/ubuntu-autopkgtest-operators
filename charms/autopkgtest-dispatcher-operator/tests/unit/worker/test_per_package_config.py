"""Tests for worker.runner.PerPackageConfig."""

from __future__ import annotations

import os

import pytest
from worker.runner import PerPackageConfig


def write_ppc(path, *lines):
    path.write_text("\n".join(lines) + ("\n" if lines else ""))
    return path


# --------------------------------------------------------------------------- #
# parsing / normalization
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        ([], set()),
        (["foo"], {"foo/all/all"}),
        (["foo/amd64"], {"foo/amd64/all"}),
        (["foo/amd64/noble"], {"foo/amd64/noble"}),
        (["# a comment", "foo"], {"foo/all/all"}),
        (["", "   ", "foo"], {"foo/all/all"}),
        (["  foo  "], {"foo/all/all"}),
        (["foo", "bar/arm64"], {"foo/all/all", "bar/arm64/all"}),
    ],
)
def test_parse(tmp_path, lines, expected):
    cfg = PerPackageConfig(write_ppc(tmp_path / "f", *lines))
    assert cfg.contents == expected


# --------------------------------------------------------------------------- #
# matches
# --------------------------------------------------------------------------- #
def test_matches_bare_package_matches_any_arch_release(tmp_path):
    cfg = PerPackageConfig(write_ppc(tmp_path / "f", "foo"))
    assert cfg.matches("foo", "amd64", "noble") is True
    assert cfg.matches("bar", "amd64", "noble") is False


def test_matches_arch_specific(tmp_path):
    cfg = PerPackageConfig(write_ppc(tmp_path / "f", "foo/amd64"))
    assert cfg.matches("foo", "amd64", "noble") is True
    assert cfg.matches("foo", "arm64", "noble") is False


def test_matches_arch_and_release_specific(tmp_path):
    cfg = PerPackageConfig(write_ppc(tmp_path / "f", "foo/amd64/noble"))
    assert cfg.matches("foo", "amd64", "noble") is True
    assert cfg.matches("foo", "amd64", "jammy") is False


def test_matches_star_glob(tmp_path):
    cfg = PerPackageConfig(write_ppc(tmp_path / "f", "foo*"))
    assert cfg.matches("foobar", "amd64", "noble") is True
    assert cfg.matches("bar", "amd64", "noble") is False


def test_matches_question_glob(tmp_path):
    cfg = PerPackageConfig(write_ppc(tmp_path / "f", "foo?"))
    assert cfg.matches("foox", "amd64", "noble") is True
    assert cfg.matches("fooxy", "amd64", "noble") is False


def test_matches_empty_contents(tmp_path):
    cfg = PerPackageConfig(write_ppc(tmp_path / "f"))
    assert cfg.matches("foo", "amd64", "noble") is False


def test_matches_multiple_patterns(tmp_path):
    cfg = PerPackageConfig(write_ppc(tmp_path / "f", "foo", "bar/arm64/jammy"))
    assert cfg.matches("bar", "arm64", "jammy") is True


# --------------------------------------------------------------------------- #
# refresh
# --------------------------------------------------------------------------- #
def test_refresh_no_change_when_mtime_unchanged(tmp_path):
    path = write_ppc(tmp_path / "f", "foo")
    cfg = PerPackageConfig(path)
    mtime = path.stat().st_mtime
    # change content but force mtime to stay the same -> refresh must NOT reparse
    write_ppc(path, "bar")
    os.utime(path, (mtime, mtime))
    cfg.refresh()
    assert cfg.contents == {"foo/all/all"}


def test_refresh_reparses_when_mtime_changes(tmp_path):
    path = write_ppc(tmp_path / "f", "foo")
    cfg = PerPackageConfig(path)
    old_mtime = path.stat().st_mtime
    write_ppc(path, "bar")
    os.utime(path, (old_mtime + 10, old_mtime + 10))
    cfg.refresh()
    assert cfg.contents == {"bar/all/all"}
