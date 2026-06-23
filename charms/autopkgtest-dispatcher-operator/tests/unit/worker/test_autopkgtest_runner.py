"""Tests for worker.adapters.AutopkgtestRunner."""

from __future__ import annotations

import subprocess
import time

import pytest
from worker.adapters import AutopkgtestRunner


def make_runner(popen, *, sleep=None, now=None):
    return AutopkgtestRunner(
        popen=popen,
        sleep=sleep or (lambda s: None),
        now=now or (lambda: 0),
    )


def test_di_defaults_use_stdlib():
    runner = AutopkgtestRunner()
    assert runner._popen is subprocess.Popen
    assert runner._sleep is time.sleep
    assert runner._now is time.time


@pytest.mark.parametrize("code", [0, 2, 16])
def test_run_returns_exit_code_and_duration(make_popen, code):
    runner = make_runner(make_popen(returncode=code))
    assert runner.run(["autopkgtest", "foo"]) == (code, 0)


def test_run_exit_code_1_raises_runtimeerror(make_popen):
    runner = make_runner(make_popen(returncode=1))
    with pytest.raises(RuntimeError, match="unexpected exit code 1"):
        runner.run(["autopkgtest"])


@pytest.mark.parametrize("poll_none_times", [0, 1, 5])
def test_sleep_called_once_per_poll_iteration(make_popen, poll_none_times):
    sleeps = []
    runner = make_runner(
        make_popen(poll_none_times=poll_none_times), sleep=sleeps.append
    )
    runner.run(["x"])
    assert sleeps == [AutopkgtestRunner.POLL_INTERVAL_SECONDS] * poll_none_times


def test_on_status_absent_is_fine(make_popen):
    runner = make_runner(make_popen(poll_none_times=2))
    assert runner.run(["x"]) == (0, 0)


def test_on_status_called_once_when_process_exits_immediately(make_popen):
    calls = []
    runner = make_runner(make_popen(poll_none_times=0))
    runner.run(["x"], on_status=lambda d, running: calls.append((d, running)))
    assert calls == [(0, False)]


def test_on_status_called_per_iteration_then_final(make_popen):
    running_flags = []
    runner = make_runner(make_popen(poll_none_times=2))
    runner.run(["x"], on_status=lambda d, running: running_flags.append(running))
    assert running_flags == [True, True, False]


def test_duration_is_int_difference_of_now(make_popen):
    nows = iter([1000, 1042])
    runner = make_runner(make_popen(poll_none_times=0), now=lambda: next(nows))
    _, duration = runner.run(["x"])
    assert duration == 42


def test_in_loop_status_duration_progresses(make_popen):
    nows = iter([100, 103, 107, 110])
    calls = []
    runner = make_runner(make_popen(poll_none_times=2), now=lambda: next(nows))
    runner.run(["x"], on_status=lambda d, running: calls.append((d, running)))
    assert calls == [(3, True), (7, True), (10, False)]


def test_popen_invoked_with_devnull_and_stdout_redirect(make_popen):
    popen = make_popen(poll_none_times=0)
    make_runner(popen).run(["autopkgtest", "--foo"])
    assert popen.calls.args == ["autopkgtest", "--foo"]
    assert popen.calls.kwargs["stdout"] is subprocess.DEVNULL
    assert popen.calls.kwargs["stderr"] is subprocess.STDOUT
