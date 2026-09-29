"""Tests for the bounded client-process scheduler (no browser or CRM)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from apps.sniper.client_scheduler import (  # noqa: E402
    ClientProcessScheduler,
    ScheduledJob,
)


class _FakeProcess:
    def __init__(self, *, polls_before_exit: int = 0, returncode: int = 0):
        self._polls_before_exit = polls_before_exit
        self.returncode = returncode
        self.terminated = False

    def poll(self):
        if self.terminated:
            return -15
        if self._polls_before_exit > 0:
            self._polls_before_exit -= 1
            return None
        return self.returncode

    def terminate(self):
        self.terminated = True


class _SchedulerHarness:
    def __init__(self, max_parallel, durations=None, returncodes=None):
        self.max_parallel = max_parallel
        self.durations = durations or {}
        self.returncodes = returncodes or {}
        self.processes = {}
        self.start_order = []
        self.finish_order = []
        self.active_slots = 0
        self.max_active_slots = 0
        self.active_groups = set()

    def popen(self, command):
        key = command[-1]
        process = _FakeProcess(
            polls_before_exit=self.durations.get(key, 0),
            returncode=self.returncodes.get(key, 0),
        )
        self.processes[key] = process
        return process

    def on_start(self, job):
        self.start_order.append(job.key)
        self.active_slots += job.slots
        self.max_active_slots = max(self.max_active_slots, self.active_slots)
        assert job.group not in self.active_groups, (
            f"two jobs from group {job.group!r} ran concurrently"
        )
        self.active_groups.add(job.group)

    def on_finish(self, result):
        self.finish_order.append(result.job.key)
        self.active_slots -= result.job.slots
        self.active_groups.remove(result.job.group)

    def scheduler(self):
        return ClientProcessScheduler(
            self.max_parallel,
            popen=self.popen,
            sleep=lambda _seconds: None,
            on_start=self.on_start,
            on_finish=self.on_finish,
        )


def _job(key, group=None, slots=1):
    return ScheduledJob(
        key=key,
        group=group or key[0],
        command=["fake-python", "worker", key],
        slots=slots,
        label=key,
    )


def test_global_slot_limit_is_never_exceeded():
    harness = _SchedulerHarness(
        max_parallel=2,
        durations={"a": 3, "b": 1, "c": 0},
    )
    results = harness.scheduler().run([_job("a"), _job("b"), _job("c")])

    assert [result.job.key for result in results] == ["a", "b", "c"]
    assert harness.max_active_slots <= 2
    assert harness.start_order[0:2] == ["a", "b"]
    assert "c" not in harness.start_order[:2]


def test_one_job_per_client_group_runs_at_a_time():
    harness = _SchedulerHarness(
        max_parallel=4,
        durations={"a1": 3, "a2": 0, "b1": 0},
    )
    harness.scheduler().run([_job("a1"), _job("a2"), _job("b1")])

    assert harness.start_order == ["a1", "b1", "a2"]
    assert harness.max_active_slots <= 4


def test_round_robin_fairness_across_clients():
    harness = _SchedulerHarness(max_parallel=1)
    jobs = [
        _job("a1", group="a"),
        _job("a2", group="a"),
        _job("b1", group="b"),
        _job("b2", group="b"),
        _job("c1", group="c"),
    ]
    harness.scheduler().run(jobs)

    assert harness.start_order == ["a1", "b1", "c1", "a2", "b2"]


def test_results_are_returned_in_input_order():
    harness = _SchedulerHarness(
        max_parallel=2,
        durations={"a": 5, "b": 0},
    )
    results = harness.scheduler().run([_job("a"), _job("b")])

    assert [result.job.key for result in results] == ["a", "b"]
    assert harness.finish_order == ["b", "a"]


def test_zero_max_parallel_disables_the_cap():
    harness = _SchedulerHarness(max_parallel=0)
    harness.scheduler().run([_job("a"), _job("b"), _job("c")])

    assert harness.start_order == ["a", "b", "c"]
    assert harness.max_active_slots == 3


def test_empty_job_list_returns_immediately():
    assert ClientProcessScheduler(2).run([]) == []


def test_duplicate_job_key_is_rejected_before_launch():
    harness = _SchedulerHarness(max_parallel=2)
    with pytest.raises(ValueError, match="duplicate scheduled job key"):
        harness.scheduler().run([_job("a"), _job("a")])
    assert harness.start_order == []


def test_job_requiring_more_slots_than_limit_is_rejected():
    harness = _SchedulerHarness(max_parallel=2)
    with pytest.raises(ValueError, match="greater than max_parallel"):
        harness.scheduler().run([_job("a", slots=3)])
    assert harness.start_order == []


def test_negative_max_parallel_is_rejected():
    with pytest.raises(ValueError, match="max_parallel must be non-negative"):
        ClientProcessScheduler(-1)


def test_keyboard_interrupt_terminates_active_children():
    harness = _SchedulerHarness(max_parallel=1, durations={"a": 100})

    def raise_interrupt(_seconds):
        raise KeyboardInterrupt

    scheduler = ClientProcessScheduler(
        1,
        popen=harness.popen,
        sleep=raise_interrupt,
        on_start=harness.on_start,
        on_finish=harness.on_finish,
    )
    with pytest.raises(KeyboardInterrupt):
        scheduler.run([_job("a")])

    assert harness.processes["a"].terminated is True
