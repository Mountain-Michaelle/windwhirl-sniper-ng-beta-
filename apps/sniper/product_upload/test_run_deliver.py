"""Unit tests for run_deliver.py's durable state machine (no browser)."""
import io
import json
import re
import sys
from pathlib import Path
import pytest


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from apps.sniper import run_deliver as rd  # noqa: E402


@pytest.fixture()
def store_root(tmp_path):
    return tmp_path / "deliver_root"


def test_fingerprint_identity_is_order_id_not_name():
    # doc §2/§17: two orders with same customer+amount must not collide
    k1 = rd.fingerprint("Michael", "This Month", "ORD1")
    k2 = rd.fingerprint("Michael", "This Month", "ORD2")
    assert k1 != k2
    assert k1 == "Michael|This Month|ORD1"


def test_store_persists_success_across_restart(store_root):
    store = rd.DeliverStore("client_x", "This Month", store_root)
    rec = store.record("John Doe", "ORD9")
    assert rec.state == rd.DISCOVERED
    rec.transition(rd.SAVE_SUBMITTED)
    store.checkpoint()
    store.mark_success(rec, "ORD9")
    assert store.is_done("John Doe")

    # fresh instance == restart after crash: idempotent skip
    store2 = rd.DeliverStore("client_x", "This Month", store_root)
    assert store2.is_done("John Doe")
    rec2 = store2.record("John Doe", "ORD9")
    assert rec2.state == rd.SUCCESS


def test_failed_retry_then_final_keeps_attempt_counter(store_root):
    store = rd.DeliverStore("client_x", "This Month", store_root)
    rec = store.record("Jane", "ORD10")
    store.mark_failed(rec, "SaveModal", "save_button_disabled", final=False)
    assert rec.data["attempt"] == 1
    store.mark_failed(rec, "SaveModal", "still disabled", final=False)
    assert rec.data["attempt"] == 2
    # reload from disk: attempt counter survives
    store2 = rd.DeliverStore("client_x", "This Month", store_root)
    key = rd.fingerprint("client_x", "This Month", "ORD10")
    assert store2.state["orders"][key]["attempt"] == 2


def test_is_done_requires_delivered_status(store_root):
    store = rd.DeliverStore("client_x", "This Month", store_root)
    store.processed["k"] = {"customer": "Ann", "status": "Pending"}
    assert not store.is_done("Ann")
    store.processed["k"]["status"] = "Delivered"
    assert store.is_done("Ann")


def test_atomic_write_roundtrip_and_defaults(store_root):
    target = store_root / "state.json"
    rd.atomic_write_json(target, {"orders": {}})
    assert rd.load_json(target, None) == {"orders": {}}
    assert rd.load_json(store_root / "missing.json", {"d": 1}) == {"d": 1}


def test_load_customers_txt_csv_json(tmp_path):
    txt = tmp_path / "c.txt"
    txt.write_text("John Doe\n  Mary Jane ,x\n\n", encoding="utf-8")
    assert rd.load_customers(txt) == ["John Doe", "Mary Jane"]
    js = tmp_path / "c.json"
    js.write_text(json.dumps({"customers": ["A B", "C D"]}), encoding="utf-8")
    assert rd.load_customers(js) == ["A B", "C D"]


def test_backoff_bounds():
    for attempt in range(1, 8):
        d = rd.backoff_delay(attempt)
        assert 0.5 <= d <= 8.2


def test_sqlite_store_persists_and_blocks_overwrite(store_root):
    from apps.sniper.product_deliver import store as st
    db1 = st.SqliteDeliverStore("client_x", "This Month", store_root / "db")
    rec = db1.record("John Doe", "ORD9")
    db1.mark_success(rec, "ORD9")
    assert db1.is_done("John Doe")
    assert db1.row("John Doe")["orderId"] == "ORD9"
    db1.close()
    # restart: same file-backed truth, no JSON overwrite races
    db2 = st.SqliteDeliverStore("client_x", "This Month", store_root / "db")
    assert db2.is_done("John Doe")
    rec2 = db2.record("John Doe")
    assert rec2.state == "SUCCESS"
    db2.mark_failed(rec2, "x", "y", final=True)
    rec3 = db2.record("John Doe")
    assert rec3.data["attempt"] == 1
    db2.close()


def test_inbox_matching_prefers_longest_key():
    from apps.sniper.product_deliver import inbox as ib
    registry = {"name_keys": {"michael": ["client_michael_0c05e53b"],
                              "michaela": ["client_michaela_9"],
                              "valentine": ["client_valentine_8232c8bd"]}}
    assert ib.match_name_keys("michael_orders.xlsx", registry) == ["michael"]
    assert ib.match_name_keys("unrelated.xlsx", registry) == []


def test_load_customers_returns_the_delivered_marked_rows(tmp_path):
    import openpyxl
    wb_path = tmp_path / "sheet.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Raw Message", "Filtered #", "Customer Name", "Phone",
               "Action"])
    ws.append(["m1", 1, "Aighewi", "1", "Scheduled"])
    ws.append(["m2", 2, "Ada", "2", "Pending"])
    ws.append(["m3", 3, "Kris Nwachukwu", "3", "Delivered"])
    ws.append(["m4", 4, "Gladys Andrew", "4", "Delivered"])
    wb.save(str(wb_path))
    # the sheet's marking is the INSTRUCTION: Delivered rows are the work
    assert rd.load_customers(wb_path) == ["Kris Nwachukwu", "Gladys Andrew"]
    assert rd.load_customers(wb_path, only_pending=False) == [
        "Aighewi", "Ada", "Kris Nwachukwu", "Gladys Andrew"]


def test_state_machine_states_exist():
    for st in (rd.DISCOVERED, rd.ROW_FOUND, rd.MODAL_OPEN, rd.READY_TO_SAVE,
               rd.SAVE_SUBMITTED, rd.VERIFYING, rd.SUCCESS,
               rd.FAILED_RETRYABLE, rd.FAILED_FINAL):
        assert isinstance(st, str)

# Guards: a missing selector constant (e.g. TABLE_ROW) once crashed
# a live run with NameError. These fail the suite instead.


def test_module_has_no_undefined_names():
    import ast
    import builtins

    tree = ast.parse(
        __import__("pathlib").Path(rd.__file__).read_text(encoding="utf-8"))
    known = set(dir(builtins)) | {
        "__file__", "__name__", "__doc__", "__package__", "__spec__",
        "__loader__", "__builtins__",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            known.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
            known.add(node.name)
        elif isinstance(node, ast.arg):
            known.add(node.arg)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                known.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            known.add(node.name)
        elif isinstance(node, ast.Global):
            known.update(node.names)
    used = {n.id for n in ast.walk(tree)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    assert sorted(used - known) == []


def test_user_option_label_strips_group_prefix():
    strip = rd.DeliverDriver._strip_group
    assert strip("Sales Rep michael") == "michael"
    assert strip("Other All") == "All"
    assert strip("michael") == "michael"


def test_preferred_user_name_from_canonical_client_id():
    pref = rd.DeliverDriver._preferred_users
    assert pref("client_michael_0c05e53b") == ["michael"]
    assert pref("Michael") == ["michael"]


def test_cmdk_user_rule_skips_other_group():
    pick = rd.DeliverDriver._user_option_index
    opts = [("Other All", "All"),
            ("Sales Rep ada", "ada"),
            ("Sales Rep michael", "michael")]
    assert pick(opts, []) == 1
    assert pick(opts, ["michael"]) == 2
    assert pick([("Other All", "All")], []) is None


def test_latest_status_file_picks_newest(tmp_path, monkeypatch):
    import os as _os
    import time as _time
    from types import SimpleNamespace
    status = tmp_path / 'jobs' / 'status'
    status.mkdir(parents=True)
    old = status / 'a_michael.xlsx'
    new = status / 'b_michael.xlsx'
    old.write_bytes(b'a')
    new.write_bytes(b'b')
    now = _time.time()
    _os.utime(old, (now - 100, now - 100))
    _os.utime(new, (now, now))
    mgr = SimpleNamespace(
        client_paths=lambda cid: SimpleNamespace(jobs_dir=tmp_path / 'jobs'))
    # _latest_status_file() builds the manager itself (single argument)
    # -- patched where it now lives (product_deliver.runner)
    from apps.sniper.product_deliver import runner as rd_runner
    monkeypatch.setattr(rd_runner, 'WorkspaceManager',
                        lambda *a, **k: mgr)
    assert rd._latest_status_file('client_michael_0c05e53b').name == \
        'b_michael.xlsx'


def test_name_key_for_is_asked_from_the_manager():
    """A bare `name_key_for(...)` call once crashed a live run with
    NameError; the current revision asks the manager for it instead."""
    pkg = Path(rd.__file__).parent / "product_deliver"
    sources = [Path(rd.__file__).read_text(encoding="utf-8")]
    sources += [p.read_text(encoding="utf-8") for p in sorted(pkg.glob("*.py"))]
    assert any("manager.name_key_for(" in src for src in sources)
    # ...and the bare helper is never called without the manager anywhere
    assert all(re.search(r"(?<!\.)\bname_key_for\s*\(", src) is None
               or "manager.name_key_for(" in src for src in sources)


# ── name matching: the "NOT_FOUND for a row the table DOES show" bug ────────


def test_name_blocks_are_joined_in_order():
    # the CRM renders 'Oladunni folasade Ramat' as TWO span.block lines
    assert rd.join_name_parts(["Oladunni folasade", "Ramat"]) == \
        "Oladunni folasade Ramat"
    assert rd.join_name_parts(["  Ewumi Elizabeth "]) == "Ewumi Elizabeth"
    assert rd.join_name_parts(["", "   ", "Ann"]) == "Ann"
    assert rd.join_name_parts([]) == ""


def test_wrapped_crm_name_matches_the_excel_name():
    # real case: sheet row 3 'Oladunni folasade Ramat' (+2348082508065)
    shown = rd.join_name_parts(["Oladunni folasade", "Ramat"])
    assert rd.names_match("Oladunni folasade Ramat", shown)
    assert rd.names_match("  oladunni   FOLASADE ramat ", shown)
    assert not rd.names_match("Oladunni folasade", shown)


def test_partial_name_candidate_is_guarded():
    # the CRM holding only PART of the name is a candidate ...
    assert rd.is_name_candidate("Oladunni folasade Ramat",
                                "Oladunni folasade")
    # ... and holding MORE of it is one too (the old fallback let that pass)
    assert rd.is_name_candidate("Blessing", "Blessing Okon")
    assert rd.is_name_candidate("Ken", "Ken Okonkwo")
    # ... but ONE word of a longer name is too weak ...
    assert not rd.is_name_candidate("Mary Jane Okon", "Okon")
    # ... only a whole-word prefix counts: no substring, no re-ordering ...
    assert not rd.is_name_candidate("Oladunni ramat", "Oladunni folasade")
    assert not rd.is_name_candidate("Ann", "Mary Jane")
    # ... and an equal name is a match, not a 'candidate'
    assert not rd.is_name_candidate("Ann Marie", "ann marie")


class _FakeText:
    def __init__(self, text):
        self._text = text

    def inner_text(self, timeout=None):
        return self._text


class _FakeList:
    """Just enough of a Playwright locator for _name_in_cell()."""

    def __init__(self, items=()):
        self._items = list(items)

    @property
    def first(self):
        return self

    def count(self):
        return len(self._items)

    def nth(self, i):
        return self._items[i]


class _FakeCell:
    """The Customer Name <td>: one span.block per rendered name line."""

    def __init__(self, blocks):
        self._blocks = [_FakeText(b) for b in blocks]

    def locator(self, selector):
        if selector == "span.block":
            return _FakeList(self._blocks)
        return _FakeList()            # no wrapper fallback needed here

    def inner_text(self, timeout=None):
        return "\n".join(t.inner_text() for t in self._blocks)


class _FakeHrefs:
    """The Contact cell's <a> elements, as href attributes."""

    def __init__(self, hrefs):
        self._hrefs = list(hrefs)

    def evaluate_all(self, expression):
        return list(self._hrefs)


class _FakeContact:
    """The Contact <td> (a `tel:` link and/or a `wa.me` link)."""

    def __init__(self, hrefs=()):
        self._hrefs = list(hrefs)

    def locator(self, selector):
        return _FakeHrefs(self._hrefs) if selector == "a" else _FakeList()

    def inner_text(self, timeout=None):
        return " ".join(self._hrefs)


class _FakeRow:
    def __init__(self, name_blocks, hrefs=(), cells=14):
        self._cells = _FakeList([_FakeText("") for _ in range(cells)])
        self._cells._items[rd.CUSTOMER_CELL_INDEX] = _FakeCell(name_blocks)
        self._cells._items[rd.CONTACT_CELL_INDEX] = _FakeContact(hrefs)

    def locator(self, selector):
        return self._cells if selector == "td" else _FakeList()


class _FakeRows:
    def __init__(self, rows):
        self._rows = list(rows)

    @property
    def first(self):
        return self

    def count(self):
        return len(self._rows)

    def nth(self, i):
        return self._rows[i]


class _FakePage:
    def __init__(self, blocks_per_row, hrefs_per_row=None):
        hrefs = list(hrefs_per_row or [])
        hrefs += [()] * (len(blocks_per_row) - len(hrefs))
        self._rows = _FakeRows([_FakeRow(blocks, phones) for blocks, phones
                                in zip(blocks_per_row, hrefs)])

    def locator(self, selector):
        return self._rows


def _driver_for_rows(blocks_per_row, hrefs_per_row=None):
    """A DeliverDriver with a fake page: find_row()/_name_in_cell() only."""
    drv = rd.DeliverDriver.__new__(rd.DeliverDriver)
    drv.page = _FakePage(blocks_per_row, hrefs_per_row)
    drv.verbose = False
    drv.phones = {}
    return drv


def test_find_row_matches_a_two_line_crm_name():
    drv = _driver_for_rows([["Oladunni folasade", "Ramat"]])
    assert drv.find_row("Oladunni folasade Ramat") is not None


def test_find_row_uses_a_partial_match_only_when_it_is_unique():
    drv = _driver_for_rows([["Oladunni folasade"]])
    assert drv.find_row("Oladunni folasade Ramat") is not None
    # two rows could both be meant ('Ken' -> 'Ken Okonkwo' / 'Ken Eze')
    drv2 = _driver_for_rows([["Ken Okonkwo"], ["Ken Eze"]])
    assert drv2.find_row("Ken") is None


def test_find_row_never_matches_a_single_word_by_prefix():
    drv = _driver_for_rows([["John"]])
    assert drv.find_row("John Okon") is None


def test_find_row_prefers_the_exact_row():
    drv = _driver_for_rows([["Oladunni folasade"],
                            ["Oladunni folasade Ramat"]])
    rows = drv.page.locator(rd.TABLE_ROW_DATA)
    assert drv.find_row("Oladunni folasade Ramat") is rows.nth(1)


def test_module_has_exactly_one_main_guard():
    """The file once carried a stale SECOND copy of this module; that is what
    made `from __future__ ...` fail mid-file. Exactly one guard now."""
    src = Path(rd.__file__).read_text(encoding='utf-8')
    guards = [ln for ln in src.splitlines()
              if ln.strip() == 'if __name__ == "__main__":']
    assert guards == ['if __name__ == "__main__":']


# ── phone identity: a name alone is not a fingerprint ───────────────────────


def test_normalize_phone_forms():
    for raw in ('+2348082508065', '2348082508065', '08082508065',
                '0808 250 8065', '234-808-250-8065'):
        assert rd.normalize_phone(raw) == '2348082508065'
    assert rd.normalize_phone('') == ''
    assert rd.normalize_phone(None) == ''


def test_phone_keys_handles_lists_and_stored_strings():
    assert rd.phone_keys('+2348082508065') == ['2348082508065']
    assert rd.phone_keys(['08082508065', '2348082508065']) == \
        ['2348082508065']
    # the store keeps several numbers space-separated in one column
    assert rd.phone_keys('2348082508065 2349122762716') == \
        ['2348082508065', '2349122762716']


def test_phone_matches_local_and_international():
    assert rd.phone_matches('+2348082508065', '08082508065')
    assert rd.phone_matches('2348082508065', '2348082508065')
    assert not rd.phone_matches('+2348082508065', '2349122762716')
    assert not rd.phone_matches('', '2348082508065')


def test_phones_from_hrefs_reads_tel_and_whatsapp():
    # the real Contact cell: tel: is the phone, wa.me is the WhatsApp
    hrefs = ['tel:2349011882044', 'https://wa.me/2349122762716',
             'https://wa.me/2349122762716?text=hi', 'mailto:x@y.z', '']
    assert rd.phones_from_hrefs(hrefs) == ['2349011882044', '2349122762716']
    assert rd.phones_from_hrefs([]) == []


def test_load_customer_phones_reads_both_columns(tmp_path):
    import openpyxl
    wb_path = tmp_path / "sheet.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Raw Message", "Customer Name", "Phone Number",
               "WhatsApp Number", "Action"])
    ws.append(["m1", "Ewumi Elizabeth", "07034541105", "07034541105",
               "Delivered"])
    ws.append(["m2", "Ken", "+2348082508065", "09122762716", "Delivered"])
    ws.append(["m3", "Gone", "08030000000", "", "Pending"])
    wb.save(str(wb_path))
    phones, headers = rd.load_customer_phones(wb_path)
    assert headers == ("Phone Number", "WhatsApp Number")
    assert phones["ewumi elizabeth"] == ["2347034541105"]
    assert phones["ken"] == ["2348082508065", "2349122762716"]
    assert "gone" not in phones          # only Delivered rows are used


def test_find_row_picks_the_row_whose_contact_matches():
    rows = [["Blessing Okon"], ["Blessing Okon"]]
    hrefs = [["tel:2348011111111"], ["tel:2348022222222"]]
    drv = _driver_for_rows(rows, hrefs)
    found = drv.find_row("Blessing Okon", ["+2348022222222"])
    assert found is drv.page.locator(rd.TABLE_ROW_DATA).nth(1)


def test_find_row_uses_the_first_row_when_no_phone_matches():
    rows = [["Blessing Okon"], ["Blessing Okon"]]
    hrefs = [["tel:2348011111111"], ["tel:2348022222222"]]
    drv = _driver_for_rows(rows, hrefs)
    found = drv.find_row("Blessing Okon", ["+2348099999999"])
    assert found is drv.page.locator(rd.TABLE_ROW_DATA).nth(0)


def test_find_row_matches_the_whatsapp_number_too():
    drv = _driver_for_rows([["Ken"]], [["https://wa.me/2349122762716"]])
    assert drv.find_row("Ken", ["+2349122762716"]) is not None
    assert drv.find_row("Ken", ["+2348000000000"]) is not None   # name only


def test_sqlite_store_keeps_phone_with_the_success(tmp_path):
    from apps.sniper.product_deliver import store as st
    db = st.SqliteDeliverStore("client_x", "This Month", tmp_path / "db")
    rec = db.record("Blessing", "ORD1", phone="+2348011111111")
    assert db.row("Blessing")["phone"] == "2348011111111"
    db.mark_success(rec, "ORD1", phone="+2348011111111")
    assert db.is_done("Blessing", "+2348011111111")
    assert db.is_done("Blessing", "08011111111")         # same subscriber
    assert not db.is_done("Blessing", "+2348022222222")  # a different person
    assert db.is_done("Blessing")                        # name only: as before
    db.close()


def test_legacy_success_without_a_phone_still_skips(tmp_path):
    from apps.sniper.product_deliver import store as st
    db = st.SqliteDeliverStore("client_x", "This Month", tmp_path / "db")
    rec = db.record("Blessing", "ORD1")
    db.mark_success(rec, "ORD1")                        # nothing to compare
    assert db.is_done("Blessing", "+2348022222222")
    db.close()


def test_json_store_checks_phone_identity_too(store_root):
    store = rd.DeliverStore("client_x", "This Month", store_root)
    rec = store.record("Blessing", phone="08011111111")
    store.mark_success(rec, "ORD1", phone="08011111111")
    assert store.is_done("Blessing", "+2348011111111")
    assert not store.is_done("Blessing", "+2348022222222")


# ── verify_delivered(): the clean-Save fast path ────────────────────────────


class _StubPage:
    def __init__(self):
        self.waits = 0

    def wait_for_timeout(self, ms):
        self.waits += 1


def _verify_driver(*, pending_rows=0, pending_status="Pending",
                   delivered_rows=0, box_holds=True):
    """A DeliverDriver with verify_delivered()'s browser work stubbed out.

    `pending_rows` / `delivered_rows` are the DATA-row counts the search
    reports per tab; state["delivered_calls"] counts Delivered-tab visits.
    """
    drv = rd.DeliverDriver.__new__(rd.DeliverDriver)
    drv.verbose = False
    drv.last_verify = ""
    drv.phones = {}
    drv.page = _StubPage()
    drv._log = lambda msg: None
    drv._say = lambda msg: None
    drv.customer_phones = lambda name: []
    drv._deadline_left = lambda started: 60.0
    drv.search_customer = lambda name: 0
    drv.read_row = lambda row: {"status": pending_status, "phones": [],
                                "phone": ""}
    state = {"tab": "Pending", "delivered_calls": 0}
    drv.state = state

    def open_tab(name):
        state["tab"] = name
        if name == "Delivered":
            state["delivered_calls"] += 1
        return 0

    def rows_now():
        return pending_rows if state["tab"] == "Pending" else delivered_rows

    drv.open_pending_tab = lambda: open_tab("Pending")
    drv.open_delivered_tab = lambda: open_tab("Delivered")
    drv._search_and_find = lambda name, phones=None: (
        (object() if rows_now() else None), (rows_now(), [], ""))
    drv.find_row = lambda name, phones=None: (
        object() if rows_now() else None)
    drv._search_box_holds = lambda name: box_holds
    return drv


def test_verify_clean_save_and_row_gone_is_delivered():
    """The headline speed-up: modal closed + no Pending row = moved."""
    drv = _verify_driver(pending_rows=0)
    assert drv.verify_delivered("Ken", 0.0, save_confirmed=True) is True
    assert drv.last_verify == "pending-gone"
    assert drv.state["delivered_calls"] == 0        # no Delivered-tab trip


def test_verify_row_still_pending_is_a_failure_after_one_more_look():
    drv = _verify_driver(pending_rows=1, pending_status="Pending")
    assert drv.verify_delivered("Ken", 0.0, save_confirmed=True) is False
    assert drv.page.waits == 1                      # the lag re-check ran
    assert drv.state["delivered_calls"] == 0


def test_verify_row_flips_to_delivered_on_the_lag_recheck():
    drv = _verify_driver(pending_rows=1, pending_status="Delivered")
    assert drv.verify_delivered("Ken", 0.0, save_confirmed=True) is True
    assert drv.last_verify == "row-delivered"


def test_verify_empty_table_without_the_name_in_the_box_checks_delivered():
    """0 rows is only proof when the search really filtered on the name."""
    drv = _verify_driver(pending_rows=0, delivered_rows=1, box_holds=False)
    assert drv.verify_delivered("Ken", 0.0, save_confirmed=True) is True
    assert drv.last_verify == "delivered-tab"
    assert drv.state["delivered_calls"] == 1


def test_verify_without_a_clean_save_checks_the_delivered_tab():
    drv = _verify_driver(pending_rows=0, delivered_rows=1)
    assert drv.verify_delivered("Ken", 0.0, save_confirmed=False) is True
    assert drv.last_verify == "delivered-tab"


def test_verify_gives_up_when_nothing_confirms(monkeypatch):
    # No clean Save and an empty Delivered tab: nothing can confirm the move.
    monkeypatch.setattr(rd.time, "sleep", lambda seconds: None)
    drv = _verify_driver(pending_rows=0, delivered_rows=0)
    assert drv.verify_delivered("Ken", 0.0, save_confirmed=False) is False
    # every bounded attempt tried the Delivered tab before giving up
    assert drv.state["delivered_calls"] == rd.VERIFY_ATTEMPTS


# ── sheet status rule: _select_work_rows + the three loaders ────────────────


def _status_sheet(tmp_path, rows, headers=("Customer Name", "Action")):
    import openpyxl
    wb_path = tmp_path / "sheet.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(list(headers))
    for row in rows:
        ws.append(list(row))
    wb.save(str(wb_path))
    return wb_path


def test_status_rule_marked_column_processes_only_delivered(tmp_path):
    path = _status_sheet(tmp_path, [
        ["Ada", "Scheduled"],
        ["Bola", "Pending"],
        ["Chidi", "Delivered"],
        ["", ""],                # junk row: no name -> never a data row
        ["Ngozi", ""],           # blank status: not work while others marked
        ["Efe", "Delivered"],
    ])
    assert rd.load_customers(path) == ["Chidi", "Efe"]


def test_status_rule_blank_column_means_every_row(tmp_path):
    path = _status_sheet(tmp_path,
                         [["Ada", ""], ["Bola", None], ["Chidi", ""]])
    assert rd.load_customers(path) == ["Ada", "Bola", "Chidi"]
    assert "entirely blank" in rd._selection_note


def test_status_rule_no_status_column_means_every_row(tmp_path):
    path = _status_sheet(tmp_path, [["Ada", "0801"], ["Bola", "0802"]],
                         headers=("Customer Name", "Phone"))
    assert rd.load_customers(path) == ["Ada", "Bola"]
    assert "no status column" in rd._selection_note


def test_status_rule_marked_but_zero_delivered_is_nothing(tmp_path):
    path = _status_sheet(tmp_path, [
        ["Ada", "Pending"], ["Bola", "Cancelled"], ["Chidi", "Scheduled"]])
    assert rd.load_customers(path) == []
    assert "NOTHING to process" in rd._selection_note
    assert "no row marked Delivered" in rd._selection_note


def test_status_rule_is_case_and_space_tolerant(tmp_path):
    path = _status_sheet(tmp_path,
                         [["Ada", " DELIVERED "], ["Bola", "delivered "]])
    assert rd.load_customers(path) == ["Ada", "Bola"]


def test_selection_summary_counts_what_it_ignored(tmp_path):
    path = _status_sheet(tmp_path, [
        ["Ada", "Delivered"], ["Bola", "Delivered"], ["Chidi", "Pending"],
        ["Dee", "Pending"], ["Efe", "Scheduled"], ["", "Delivered"]])
    assert rd.load_customers(path) == ["Ada", "Bola"]
    note = rd._selection_note
    assert "2 delivered" in note and "3 other" in note
    assert "2 pending" in note and "1 scheduled" in note
    assert "-> processing 2" in note


def test_price_and_phone_loaders_follow_the_same_rows(tmp_path):
    path = _status_sheet(tmp_path, [
        ["Ada", "Pending", "5000", "08011111111"],
        ["Bola", "Delivered", "7000", "08022222222"],
    ], headers=("Customer Name", "Action", "Price (₦)", "Phone Number"))
    prices, header, conflicts = rd.load_customer_prices(path)
    assert prices == {"bola": 7000} and not conflicts
    phones, headers_used = rd.load_customer_phones(path)
    assert set(phones) == {"bola"}


def test_tee_writes_to_every_stream_and_survives_a_broken_one():
    class Broken:
        def write(self, data):
            raise RuntimeError("disk full")

        def flush(self):
            raise RuntimeError("disk full")

    good = io.StringIO()
    tee = rd._Tee(Broken(), good)
    assert tee.write("hello\n") == 6
    assert good.getvalue() == "hello\n"
    tee.flush()                  # must not raise


def test_price_loader_drops_phone_numbers_in_the_price_cell(tmp_path):
    path = _status_sheet(tmp_path, [
        ["Ada", "Delivered", "28500", "08011111111"],
        # someone typed the customer's phone number into the Price cell:
        ["Bola", "Delivered", "07049528233", "07049528233"],
    ], headers=("Customer Name", "Action", "Price (₦)", "Phone Number"))
    prices, header, conflicts = rd.load_customer_prices(path)
    assert prices == {"ada": 28500}     # Bola's junk price is dropped, so
    assert not conflicts                # the CRM row Amount is used instead


def test_filtered_sn_column_keys_the_real_rows(tmp_path):
    # the filtered s/n column is the benchmark for a real data row: a row
    # with a name but NO s/n value is not real -- not work, not counted.
    path = _status_sheet(tmp_path, [
        [1, "Ada", "Delivered"],
        ["", "Bola", "Delivered"],       # no s/n -> not a real row
        [3, "Chidi", ""],                # real row, blank status: ignored
    ], headers=("Filtered #", "Customer Name", "Action"))
    assert rd.load_customers(path) == ["Ada"]
    assert "1 delivered" in rd._selection_note
    # an entirely blank status column: every s/n-keyed real row is work
    path = _status_sheet(tmp_path, [
        [1, "Ada", ""], [2, "Bola", None], ["", "Cee", ""]],
        headers=("Filtered #", "Customer Name", "Action"))
    assert rd.load_customers(path) == ["Ada", "Bola"]
    assert "entirely blank" in rd._selection_note
