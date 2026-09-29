"""
Phase 0 wait-profiling tests.

These lock in the guarantees the measurement pass must not break:

  1. The @profiled decorator is TRANSPARENT -- same args, same return
     value, same exceptions, same __name__/__doc__/signature. Wrapping a
     wait helper must never be able to change what the automation does.
  2. Accounting is correct on EVERY path: success, exception, and early
     return. A wait that is only recorded on the happy path would make
     the very numbers Phase 0 relies on wrong exactly when something is
     going wrong.
  3. The Phase 1 dead-poll removal preserved semantics: a page-scope
     (no .element_handle) _wait_for_option_render still runs the same
     "2 consecutive equal reads" loop at the same 25ms poll, and a
     Locator scope still takes the in-page path.
  4. Accounting is OPT-IN -- off by default, so an ordinary run pays nothing
     for numbers nobody reads -- and disabling it via WW_WAIT_PROFILE=0 is a
     true no-op on behaviour.

Run:  python -m pytest -q apps/sniper/product_upload/test_wait_profile.py
"""

import inspect

import pytest

from apps.sniper.product_upload import wait_profile
from apps.sniper.product_upload.wait_profile import WaitProfile


@pytest.fixture(autouse=True)
def _clean_profile():
    """Reset the process-wide profile around every test AND force it ON.

    PERF (Sep 2026): accounting is now opt-in (off by default -- see
    wait_profile's module docstring), but THESE tests are specifically about
    the accounting, so they must not depend on an ambient WW_WAIT_PROFILE.
    The previous value is restored afterwards, which is what lets a test still
    assert the disabled behaviour explicitly.
    """
    previous = wait_profile.PROFILE.enabled
    wait_profile.PROFILE.enabled = True
    wait_profile.reset()
    yield
    wait_profile.reset()
    wait_profile.PROFILE.enabled = previous


# --------------------------------------------------------------------------
# The decorator is transparent
# --------------------------------------------------------------------------

def test_profiled_preserves_return_value_and_metadata():
    @wait_profile.profiled("unit_ok")
    def add(a, b=2):
        """docstring here"""
        return a + b

    assert add(3) == 5
    assert add(3, b=10) == 13
    assert add.__name__ == "add"
    assert add.__doc__ == "docstring here"
    assert str(inspect.signature(add)) == "(a, b=2)"
    assert wait_profile.PROFILE.snapshot()["kinds_ms"].get("unit_ok") is not None


def test_profiled_records_on_exception_and_still_raises():
    @wait_profile.profiled("unit_boom")
    def boom():
        raise ValueError("nope")

    with pytest.raises(ValueError, match="nope"):
        boom()
    # recorded AND propagated -- both must happen
    assert "unit_boom" in wait_profile.PROFILE.snapshot()["kinds_ms"]
    assert wait_profile.PROFILE.snapshot()["calls"] == 1


def test_profiling_is_cheap_enough_to_leave_on():
    """The accounting must be a dict update, not a syscall or a write.
    Guard against someone 'optimising' it into per-call I/O later."""
    prof = WaitProfile(enabled=True)
    for _ in range(1000):
        prof.add("k", 1.0)
    snap = prof.snapshot()
    assert snap["calls"] == 1000
    assert snap["kinds_ms"]["k"] == 1000.0


def test_disabled_profile_is_a_noop(monkeypatch):
    prof = WaitProfile()
    prof.enabled = False

    @wait_profile.profiled("unit_off")
    def still_works(x):
        return x * 2

    original = wait_profile.PROFILE
    monkeypatch.setattr(wait_profile, "PROFILE", prof)
    try:
        assert still_works(21) == 42      # behaviour unchanged
        assert prof.snapshot()["total_ms"] == 0.0   # nothing recorded
    finally:
        monkeypatch.setattr(wait_profile, "PROFILE", original)


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------

def test_render_lines_reports_share_and_verdict():
    prof = WaitProfile(enabled=True)
    prof.add("a", 1000.0)
    prof.add("b", 500.0)
    lines = prof.render_lines(order_ms=10000.0)
    text = "\n".join(lines)
    assert "15.0% of it" in text          # 1500ms of 10000ms
    assert "VERDICT (15.0%)" in text
    assert "material" in text             # 10% <= 15% < 30%
    assert lines[1].split()[1] == "a"     # biggest kind listed first


def test_verdict_says_not_bottleneck_below_10pct():
    prof = WaitProfile(enabled=True)
    prof.add("a", 500.0)
    assert "NOT the bottleneck" in "\n".join(prof.render_lines(10000.0))


def test_verdict_says_dominant_above_30pct():
    prof = WaitProfile(enabled=True)
    prof.add("a", 9000.0)
    assert "DOMINANT" in "\n".join(prof.render_lines(10000.0))


def test_render_lines_empty_when_disabled_or_unused():
    # Both halves are asserted deliberately: an ENABLED profile with nothing
    # recorded renders nothing, and a disabled one renders nothing even after
    # data is added (see the opt-in default tests below).
    assert WaitProfile(enabled=True).render_lines(1000.0) == []
    p = WaitProfile(enabled=True)
    p.enabled = False
    p.add("x", 5.0)
    assert p.render_lines(1000.0) == []


# --------------------------------------------------------------------------
# PERF (Sep 2026): accounting is OPT-IN, and that default is guarded here so
# nobody quietly turns the overhead back on for every ordinary run.
# --------------------------------------------------------------------------

def test_profiling_is_off_by_default(monkeypatch):
    monkeypatch.delenv(wait_profile.ENV_FLAG, raising=False)
    assert WaitProfile().enabled is False


def test_env_var_can_turn_profiling_on_or_off(monkeypatch):
    monkeypatch.setenv(wait_profile.ENV_FLAG, "1")
    assert WaitProfile().enabled is True
    monkeypatch.setenv(wait_profile.ENV_FLAG, "0")
    assert WaitProfile().enabled is False


def test_explicit_argument_overrides_the_environment(monkeypatch):
    monkeypatch.setenv(wait_profile.ENV_FLAG, "0")
    assert WaitProfile(enabled=True).enabled is True
    monkeypatch.setenv(wait_profile.ENV_FLAG, "1")
    assert WaitProfile(enabled=False).enabled is False


def test_disabled_by_default_profile_records_nothing(monkeypatch):
    monkeypatch.delenv(wait_profile.ENV_FLAG, raising=False)
    prof = WaitProfile()
    prof.add("k", 5.0)
    assert prof.snapshot()["calls"] == 0
    assert prof.snapshot()["total_ms"] == 0.0


def test_snapshot_shape_is_stable():
    assert set(WaitProfile().snapshot()) == {
        "enabled", "total_ms", "calls", "kinds_ms", "counts"}


# --------------------------------------------------------------------------
# The instrumented wait primitives still behave identically
# --------------------------------------------------------------------------

def test_fast_waits_helpers_keep_their_signatures_and_are_wrapped():
    from apps.sniper.product_upload import fast_waits as fw
    for name in ("wait_value_equals", "wait_value_stable",
                 "wait_enabled_stable", "wait_enabled_now",
                 "wait_count_stable", "wait_text_matches",
                 "wait_box_stable", "wait_value_shorter"):
        fn = getattr(fw, name)
        assert hasattr(fn, "__wrapped__"), f"{name} lost its wrapper"
        assert fn.__name__ == name
        assert fn.__doc__, f"{name} lost its docstring"


def test_python_fallbacks_are_tagged_so_silent_fallback_is_visible():
    from apps.sniper.product_upload import fast_waits as fw
    for name in ("_py_wait_value_equals", "_py_wait_value_stable",
                 "_py_wait_enabled_stable", "_py_wait_count_stable",
                 "_py_wait_text_matches", "_py_wait_box_stable",
                 "_py_wait_value_shorter"):
        assert hasattr(getattr(fw, name), "__wrapped__"), f"{name} not instrumented"


# --------------------------------------------------------------------------
# Phase 1: the dead page-scope poll removal kept identical semantics
# --------------------------------------------------------------------------

class _FakeLocator:
    """Locator stand-in: HAS element_handle, so it takes the in-page path."""

    def __init__(self):
        self.handles = 0

    def element_handle(self, timeout=None):
        self.handles += 1
        raise RuntimeError("no handle in this fake")


class _CountingScope:
    """Scope stand-in with .locator() but NO .element_handle -- exactly the
    page-shaped scope whose hand-rolled poll Phase 1 removed."""

    def __init__(self, counts):
        self._counts = list(counts)
        self.reads = 0

    def locator(self, selector):
        outer = self

        class _L:
            def count(self_inner):
                outer.reads += 1
                idx = min(outer.reads - 1, len(outer._counts) - 1)
                return outer._counts[idx]
        return _L()


class _Cfg:
    option_render_wait_ms = 1234


def test_wait_for_option_render_page_scope_uses_the_one_shared_path(monkeypatch):
    """A page-scope render wait must now go through fast_waits (so it is
    instrumented) with the SAME 25ms poll the removed loop hardcoded."""
    from apps.sniper.product_upload import dialog_controls as dc
    from apps.sniper.product_upload import fast_waits as fw

    captured = {}

    def fake(scope, child_selector, timeout_ms, min_stable_ms=50, poll_ms=25):
        captured.update(scope=scope, child_selector=child_selector,
                        timeout_ms=timeout_ms, poll_ms=poll_ms)

    monkeypatch.setattr(fw, "wait_count_stable", fake)
    page = _CountingScope([0, 0, 0])
    dc._wait_for_option_render(page, _Cfg())

    assert captured["scope"] is page
    assert captured["timeout_ms"] == 1234
    assert captured["poll_ms"] == 25


def test_wait_for_option_render_locator_scope_keeps_stability_params(monkeypatch):
    from apps.sniper.product_upload import dialog_controls as dc
    from apps.sniper.product_upload import fast_waits as fw

    seen = {}

    def fake(scope, child_selector, timeout_ms, min_stable_ms=50, poll_ms=25):
        seen.update(min_stable_ms=min_stable_ms, poll_ms=poll_ms)

    monkeypatch.setattr(fw, "wait_count_stable", fake)
    dc._wait_for_option_render(_FakeLocator(), _Cfg())
    assert seen == {"min_stable_ms": 50, "poll_ms": 25}


def test_py_count_stable_still_requires_two_consecutive_equal_reads():
    """The fallback the page scope now delegates to must keep the
    '2 stable reads' exit. Settling on ONE read could latch onto a
    half-rendered option list -- the exact bug the window prevents."""
    from apps.sniper.product_upload import fast_waits as fw

    scope = _CountingScope([5, 5, 5])
    fw._py_wait_count_stable(scope, "li", timeout_ms=2000, poll_ms=1)
    assert scope.reads == 3, "settled before two consecutive equal reads"


def test_py_count_stable_resets_when_the_count_keeps_changing():
    from apps.sniper.product_upload import fast_waits as fw

    scope = _CountingScope([1, 2, 3, 4, 5, 6, 7, 8])
    # FIX (Sep 2026, flaky under load): the budget was 30ms with a 1ms poll,
    # which only guaranteed 3 reads on an IDLE machine. time.sleep(0.001)
    # costs ~1.6ms idle but can stretch to ~15ms while the machine is busy
    # (measured here: one full-suite run took 16.95s where a later one took
    # 3.48s), so the loop legitimately exited after 2 reads and this failed
    # for a reason that has nothing to do with the behaviour it checks. A
    # 200ms budget keeps the assertion (>= 3 reads, i.e. it did NOT settle
    # early on a list whose count keeps changing) exactly as strict while
    # making it robust to that sleep granularity.
    fw._py_wait_count_stable(scope, "li", timeout_ms=200, poll_ms=1)
    assert scope.reads >= 3


# --------------------------------------------------------------------------
# Sub-phase timings do not disturb the coarse-phase tiling
# --------------------------------------------------------------------------

def test_summary_reports_phases_and_substeps_separately(capsys):
    """Sub-steps are nested INSIDE the coarse phases. The summary must not
    fold them into the total (that would double-count) and must not lose
    them either -- they are the whole point of Phase 0."""
    from apps.sniper.run_upload import print_performance_summary

    counters = {"timings_ms": [
        {"total_ms": 1000.0, "product_lines_ms": 400.0,
         "charges_and_customer_ms": 300.0, "submit_ms": 200.0,
         "products_screen_ms": 380.0, "combobox_ms": 90.0},
        {"total_ms": 2000.0, "product_lines_ms": 800.0,
         "charges_and_customer_ms": 600.0, "submit_ms": 400.0,
         "products_screen_ms": 760.0, "combobox_ms": 180.0},
    ]}
    print_performance_summary(counters)
    out = capsys.readouterr().out

    assert "orders=2" in out
    # per-phase share, and the phases must sum to ~100% of the order
    assert "product_lines_ms" in out
    assert "% of order" in out
    # sub-steps present but explicitly labelled as non-additive
    assert "products_screen_ms" in out
    assert "not additive" in out


def test_summary_uses_total_ms_not_the_sum_of_everything(capsys):
    """Regression guard: the old summary summed every value in the timing
    dict, which double-counted because total_ms sat alongside its own
    phases. The average must reflect the real per-order time."""
    from apps.sniper.run_upload import print_performance_summary

    counters = {"timings_ms": [
        {"total_ms": 1000.0, "product_lines_ms": 400.0,
         "charges_and_customer_ms": 300.0, "submit_ms": 200.0},
    ]}
    print_performance_summary(counters)
    out = capsys.readouterr().out
    assert "avg=1000.0" in out      # not avg=1900.0 (the double-counted sum)


def test_summary_survives_empty_and_partial_samples(capsys):
    from apps.sniper.run_upload import print_performance_summary

    print_performance_summary({})                        # no samples at all
    print_performance_summary({"timings_ms": [{}]})       # empty timing dict
    print_performance_summary({"timings_ms": [{"total_ms": 5.0}]})
    assert "orders=1" in capsys.readouterr().out
