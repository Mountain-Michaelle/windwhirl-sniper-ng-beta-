"""
test_failed_ledger.py

Unit tests for the failed-orders ledger and the workspace completion
helpers added in Sep 2026 ("retry all failed uploads first on every fresh
run", "never mark a job completed while failed customers are yet to be
uploaded", "file in use" archive hardening, completed-job logging).

Pure file-IO tests -- no browser, no CRM.
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest

from apps.sniper.product_upload import failed_ledger as fl
from apps.sniper.product_upload.price_engine import (
    PriceVarOption, decide_price_offer,
)
from apps.sniper.client_workspace import WorkspaceManager


ORDER = {"customer_name": "Ada", "phone_number": "0801", "product": "Collagen",
         "price": "28500", "address": "Patani, Delta"}


# ----------------------------------------------------------------- ledger
def test_ledger_record_attempts_and_persistence(tmp_path):
    path = tmp_path / "failed_orders.json"
    led = fl.FailedLedger(path=str(path))
    assert led.count == 0 and led.pending_entries() == []

    entry = led.record_failure(ORDER, "network timeout", row_number=3)
    assert led.count == 1
    assert entry.attempts == 1 and entry.row_number == 3
    assert entry.reason == "network timeout"

    again = led.record_failure(ORDER, "network timeout", row_number=3)
    assert again.attempts == 2
    assert again.first_failed_at == entry.first_failed_at

    fresh = fl.FailedLedger(path=str(path))
    assert fresh.count == 1
    loaded = fresh.pending_entries()[0]
    assert loaded.reason == "network timeout" and loaded.attempts == 2
    assert fresh.get(entry.key) is not None


def test_ledger_remove_and_corrupt_tolerance(tmp_path):
    path = tmp_path / "failed_orders.json"
    led = fl.FailedLedger(path=str(path))
    led.record_failure(ORDER, "timeout")
    led.remove(ORDER)
    assert led.count == 0
    assert fl.FailedLedger(path=str(path)).count == 0

    path.write_text("{not json", encoding="utf-8")
    assert fl.FailedLedger(path=str(path)).count == 0


def test_ledger_shard_partition_and_merge(tmp_path):
    main_path = tmp_path / "failed_orders.json"
    main = fl.FailedLedger(path=str(main_path))
    for i in range(3):
        order = dict(ORDER, customer_name=f"C{i}", phone_number=f"080{i}")
        main.record_failure(order, "timeout")
    fl.partition_ledger_into_shards(tmp_path, main_path, 2)
    assert fl.FailedLedger(path=str(main_path)).count == 0
    shard_files = sorted(tmp_path.glob("failed_orders.shard*of*.json"))
    assert len(shard_files) == 2
    merged = fl.collect_and_merge_shard_ledgers(tmp_path, main_path)
    assert merged == 2
    assert fl.FailedLedger(path=str(main_path)).count == 3
    assert fl.collect_and_merge_shard_ledgers(tmp_path, main_path) == 0

# ----------------------------------------------------------- price engine
def test_price_not_in_dropdown_gets_custom_price_flag():
    opts = [PriceVarOption(label="bundle A", value="a", price=21500),
            PriceVarOption(label="bundle B", value="b", price=29500)]
    d = decide_price_offer(28500, opts)
    assert d.unresolvable and d.needs_custom_price
    # FIRST dropdown option wins even though bundle B (29500) is closer.
    assert (d.option_label, d.option_data_value, d.bundle_price) == ("bundle A", "a", 21500), d
    assert d.needs_amount_edit


def test_exact_and_trailing_digit_paths_unchanged():
    opts = [PriceVarOption(label="bundle A", value="a", price=21500),
            PriceVarOption(label="bundle B", value="b", price=29500)]
    exact = decide_price_offer(21500, opts)
    assert not exact.unresolvable and not exact.needs_custom_price
    assert not exact.needs_amount_edit

    trail = decide_price_offer(29599, opts)
    assert not trail.unresolvable and not trail.needs_custom_price
    assert trail.needs_amount_edit and trail.edit_suffix_len == 2


def test_degenerate_unresolvable_cases_do_not_maneuver():
    opts = [PriceVarOption(label="bundle A", value="a", price=21500)]
    bad = decide_price_offer("n/a", opts)
    assert bad.unresolvable and not bad.needs_custom_price
    empty = decide_price_offer(21500, [])
    assert empty.unresolvable and not empty.needs_custom_price


# ------------------------------------------------------------- workspace
def _make_job(root: Path, client: str, job: str, status: str,
              manifest_extras: dict | None = None) -> Path:
    job_dir = root / client / "jobs" / job
    (job_dir / "state").mkdir(parents=True, exist_ok=True)
    (job_dir / "source.csv").write_text("customer,phone,product,price\n", encoding="utf-8")
    manifest = {"status": status, "job_id": job, "client_id": client,
                "updated_at": "2026-09-18T10:00:00Z"}
    if manifest_extras:
        manifest.update(manifest_extras)
    (job_dir / "job.json").write_text(json.dumps(manifest), encoding="utf-8")
    return job_dir


@pytest.fixture()
def manager(tmp_path):
    reg = {"schema_version": 1, "name_keys": {"ada": ["client_ada_1234abcd"]}}
    (tmp_path / "_registry.json").write_text(json.dumps(reg), encoding="utf-8")
    (tmp_path / "_inbox" / ".claimed").mkdir(parents=True, exist_ok=True)
    return WorkspaceManager(root=tmp_path)


def test_archive_success_and_completed_summary(manager, tmp_path):
    _make_job(tmp_path, "client_ada_1234abcd", "job_ok", "completed",
              manifest_extras={"succeeded": 12, "failed": 0})
    dest = manager.archive_completed_job("client_ada_1234abcd", "job_ok")
    assert dest is not None and dest.is_dir()
    assert manager.read_job_status("client_ada_1234abcd", "job_ok") == "completed"
    summary = manager.completed_job_summary("client_ada_1234abcd", "job_ok")
    assert summary and "succeeded=12" in summary and "archived at" in summary
    assert not manager.job_has_pending_uploads("client_ada_1234abcd", "job_ok")


def test_archive_blocked_is_deferred_then_retried(manager, tmp_path):
    job_dir = _make_job(tmp_path, "client_ada_1234abcd", "job_busy", "completed")
    blocker = open(job_dir / "state" / "busy.db", "wb")  # holds the dir hostage
    try:
        assert manager.archive_completed_job("client_ada_1234abcd", "job_busy") is None
    finally:
        blocker.close()
    manifest = json.loads((job_dir / "job.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "completed" and manifest.get("archive_deferred")
    assert manager.retry_deferred_archives() == 1
    archived = tmp_path / "client_ada_1234abcd" / "jobs" / "archive" / "completed" / "job_busy"
    assert archived.is_dir()

def test_discover_includes_completed_with_failures_excludes_clean(manager, tmp_path):
    clean = _make_job(tmp_path, "client_ada_1234abcd", "job_clean", "completed")
    dirty_live = _make_job(tmp_path, "client_ada_1234abcd", "job_dirty_live", "completed")
    ledger_key = "abcdef0123456789"
    (dirty_live / "state" / "failed_orders.json").write_text(
        json.dumps({ledger_key: {"key": ledger_key, "order": ORDER,
                                  "reason": "timeout", "attempts": 1}}),
        encoding="utf-8")
    archived_dirty = _make_job(tmp_path, "client_ada_1234abcd", "job_dirty_archived", "completed")
    archive_dir = tmp_path / "client_ada_1234abcd" / "jobs" / "archive" / "completed"
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archived_dirty / "state" / "failed_orders.json").write_text(
        json.dumps({ledger_key: {"key": ledger_key, "order": ORDER,
                                  "reason": "timeout", "attempts": 1}}),
        encoding="utf-8")
    shutil.move(str(archived_dirty), str(archive_dir / "job_dirty_archived"))
    pending = manager.discover_pending_jobs()
    found = {w.job_id for w in pending}
    assert "job_dirty_archived" in found, found
    assert "job_dirty_live" in found, found
    assert "job_clean" not in found, found
    # the archived one was restored back into the live jobs directory
    restored = tmp_path / "client_ada_1234abcd" / "jobs" / "job_dirty_archived"
    assert restored.is_dir()
    restored_manifest = json.loads((restored / "job.json").read_text(encoding="utf-8"))
    assert restored_manifest["status"] == "partial"

# --------------------------------------------------- profile self-heal
def _make_profile(manager, tmp_path, client: str, crm_account_id: str) -> Path:
    profile_path = manager.client_paths(client).profile_path
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path.write_text(
        "client_id: " + client + "\n"
        "name_key: ada\n"
        "enabled: true\n"
        "credential_reference: env:ADA_1234ABCD\n"
        f"crm_account_id: {crm_account_id}\n",
        encoding="utf-8",
    )
    return profile_path


def test_missing_crm_account_id_self_heals(manager, tmp_path):
    profile = _make_profile(manager, tmp_path, "client_ada_1234abcd", "null")
    with pytest.raises(RuntimeError) as exc:
        manager.validate_client_ready("client_ada_1234abcd")
    # the account gate must no longer be the blocker -- the remaining
    # error is the (expected, env-less) credential audit
    assert "crm_account_id" not in str(exc.value)
    # ...and the documented default identity was persisted
    assert "crm_account_id: client_ada_1234abcd" in profile.read_text(encoding="utf-8")
    # idempotent: a second validation must not re-block or re-write
    with pytest.raises(RuntimeError) as exc2:
        manager.validate_client_ready("client_ada_1234abcd")
    assert "crm_account_id" not in str(exc2.value)
    assert "crm_account_id: client_ada_1234abcd" in profile.read_text(encoding="utf-8")


def test_explicit_crm_account_id_is_untouched(manager, tmp_path):
    profile = _make_profile(manager, tmp_path, "client_ada_1234abcd", "crm_account_777")
    with pytest.raises(RuntimeError) as exc:
        manager.validate_client_ready("client_ada_1234abcd")
    assert "crm_account_id" not in str(exc.value)
    assert "crm_account_id: crm_account_777" in profile.read_text(encoding="utf-8")


def test_call_arity_matches_signatures():
    """AST guard: every call to the ledger-aware run_upload functions
    passes enough POSITIONAL args to satisfy the def's required
    parameters. This is the exact check that would have caught the
    "missing 1 required positional argument: 'flagged_rows'" TypeError:
    a stale call site that omits failed_ledger silently shifts
    failure_log/failed_rows/counters into the wrong slots and leaves
    flagged_rows unbound -- py_compile cannot see it, but this can."""
    import ast
    import inspect
    import apps.sniper.run_upload as ru

    targets = {
        name: inspect.signature(getattr(ru, name))
        for name in ("_attempt_order", "_drain_retry_queue",
                     "_drain_failed_ledger", "_run_session",
                     "_run_upload", "_run_sharded", "main")
    }
    source = Path(ru.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    checked = 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in targets):
            continue
        sig = targets[node.func.id]
        positional = [
            a for a in node.args
            if not isinstance(a, ast.Starred)
        ]
        keywords = {
            k.arg: None for k in node.keywords if k.arg is not None
        }
        try:
            sig.bind(*[None] * len(positional), **keywords)
        except TypeError as exc:
            raise AssertionError(
                f"{node.func.id}() call at line {node.lineno} passes "
                f"{len(positional)} positional arg(s) but the def requires "
                f"more -- stale call site after a signature change ({exc})"
            ) from exc
        checked += 1
    # every def + its call sites must actually have been walked
    assert checked >= 10, f"only {checked} call(s) matched -- guard is broken"

# ------------------------------------- live-row recheck (stale-skip fix)
# Oct 2026: "failed rows were judged from CACHED data and never re-read the
# real source file, so a row the user FIXED after it failed was still skipped
# as 'same error'." _match_fresh_order is the fix: it re-reads the row from
# the freshly loaded workbook before any skip decision.
def _entry(order, row_number, reason="timeout"):
    from apps.sniper.product_upload.progress_tracker import order_key
    return fl.FailedLedgerEntry(
        key=order_key(order), order=dict(order), reason=reason,
        row_number=row_number, attempts=1,
    )


def test_match_fresh_order_unchanged_row():
    from apps.sniper.run_upload import _match_fresh_order
    entry = _entry(ORDER, 2)
    orders = [dict(ORDER, customer_name="Other"), dict(ORDER),
              dict(ORDER, customer_name="Late")]
    fresh, row, changed, note = _match_fresh_order(entry, orders)
    assert fresh == ORDER
    assert row == 2 and changed is False and note is None


def test_match_fresh_order_edited_row_is_marked_changed():
    from apps.sniper.run_upload import _match_fresh_order
    entry = _entry(ORDER, 1)
    fixed = dict(ORDER, price="23000")          # user corrected the price
    fresh, row, changed, note = _match_fresh_order(entry, [fixed])
    assert fresh == fixed
    assert changed is True and row == 1
    assert note and "EDITED" in note


def test_match_fresh_order_removed_row_is_dropped():
    from apps.sniper.run_upload import _match_fresh_order
    entry = _entry(ORDER, 1)
    fresh, row, changed, note = _match_fresh_order(
        entry, [dict(ORDER, customer_name="Somebody Else")])
    assert fresh is None and changed is False
    assert note and "removed" in note


def test_match_fresh_order_survives_reordering():
    from apps.sniper.run_upload import _match_fresh_order
    entry = _entry(ORDER, 1)
    orders = [dict(ORDER, customer_name="AAA"), dict(ORDER, customer_name="BBB"),
              dict(ORDER)]
    fresh, row, changed, note = _match_fresh_order(entry, orders)
    assert fresh == ORDER and changed is False and row == 3


# ---------------------------------- drain-level regression (stale-cache bug)
class _NotDone:
    def is_done(self, order):
        return False


class _FakeRetryCache:
    def __init__(self):
        self.removed = []

    def remove_key(self, key):
        self.removed.append(key)

    def remove(self, order):
        pass


def test_failed_drain_retries_edited_row_despite_permanent_reason(
        tmp_path, monkeypatch):
    """The exact reported bug: a row the user FIXED after it failed (so its
    dedup key changed) must be attempted even though the last recorded reason
    looked like a permanent data rejection -- otherwise the fix is ignored
    and the row is skipped forever as 'same error'."""
    import apps.sniper.run_upload as ru
    from apps.sniper.product_upload.progress_tracker import order_key

    stale = dict(ORDER, price="28500")
    led = fl.FailedLedger(path=str(tmp_path / "failed_orders.json"))
    led.record_failure(stale, "no sellable product found", row_number=1)
    rc = _FakeRetryCache()
    attempts = []

    def fake_attempt(page, cfg, order, row_number, *a, **k):
        attempts.append((order, row_number))
        return True

    monkeypatch.setattr(ru, "_attempt_order", fake_attempt)

    fixed = dict(ORDER, price="23000")
    ru._drain_failed_ledger(
        None, None, [fixed], _NotDone(), _NotDone(), rc, led,
        None, [], {}, [], "test", lock_mode=False)

    assert attempts, "the amended row was never retried"
    assert attempts[0][0] == fixed          # the FRESH row, not the cached one
    assert attempts[0][1] == 1
    assert order_key(stale) in rc.removed   # stale entry cleaned out
    assert led.count == 0                   # stale ledger entry removed


def test_failed_drain_skips_unchanged_permanent_row(tmp_path, monkeypatch):
    """Backward compatibility: an UNCHANGED row whose last failure was a
    permanent data rejection is still skipped with no browser attempt."""
    import apps.sniper.run_upload as ru

    led = fl.FailedLedger(path=str(tmp_path / "failed_orders.json"))
    led.record_failure(ORDER, "no sellable product found", row_number=1)
    attempts = []
    monkeypatch.setattr(ru, "_attempt_order",
                        lambda *a, **k: attempts.append(a) or True)

    ru._drain_failed_ledger(
        None, None, [dict(ORDER)], _NotDone(), _NotDone(),
        _FakeRetryCache(), led, None, [], {}, [], "test", lock_mode=False)

    assert not attempts, "an unchanged permanent rejection must not be retried"


def test_failed_drain_drops_row_deleted_from_file(tmp_path, monkeypatch):
    """A row removed from the upload file is cleared from both stores with no
    browser interaction."""
    import apps.sniper.run_upload as ru

    led = fl.FailedLedger(path=str(tmp_path / "failed_orders.json"))
    led.record_failure(ORDER, "timeout", row_number=1)
    rc = _FakeRetryCache()
    attempts = []
    monkeypatch.setattr(ru, "_attempt_order",
                        lambda *a, **k: attempts.append(a) or True)

    ru._drain_failed_ledger(
        None, None, [dict(ORDER, customer_name="Someone Else")],
        _NotDone(), _NotDone(), rc, led, None, [], {}, [], "test",
        lock_mode=False)

    assert not attempts
    assert led.count == 0


# __TEST_PART_4__

# ---------------- Oct 2026: matcher aliases + PRICE_TO_PRODUCT -----------
def test_price_alias_families_resolve_to_confirmed_bundles():
    """The two recurring Excel families must resolve to the confirmed live
    bundles through the REAL pipeline (parse_order_products: whole-string
    match, then promo filtering + split). No fuzzy-only rescue, no price
    inference -- alias/direct hits at high confidence."""
    from apps.sniper.product_upload.product_matcher import parse_order_products
    catalog = ["Sadoer Collagen Combo + Body Lotion", "Sadoer Collagen Combo Set"]
    rows = {
        "1 Advanced Collagen Body Lotion + Free Scar Repair Cream "
        "+ Free Collagen Hand Cream": "Sadoer Collagen Combo + Body Lotion",
        "1 Combo set -(1 serum & 1 Cream) + 2 FREE Spa Facial Masks "
        "+ Free Doorstep Delivery": "Sadoer Collagen Combo Set",
        "Combo-set face cream and serum with 2free face mask":
            "Sadoer Collagen Combo Set",
    }
    for text, expected in rows.items():
        matches = parse_order_products(text, catalog)
        assert matches and all(m.matched_product is not None for m in matches), \
            (text, matches)
        assert matches[0].matched_product == expected, (text, matches)
        assert matches[0].confidence_band == "high", (text, matches)


def test_product_for_price_maps_confirmed_pairs_and_guards_catalog():
    """28500 <-> Body Lotion, 29500 <-> Combo Set; a stale target that left
    the live catalog resolves to None instead of a wrong label."""
    from apps.sniper.product_upload.product_matcher import product_for_price
    live = ["Sadoer Collagen Combo + Body Lotion", "Sadoer Collagen Combo Set"]
    assert product_for_price(28500, live).matched_product == \
        "Sadoer Collagen Combo + Body Lotion"
    assert product_for_price("29,500", live).matched_product == \
        "Sadoer Collagen Combo Set"
    assert product_for_price(28500, ["Sadoer Collagen Combo Set"]) is None
    assert product_for_price(12345, live) is None
    assert product_for_price("n/a", live) is None


def test_planning_price_fallback_fills_hole_but_never_overrides_hit():
    """planning.product_for_price is consulted only when normal matching
    yields nothing sellable; a real hit is never replaced."""
    from apps.sniper.product_upload import planning
    catalog = ["Sadoer Collagen Combo + Body Lotion", "Sadoer Collagen Combo Set"]
    gibberish = {"product": "zz qq xx", "price": "28500",
                 "address": "Ikeja Lagos"}
    plan = planning.build_execution_plan(gibberish, catalog)
    assert plan.matches[0].matched_product == "Sadoer Collagen Combo + Body Lotion"
    assert plan.matches[0].needs_manual_review is True

    real = {"product": "Sadoer Collagen Combo Set", "price": "28500",
            "address": "Ikeja Lagos"}
    plan2 = planning.build_execution_plan(real, catalog)
    assert plan2.matches[0].matched_product == "Sadoer Collagen Combo Set"
    assert "(inferred from price" not in plan2.matches[0].excel_text

# __TEST_PART_5__

# ------------- Oct 2026: local-only state inference (no web, no fallback) --
def test_planning_blank_address_raises_missing_address_error():
    """Empty address -> MissingAddressError (a PlanningError subclass): the
    caller flags the row for a human on the first sighting. No state and no
    fallback are guessed here."""
    from apps.sniper.product_upload import planning
    with pytest.raises(planning.MissingAddressError):
        planning.build_execution_plan(
            {"product": "Sadoer Collagen Combo Set", "price": "29500",
             "address": "   "}, ["Sadoer Collagen Combo Set"])


def test_planning_unresolvable_address_skips_state():
    """An address the local tables cannot resolve -> plan.state is None:
    the caller skips the state field entirely and saves. No network, no
    Lagos fallback."""
    from apps.sniper.product_upload import planning
    plan = planning.build_execution_plan(
        {"product": "Sadoer Collagen Combo Set", "price": "29500",
         "address": "Okapanam unmappable zone 99xyz"},
        ["Sadoer Collagen Combo Set"])
    assert plan.state is None
    assert plan.used_state_fallback is False


def test_planning_local_hit_selects_state(monkeypatch):
    """A locally resolvable address still resolves to its state, and the
    memo never touches the network (no such call site exists any more)."""
    from apps.sniper.product_upload import planning
    import apps.sniper.product_upload.state_engine as se
    se.clear_memo()
    plan = planning.build_execution_plan(
        {"product": "Sadoer Collagen Combo Set", "price": "29500",
         "address": "14 Allen Avenue, Ikeja, Lagos"},
        ["Sadoer Collagen Combo Set"])
    assert plan.state == "Lagos"


def test_memo_repeats_cost_one_inference(monkeypatch):
    """The in-process memo: the same address (different spacing) resolves
    once; a counting infer_state proves the second call is served from
    the memo."""
    import apps.sniper.product_upload.state_engine as se
    se.clear_memo()
    calls = []
    real = se.infer_state

    def _counting(addr):
        calls.append(addr)
        return real(addr)
    monkeypatch.setattr(se, "infer_state", _counting)
    assert se.infer_state_memoized("  14 Allen   Ikeja  ") == "Lagos"
    assert se.infer_state_memoized("14 Allen Ikeja") == "Lagos"
    assert len(calls) == 1
    se.clear_memo()

# __TEST_PART_6__

# ------------- Oct 2026: relaxed Excel load gate -------------------------
def test_load_gate_only_skips_genuinely_unactionable_rows():
    """Address-less rows now load (Lagos default downstream); rows with no
    product+price, no name, or no phone/whatsapp are still skipped."""
    from apps.sniper.run_upload import _missing_required_fields
    assert _missing_required_fields(
        {"product": "", "price": "", "customer_name": "A",
         "phone_number": "0801", "whatsapp_number": "",
         "address": "Lagos"}) == ["product and price"]
    assert _missing_required_fields(
        {"product": "x", "price": "100", "customer_name": " ",
         "phone_number": "0801", "whatsapp_number": "",
         "address": "Lagos"}) == ["customer_name"]
    assert _missing_required_fields(
        {"product": "x", "price": "100", "customer_name": "A",
         "phone_number": "", "whatsapp_number": " ",
         "address": "Lagos"}) == ["phone_number/whatsapp_number"]
    # the relaxed rule: blank address alone is NOT a skip reason
    assert _missing_required_fields(
        {"product": "Sadoer Collagen Combo Set", "price": "29500",
         "customer_name": "A", "phone_number": "0801",
         "whatsapp_number": "", "address": "   "}) == []
    # whatsapp alone satisfies the contact requirement
    assert _missing_required_fields(
        {"product": "x", "price": "100", "customer_name": "A",
         "phone_number": "", "whatsapp_number": "0802",
         "address": ""}) == []


# ---- Oct 2026: done-rows are filtered BEFORE planning (long-wait fix) ----
def test_todo_filter_skips_done_rows_before_planning(tmp_path, monkeypatch):
    """The exact reported stall: planning (product match + state web lookup)
    must run ONLY for rows that can actually be attempted. Pre-mark half the
    rows done in a temp progress DB and assert build_execution_plan is never
    called for them -- and that original 1-based row numbers survive."""
    import apps.sniper.run_upload as ru
    from apps.sniper.product_upload.progress_tracker import ProgressTracker

    orders = [
        {"customer_name": f"C{i}", "phone_number": f"080{i}",
         "product": "Sadoer Collagen Combo Set", "price": "29500",
         "address": "Ikeja Lagos"}
        for i in range(4)
    ]
    done_db = ProgressTracker(path=str(tmp_path / "done.db"))
    empty_db = ProgressTracker(path=str(tmp_path / "empty.db"))
    done_db.mark_done(orders[0], row_number=1)
    done_db.mark_done(orders[2], row_number=3)

    todo = [
        (n, o) for n, o in enumerate(orders, start=1)
        if not (done_db.is_done(o) or empty_db.is_done(o))
    ]
    assert [n for n, _ in todo] == [2, 4]

    calls = []
    monkeypatch.setattr(
        ru, "build_execution_plan",
        lambda order, catalog: calls.append(order) or "PLAN")
    plans = {}
    for row_number, order in todo:
        plans[row_number] = ru.build_execution_plan(order, ["catalog"])
    assert sorted(plans) == [2, 4]
    assert calls == [orders[1], orders[3]]
    done_db.close()
    empty_db.close()


# - Oct 2026: modal-closing bug (Escape fallback killed the whole dialog) --
class _FakeLocator:
    """Minimal Playwright-locator stand-in: records clicks, answers
    count()/is_visible()/wait_for() from scriptable flags."""

    def __init__(self, owner, visible=True, count=1):
        self._owner = owner
        self._visible = visible
        self._count = count

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        raise AssertionError(f"unexpected locator attr {name!r} in test fake")

    @property
    def first(self):
        return self

    def count(self):
        return self._count

    def is_visible(self):
        return self._visible() if callable(self._visible) else self._visible

    def click(self, timeout=None, no_wait_after=None):
        self._owner.clicked.append(self._owner.name)
        self._owner.on_click()

    def wait_for(self, state=None, timeout=None):
        return None

    def focus(self):
        return None

    def evaluate(self, *a, **k):
        return None


class _FakeScope:
    """Stand-in for page + dialog. `overlay_open` drives LISTBOX_SELECTOR
    visibility; every other selector is visible-but-inert."""

    LISTBOX = "listbox"

    def __init__(self, overlay_open=True):
        self.name = "scope"
        self.clicked: list[str] = []
        self.overlay_open = overlay_open
        self.keyboard_calls: list[str] = []

    def on_click(self):
        pass

    def locator(self, selector):
        if selector == self.LISTBOX:
            return _FakeLocator(self, visible=lambda: self.overlay_open)
        return _FakeLocator(self, visible=True)

    @property
    def keyboard(self):
        scope = self

        class _Kb:
            def press(self, key):
                scope.keyboard_calls.append(key)
                raise AssertionError(
                    "keyboard.press must never fire during combo cleanup")

        return _Kb()

    def evaluate(self, *a, **k):
        return "BUTTON"


def _combo_cfg(**over):
    import apps.sniper.product_upload.add_products as ap
    kw = dict(crm_catalog=["x"], diagnostics_dir=".",
              combo_click_timeout_ms=50)
    kw.update(over)
    return ap.AutomationConfig(**kw)


def _combo_state(dc, trigger=None, last_clicked=None):
    st = dc._ComboState()
    st.current_trigger = trigger
    st.last_clicked = last_clicked
    return st


def test_close_open_combo_noop_when_nothing_open():
    import apps.sniper.product_upload.dialog_controls as dc
    import apps.sniper.utils.field_selectors as sel
    page = _FakeScope(overlay_open=False)
    real_listbox = sel.LISTBOX_SELECTOR
    sel.LISTBOX_SELECTOR = _FakeScope.LISTBOX
    try:
        st = _combo_state(dc)
        assert dc._close_open_combo(page, object(), st, _combo_cfg()) is True
        assert page.clicked == [] and page.keyboard_calls == []
        assert st.current_trigger is None
    finally:
        sel.LISTBOX_SELECTOR = real_listbox


def test_close_open_combo_trigger_reclick_clears_overlay():
    """Primary path: re-clicking the tracked trigger clears the overlay and
    returns True -- with zero keypresses."""
    import apps.sniper.product_upload.dialog_controls as dc
    import apps.sniper.utils.field_selectors as sel
    real_listbox = sel.LISTBOX_SELECTOR
    sel.LISTBOX_SELECTOR = _FakeScope.LISTBOX
    try:
        page = _FakeScope(overlay_open=True)
        trigger = _FakeLocator(page, visible=True)
        page.on_click = lambda: setattr(page, "overlay_open", False)
        st = _combo_state(dc, trigger=trigger)
        assert dc._close_open_combo(page, object(), st, _combo_cfg()) is True
        assert page.keyboard_calls == []
        assert st.current_trigger is None
    finally:
        sel.LISTBOX_SELECTOR = real_listbox


def test_close_open_combo_last_selection_fallback_before_dead_space():
    """When the trigger re-click fails to clear, the last real selection
    (click_resilient's pattern) is tried BEFORE any in-dialog dead-space
    click -- and a stubborn overlay returns False instead of Escaping."""
    import apps.sniper.product_upload.dialog_controls as dc
    import apps.sniper.utils.field_selectors as sel
    real_listbox = sel.LISTBOX_SELECTOR
    sel.LISTBOX_SELECTOR = _FakeScope.LISTBOX
    try:
        order = []

        class _OrderScope(_FakeScope):
            def locator(self, selector):
                loc = super().locator(selector)
                if selector != self.LISTBOX:
                    loc._owner.name = f"dead:{selector}"
                return loc

        page = _OrderScope(overlay_open=True)
        trigger = _FakeLocator(page, visible=True)
        last = _FakeLocator(page, visible=True)

        orig_click = _FakeLocator.click

        def _tracking_click(self, timeout=None, no_wait_after=None):
            order.append("trigger" if self is trigger
                         else "last" if self is last else "dead")
            return orig_click(self, timeout=timeout,
                              no_wait_after=no_wait_after)

        _FakeLocator.click = _tracking_click
        try:
            st = _combo_state(dc, trigger=trigger, last_clicked=last)
            assert dc._close_open_combo(page, object(), st,
                                        _combo_cfg()) is False
        finally:
            _FakeLocator.click = orig_click
        assert order[0] == "trigger" and order[1] == "last", order
        assert page.keyboard_calls == [], page.keyboard_calls
    finally:
        sel.LISTBOX_SELECTOR = real_listbox


# __TEST_PART_7__

# - Oct 2026: NaN-address modal-close fix + temporal state cache + fast search
def _clear_state_caches():
    import apps.sniper.product_upload.state_engine as se
    se.clear_memo()
    return se


def test_nan_address_plan_raises_missing_address_error():
    """pandas NaN address -> MissingAddressError (the caller flags it for a
    human on the first sighting and injects its own 'Lagos' default on the
    second); planning never guesses a state from nothing."""
    from apps.sniper.product_upload import planning
    _clear_state_caches()
    with pytest.raises(planning.MissingAddressError):
        planning.build_execution_plan(
            {"product": "Sadoer Collagen Combo Set", "price": "29500",
             "address": float("nan")},
            ["Sadoer Collagen Combo Set"])


def test_fill_order_for_plan_merges_planned_address():
    """The fill dict takes the plan's effective address; every other field
    and the raw order stay untouched; no plan -> same object back."""
    import apps.sniper.product_upload.add_products as ap
    from apps.sniper.product_upload import planning
    _clear_state_caches()
    raw = {"customer_name": "N", "phone_number": "0801",
           "address": float("nan"), "product": "x"}
    # Second-sighting behaviour (run_upload.py injects 'Lagos' before
    # planning), so the plan holds a real, verifiable fill address.
    plan = planning.build_execution_plan(
        {"product": "Sadoer Collagen Combo Set", "price": "29500",
         "address": "Lagos"},
        ["Sadoer Collagen Combo Set"])
    filled = ap._fill_order_for_plan(raw, plan)
    assert filled["address"] == "Lagos"
    assert filled["phone_number"] == "0801"
    assert raw["address"] != raw["address"]  # still NaN: raw never mutated
    assert ap._fill_order_for_plan(raw, None) is raw


class _FillLoc:
    """Minimal stand-in for a text input: fill() stores, input_value()
    reads back -- everything _type_and_verify needs via the fast_waits
    Python fallback."""

    def __init__(self, store, key):
        self._store = store
        self._key = key

    def wait_for(self, state=None, timeout=None):
        return None

    def fill(self, text):
        self._store[self._key] = text

    def input_value(self):
        return self._store.get(self._key, "")


class _FillDialog:
    def __init__(self, store):
        self._store = store

    def locator(self, selector):
        return _FillLoc(self._store, selector)


class _FillCfg:
    element_visible_timeout_ms = 100
    value_poll_timeout_ms = 100
    value_poll_interval_s = 0.01


def test_fill_customer_info_nan_address_fills_lagos():
    """The address box receives 'Lagos' (verifiable, succeeds first try)
    instead of an empty fill that always failed transient."""
    import apps.sniper.product_upload.field_values as fv
    import apps.sniper.utils.field_selectors as sel
    store: dict = {}
    err = fv._fill_customer_info(
        _FillDialog(store),
        {"customer_name": "N", "phone_number": "0801",
         "address": float("nan")},
        _FillCfg())
    assert err is None, err
    assert store[sel.ADDRESS_INPUT_SELECTOR] == "Lagos"


# ---------------- Oct 2026: missing-address counter (first flag, 2nd Lagos) --
def _ma_order(**over):
    order = {"customer_name": "Ada Obi", "phone_number": "08011112222",
             "product": "Sadoer Collagen Combo Set", "price": "29500",
             "address": "", "filtered_sn": "7", "excel_row": 13}
    order.update(over)
    return order


def test_missing_address_counter_flags_then_falls_back_and_resets(tmp_path,
                                                                  monkeypatch,
                                                                  capsys):
    """Sighting 1 -> True (flagged, NOT uploaded) + HUMAN ACTION block naming
    the Filtered # and customer name; sighting 2 -> False (proceed) and the
    stored count RESETS, so the cycle can repeat."""
    import apps.sniper.run_upload as ru
    monkeypatch.setattr(ru, "PROGRESS_DIR", tmp_path)
    counts: dict[str, int] = {}

    first = ru._handle_missing_address(_ma_order(), 3, counts)
    assert first is True
    out = capsys.readouterr().out
    assert "HUMAN ACTION NEEDED" in out
    assert "Filtered # = 7" in out and "Ada Obi" in out
    assert "NOT uploaded" in out
    assert set(counts.values()) == {1}
    # persisted immediately
    import json
    on_disk = json.loads((tmp_path / "missing_address_counts.json")
                         .read_text(encoding="utf-8"))
    assert set(on_disk.values()) == {1}

    second = ru._handle_missing_address(_ma_order(), 3, counts)
    assert second is False
    out = capsys.readouterr().out
    assert "proceeding automatically with the 'Lagos' address default" in out
    assert counts == {}  # reset
    on_disk = json.loads((tmp_path / "missing_address_counts.json")
                         .read_text(encoding="utf-8"))
    assert on_disk == {}


def test_missing_address_save_merges_concurrent_writers(tmp_path, monkeypatch):
    """A shard sibling's keys on disk are never wiped: save merges on
    write (last-write-wins per key only)."""
    import json
    import apps.sniper.run_upload as ru
    monkeypatch.setattr(ru, "PROGRESS_DIR", tmp_path)
    (tmp_path / "missing_address_counts.json").write_text(
        json.dumps({"sibling-key": 1}), encoding="utf-8")
    ru._save_missing_address_counts({"mine": 1})
    data = json.loads((tmp_path / "missing_address_counts.json")
                      .read_text(encoding="utf-8"))
    assert data == {"sibling-key": 1, "mine": 1}


def test_load_missing_address_counts_tolerates_corrupt(tmp_path, monkeypatch):
    """A corrupt/missing counts file loads as {} -- counting restarts
    instead of failing the run."""
    import apps.sniper.run_upload as ru
    monkeypatch.setattr(ru, "PROGRESS_DIR", tmp_path)
    assert ru._load_missing_address_counts() == {}
    (tmp_path / "missing_address_counts.json").write_text(
        "{not json", encoding="utf-8")
    assert ru._load_missing_address_counts() == {}


def test_load_orders_carries_filtered_sn_and_excel_row(tmp_path):
    """load_orders stamps every order with the human-facing cell identifiers
    (Filtered # value + real Excel row) used by the missing-address flag."""
    import pandas as pd
    import apps.sniper.run_upload as ru
    src = tmp_path / "orders.xlsx"
    naira = chr(0x20A6)  # the real 'Price (₦)' header, encoding-proof
    pd.DataFrame({
        "Filtered #": [1, 2],
        "Customer Name": ["Ada", "Bola"],
        "Phone Number": ["0801", "0802"],
        "WhatsApp Number": ["", ""],
        "Address": ["Ikeja Lagos", ""],
        "Product": ["Sadoer Collagen Combo Set", "Sadoer Collagen Combo Set"],
        f"Price ({naira})": ["29500", "29500"],
    }).to_excel(src, index=False)
    orders = ru.load_orders(str(src))
    assert orders[0]["filtered_sn"] == "1" and orders[0]["excel_row"] == 2
    assert orders[1]["excel_row"] == 3


def test_load_orders_still_skips_fully_blank_rows(tmp_path):
    """The Filtered #/excel-row stamping must never make an all-blank row
    look non-blank (it is stamped only after the blank/required checks)."""
    import pandas as pd
    import apps.sniper.run_upload as ru
    naira = chr(0x20A6)
    src = tmp_path / "orders.xlsx"
    pd.DataFrame({
        "Filtered #": [1, 2, 3],
        "Customer Name": ["Ada", "", ""],
        "Phone Number": ["0801", "", ""],
        "WhatsApp Number": ["", "", ""],
        "Address": ["Ikeja Lagos", "", ""],
        "Product": ["Sadoer Collagen Combo Set", "", ""],
        f"Price ({naira})": ["29500", "", ""],
    }).to_excel(src, index=False)
    orders = ru.load_orders(str(src))
    assert [o["customer_name"] for o in orders] == ["Ada"]





