"""Tests for worker.models (pure domain logic)."""

from __future__ import annotations

import re

import pytest
from worker import models
from worker.models import PPA, Request, Result, get_package_hash


# --------------------------------------------------------------------------- #
# get_package_hash
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("package", "expected"),
    [
        ("foo", "f"),
        ("libfoo", "libf"),
        ("Foo", "F"),
        ("lib", "lib"),  # exact boundary: package[:4] of a 3-char string
        ("library", "libr"),
        ("libreoffice", "libr"),
    ],
)
def test_get_package_hash(package, expected):
    assert get_package_hash(package) == expected


# --------------------------------------------------------------------------- #
# PPA
# --------------------------------------------------------------------------- #
def test_ppa_public():
    ppa = PPA("user/ppa")
    assert ppa.user == "user"
    assert ppa.name == "ppa"
    assert ppa.creds_user is None
    assert ppa.creds_pass is None
    assert ppa.fingerprint == ""
    assert ppa.is_private() is False


def test_ppa_private_with_creds():
    ppa = PPA("alice:s3cret@user/ppa")
    assert ppa.creds_user == "alice"
    assert ppa.creds_pass == "s3cret"  # noqa: S105 - asserting parsed dummy value
    assert ppa.is_private() is True


def test_ppa_with_fingerprint():
    ppa = PPA("user/ppa:ABCDEF")
    assert ppa.fingerprint == "ABCDEF"
    assert ppa.is_private() is False


def test_ppa_private_with_fingerprint():
    ppa = PPA("alice:tok@user/ppa:FFFF")
    assert ppa.creds_user == "alice"
    assert ppa.fingerprint == "FFFF"
    assert ppa.is_private() is True


@pytest.mark.parametrize("bad", ["noslash", "onlyuser@user/ppa"])
def test_ppa_malformed_raises(bad):
    with pytest.raises(ValueError, match="invalid ppa format"):
        PPA(bad)


@pytest.mark.parametrize(
    "ppa_str",
    [
        "user/ppa",
        "user/ppa:ABCDEF",
        "alice:tok@user/ppa",
        "alice:tok@user/ppa:FFFF",
    ],
)
def test_ppa_str_roundtrip(ppa_str):
    assert str(PPA(ppa_str)) == ppa_str


# --------------------------------------------------------------------------- #
# Request construction / validation
# --------------------------------------------------------------------------- #
def test_request_minimal_defaults(make_request):
    req = make_request()
    assert req.package == "testpkg"
    assert req.env == []
    assert req.triggers == []
    assert req.requester == ""
    assert req.readable_by == []
    assert req.ppas == []
    assert req.all_proposed is False
    assert req.is_big_pkg is False
    # all per-package classification flags default off
    assert req.is_long_test is False
    assert req.is_vm_pkg is False
    assert req.is_never_run is False
    assert req.run_id.endswith("@")


@pytest.mark.parametrize("bad", [None, "", "has space", "weird@chars"])
def test_request_invalid_package_raises(bad, frozen_clock):
    kwargs = {"arch": "amd64", "release": "noble"}
    if bad is not None:
        kwargs["package"] = bad
    with pytest.raises(ValueError, match="invalid package name"):
        Request(**kwargs)


def test_request_private_ppa_without_swiftuser_raises(frozen_clock):
    with pytest.raises(ValueError, match="must also have a swiftuser"):
        Request(package="foo", arch="amd64", release="noble", ppas=["a:tok@user/ppa"])


def test_request_private_ppa_with_swiftuser_ok(frozen_clock):
    req = Request(
        package="foo",
        arch="amd64",
        release="noble",
        ppas=["a:tok@user/ppa"],
        swiftuser="alice",
    )
    assert req.is_private() is True
    assert len(req.ppas) == 1


def test_request_all_proposed_is_key_presence(make_request):
    # all_proposed is driven by key presence, not truthiness of value
    req = make_request(**{"all-proposed": ""})
    assert req.all_proposed is True


def test_request_dashed_keys_mapped(make_request):
    req = make_request(
        **{
            "readable-by": ["team"],
            "test-git": "git-url",
            "build-git": "build-url",
            "submit-time": "2026-01-01",
        }
    )
    assert req.readable_by == ["team"]
    assert req.test_git == "git-url"
    assert req.build_git == "build-url"
    assert req.submit_time == "2026-01-01"


# --------------------------------------------------------------------------- #
# Request.__str__ / run_id
# --------------------------------------------------------------------------- #
def test_request_str_excludes_run_id_and_is_sorted(make_request):
    body = str(make_request())
    assert "run_id=" not in body
    # deterministic, sorted by key: arch comes before package
    assert body.index("arch=") < body.index("package=")


def test_run_id_uses_frozen_clock(make_request, frozen_clock):
    req = make_request()
    assert req.run_id.startswith(frozen_clock + "_")
    assert re.fullmatch(rf"{frozen_clock}_[0-9a-f]{{5}}@", req.run_id)


# --------------------------------------------------------------------------- #
# Request small accessors
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("swiftuser", "expected"),
    [("", False), ("alice", True)],
)
def test_request_is_private(make_request, swiftuser, expected):
    assert make_request(swiftuser=swiftuser).is_private() is expected


def test_get_skip_reason_never_run(make_request):
    assert make_request(is_never_run=True).get_skip_reason() is not None


def test_get_skip_reason_runnable(make_request):
    assert make_request().get_skip_reason() is None


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({}, "autopkgtest-noble"),
        ({"ppas": ["user/ppa"]}, "autopkgtest-noble-user-ppa"),
        ({"swiftuser": "alice"}, "private-autopkgtest-noble"),
        (
            {"ppas": ["a:tok@user/ppa"], "swiftuser": "alice"},
            "private-autopkgtest-noble-user-ppa",
        ),
    ],
)
def test_get_container_name(make_request, kwargs, expected):
    assert make_request(**kwargs).get_container_name() == expected


def test_get_swift_dir(make_request):
    req = make_request(package="foo")
    assert req.get_swift_dir() == f"noble/amd64/f/foo/{req.run_id}"


# --------------------------------------------------------------------------- #
# compute_timeouts
# --------------------------------------------------------------------------- #
def test_compute_timeouts_default(make_request):
    assert make_request().compute_timeouts() == models.TIMEOUTS


def test_compute_timeouts_riscv64(make_request):
    timeouts = make_request(arch="riscv64").compute_timeouts()
    assert timeouts == {k: v * 4 for k, v in models.TIMEOUTS.items()}


def test_compute_timeouts_long_test(make_request):
    timeouts = make_request(is_long_test=True).compute_timeouts()
    assert timeouts["test"] == models.TIMEOUTS["test"] * 4
    assert timeouts["build"] == models.TIMEOUTS["build"] * 2


def test_compute_timeouts_big_pkg(make_request):
    timeouts = make_request(is_big_pkg=True).compute_timeouts()
    assert timeouts["test"] == models.TIMEOUTS["test"] * 2
    assert timeouts["build"] == models.TIMEOUTS["build"]


def test_compute_timeouts_long_test_precedence_over_big(make_request):
    # is_long_test wins over is_big_pkg (elif), so test is x4 not x2
    timeouts = make_request(is_long_test=True, is_big_pkg=True).compute_timeouts()
    assert timeouts["test"] == models.TIMEOUTS["test"] * 4


def test_compute_timeouts_riscv64_and_long_test_compound(make_request):
    timeouts = make_request(arch="riscv64", is_long_test=True).compute_timeouts()
    assert timeouts["test"] == models.TIMEOUTS["test"] * 4 * 4
    assert timeouts["build"] == models.TIMEOUTS["build"] * 4 * 2


def test_compute_timeouts_returns_fresh_copy(make_request):
    # compute_timeouts must hand back a fresh dict, never the shared module
    # constant, so callers cannot mutate TIMEOUTS for every later request
    timeouts = make_request().compute_timeouts()
    assert timeouts == models.TIMEOUTS
    assert timeouts is not models.TIMEOUTS
    timeouts["test"] = -1
    assert models.TIMEOUTS["test"] != -1


# --------------------------------------------------------------------------- #
# get_params
# --------------------------------------------------------------------------- #
def test_get_params_empty(make_request):
    assert make_request().get_params() == {}


def test_get_params_all_fields(make_request):
    req = make_request(
        requester="bob",
        triggers=["t/1"],
        env=["A=1"],
        ppas=["user/ppa"],
        testname="mytest",
        **{
            "submit-time": "2026",
            "all-proposed": "x",
            "test-git": "g",
            "build-git": "b",
        },
    )
    params = req.get_params()
    assert params["requester"] == "bob"
    assert params["submit-time"] == "2026"
    assert params["triggers"] == ["t/1"]
    assert params["all-proposed"] == "1"
    assert params["test-git"] == "g"
    assert params["build-git"] == "b"
    assert params["ppas"] == ["user/ppa"]
    assert params["env"] == ["A=1"]
    assert params["testname"] == "mytest"


def test_get_params_list_values_are_copies(make_request):
    req = make_request(triggers=["t/1"], env=["A=1"])
    params = req.get_params()
    assert params["triggers"] is not req.triggers
    assert params["env"] is not req.env


# --------------------------------------------------------------------------- #
# build_args
# --------------------------------------------------------------------------- #
def test_build_args_default(make_request):
    args = make_request(package="foo").build_args()
    assert args == [
        "--apt-upgrade",
        "foo",
        "--timeout-short=300",
        "--timeout-install=3000",
        "--timeout-test=10000",
        "--timeout-copy=900",
        "--timeout-build=20000",
    ]


def test_build_args_i386_prepends_test_architecture(make_request):
    args = make_request(arch="i386").build_args()
    assert args[0] == "--test-architecture=i386"


def test_build_args_apt_upgrade_always_present(make_request):
    assert "--apt-upgrade" in make_request().build_args()


def test_build_args_no_pocket_without_triggers(make_request):
    # no triggers + no ppas => pocket_arg collapses to "" and is omitted
    args = make_request().build_args()
    assert not any(a.startswith("--apt-pocket") for a in args)


def test_build_args_pocket_with_triggers_filters_migration_reference(make_request):
    args = make_request(
        triggers=["foo/1.0", "migration-reference/0", "bar/2"]
    ).build_args()
    assert "--apt-pocket=proposed=src:foo,src:bar" in args


def test_build_args_all_proposed_pocket_without_trigger_suffix(make_request):
    args = make_request(**{"all-proposed": "1"}).build_args()
    assert "--apt-pocket=proposed" in args


def test_build_args_ppas_suppress_pocket_and_add_source(make_request):
    args = make_request(ppas=["user/ppa"]).build_args()
    assert not any(a.startswith("--apt-pocket") for a in args)
    assert "--add-apt-source=ppa:user/ppa" in args


def test_build_args_test_git(make_request):
    args = make_request(**{"test-git": "git-url"}).build_args()
    assert "--no-built-binaries" in args
    assert "git-url" in args
    assert not any(a.startswith("--apt-pocket") for a in args)


def test_build_args_build_git(make_request):
    args = make_request(**{"build-git": "build-url"}).build_args()
    assert "build-url" in args
    assert "--no-built-binaries" not in args


def test_build_args_env_and_testname_and_triggers_env(make_request):
    args = make_request(
        env=["FOO=bar", "BAZ=1"], testname="mytest", triggers=["t/1"]
    ).build_args()
    assert "--env=FOO=bar" in args
    assert "--env=BAZ=1" in args
    assert "--testname=mytest" in args
    assert "--env=ADT_TEST_TRIGGERS=t/1" in args


# --------------------------------------------------------------------------- #
# Result
# --------------------------------------------------------------------------- #
def test_result_optional_fields_default_none(make_request):
    result = Result(
        request=make_request(),
        container="c",
        run_id="r",
        duration=1,
        exitcode=0,
    )
    assert result.testinfo is None
    assert result.testpkg_version is None
    assert result.logs is None


def test_mock_result_shape(make_request):
    req = make_request(package="foo", triggers=["t/1"])
    result = Result.mock_result(req, "skipped because reasons")
    assert result.exitcode == 99
    assert result.duration == 0
    assert result.logs == "skipped because reasons"
    assert result.run_id == req.run_id
    assert result.container == req.get_container_name()
    assert result.testpkg_version == "foo blacklisted"
    assert result.testinfo["custom_environment"] == ["ADT_TEST_TRIGGERS=t/1"]
