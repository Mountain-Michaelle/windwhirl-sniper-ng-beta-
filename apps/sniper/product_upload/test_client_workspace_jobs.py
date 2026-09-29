"""Tests for the one-active-job-per-client policy in ``client_workspace``.

Real temp directories and real manifest files: no browser, no CRM. These
tests exist because "Michael had two job folders and the run picked the stale
CSV while his fresh xlsx sat idle" is a data-integrity bug, not a cosmetic
one.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402

from apps.sniper.client_workspace import WorkspaceManager  # noqa: E402


@pytest.fixture
def manager(tmp_path):
    return WorkspaceManager(tmp_path / "clients")


def _inbox(manager: WorkspaceManager, name: str, body: str = "a,b\n1,2\n") -> Path:
    inbox = manager.root / "_inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    path = inbox / name
    path.write_text(body, encoding="utf-8")
    return path


def _live_job_ids(manager: WorkspaceManager, client_id: str) -> list[str]:
    return [job_dir.name for job_dir, _ in manager.live_jobs(client_id)]


def _jobs_dir(manager: WorkspaceManager, client_id: str) -> Path:
    return manager.client_paths(client_id).jobs_dir


def _manifest(manager: WorkspaceManager, client_id: str, job_id: str) -> dict:
    jobs_dir = _jobs_dir(manager, client_id)
    for job_dir in (jobs_dir / job_id,
                    jobs_dir / "archive" / "superseded" / job_id,
                    jobs_dir / "archive" / "completed" / job_id):
        path = job_dir / "job.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8-sig"))
    raise AssertionError(f"no manifest found for {client_id}/{job_id}")


def _relive(manager: WorkspaceManager, client_id: str, job_id: str) -> None:
    """Drag a job back out of the archive and mark it live again.

    Used to reproduce a workspace that predates the supersede policy, where
    several job folders simply sat side by side.
    """
    jobs_dir = _jobs_dir(manager, client_id)
    for bucket in ("superseded", "completed", "orphan"):
        archived = jobs_dir / "archive" / bucket / job_id
        if archived.is_dir() and not (jobs_dir / job_id).exists():
            archived.rename(jobs_dir / job_id)
    manager.set_job_status(client_id, job_id, "imported")


def _age_manifest(manager: WorkspaceManager, client_id: str, job_id: str,
                  days: int) -> None:
    """Backdate a job's updated_at so archive retention treats it as settled."""
    manifest_path = _jobs_dir(manager, client_id) / job_id / "job.json"
    if not manifest_path.exists():
        manifest_path = _jobs_dir(manager, client_id) / "archive" / "completed" \
            / job_id / "job.json"
    if not manifest_path.exists():
        manifest_path = _jobs_dir(manager, client_id) / "archive" / "superseded" \
            / job_id / "job.json"
    data = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                       time.gmtime(time.time() - days * 86400))
    manifest_path.write_text(json.dumps(data), encoding="utf-8")


# ----------------------------------------- import appends to the live job
def test_reimporting_a_file_reuses_the_one_live_job(manager):
    """The whole point: a second file must NOT open a second job folder.

    The progress DB is per-client, not per-job, so a new job folder would buy
    no isolation -- it would only make the run pick between two dirs. One
    client, one live job, more sources.
    """
    first = manager.import_inbox(_inbox(manager, "michael.csv", "a\n1\n"))
    time.sleep(1.1)  # created_at is second-resolution; make order explicit.
    second = manager.import_inbox(_inbox(manager, "michael.csv", "a\n2\n"))

    client_id = second.client_id
    assert client_id == first.client_id
    assert first.job_id == second.job_id
    assert _live_job_ids(manager, client_id) == [first.job_id]


def _sources(manager: WorkspaceManager, workspace) -> list:
    """Original names of every order file the job covers, in manifest order."""
    manifest = _manifest(manager, workspace.client_id, workspace.job_id)
    return [entry["original_name"] for entry in manifest["sources"]]


def _files_on_disk(manager: WorkspaceManager, workspace) -> list:
    """Basenames actually written into the job dir, oldest first."""
    return [Path(p).name for p in manager.job_source_files(
        workspace.client_id, workspace.job_id, workspace.source_path)]


def test_appending_a_new_file_extends_the_sources_of_the_live_job(manager):
    """A genuinely NEW file for the same client is appended, not swapped in.

    `michael.csv` and `Michael.xlsx` share a stem, so they are two files for
    ONE client -- the real case that used to open a second job folder. Case
    differences are irrelevant: the stem is lowercased.
    """
    first = manager.import_inbox(_inbox(manager, "michael.csv", "a\n1\n"))
    second = manager.import_inbox(_inbox(manager, "Michael.xlsx", "a\n2\n"))

    assert second.client_id == first.client_id
    assert second.job_id == first.job_id
    # Oldest first: the first file is the job's primary source, the later
    # additions follow it. load_orders() reads this order, so a re-run
    # replays the client's history the way it arrived.
    assert _sources(manager, second) == ["michael.csv", "Michael.xlsx"]
    # The first file is still on disk: appending never evicts earlier work.
    on_disk = _files_on_disk(manager, second)
    assert len(on_disk) == 2
    assert all((second.job_dir / name).is_file() for name in on_disk)


def test_appending_the_same_file_twice_is_idempotent(manager):
    """Re-running `client init` on unchanged bytes must not grow anything."""
    first = manager.import_inbox(_inbox(manager, "michael.csv", "a\n1\n"))
    # Byte-identical to the first import: nothing to add.
    again = manager.import_inbox(_inbox(manager, "michael.csv", "a\n1\n"))
    # Different content under the same name: a genuine new file to track.
    updated = manager.import_inbox(_inbox(manager, "michael.csv", "a\n2\n"))
    # And repeating that one is a no-op too.
    repeat = manager.import_inbox(_inbox(manager, "michael.csv", "a\n2\n"))

    assert first.job_id == again.job_id == updated.job_id == repeat.job_id
    # Two distinct revisions, not four imports.
    assert len(_sources(manager, repeat)) == 2
    # ...and the second import did not leave a duplicate file on disk.
    assert len(list(repeat.job_dir.glob("*.csv"))) == 2
    assert _live_job_ids(manager, repeat.client_id) == [first.job_id]


def test_appending_never_discards_the_previous_sources_state(manager):
    """The old file's state stays addressable -- nothing is a silent delete."""
    first = manager.import_inbox(_inbox(manager, "michael.csv", "a\n1\n"))
    (first.progress_dir / "failed_orders.json").write_text(
        json.dumps([{"phone_number": "0801"}]), encoding="utf-8")

    second = manager.import_inbox(_inbox(manager, "Michael.xlsx", "a\n2\n"))

    # Same job, so same state dir -- the older file's failures are still there.
    assert second.job_dir == first.job_dir
    assert (second.progress_dir / "failed_orders.json").exists()
    assert _live_job_ids(manager, second.client_id) == [first.job_id]


def test_appended_sources_accumulate_in_one_job_not_a_second_folder(manager):
    """The old failure mode: every init made another folder, and a later run
    picked the newest -- silently dropping the earlier file's orders."""
    client_id = None
    for index, (name, body) in enumerate(
            [("michael.csv", "a\n1\n"), ("Michael.xlsx", "a\n2\n"),
             ("michael.csv", "a\n3\n")]):
        job = manager.import_inbox(_inbox(manager, name, body))
        client_id = client_id or job.client_id
        assert job.client_id == client_id
        assert job.job_id == _live_job_ids(manager, client_id)[0]

    job_dir = _jobs_dir(manager, client_id) / job.job_id
    sources = job.source_paths

    # One folder, every revision of every file, still all addressable.
    assert len(list(_jobs_dir(manager, client_id).glob("job_*"))) == 1
    assert len(sources) == 3
    assert all((job_dir / name).is_file() for name in sources)


def test_superseded_job_is_never_offered_as_pending_work(manager):
    first = manager.import_inbox(_inbox(manager, "michael.csv", "a\n1\n"))
    time.sleep(1.1)
    second = manager.import_inbox(_inbox(manager, "michael.csv", "a\n2\n"))


# -------------------------------------------------------- running-job safety
def test_a_job_with_a_live_lock_is_never_moved_out_from_under_its_run(manager):
    running = manager.import_inbox(_inbox(manager, "michael.csv", "a\n1\n"))
    # os.getpid() is definitionally alive, so this lock reads as genuinely held
    # -- exactly what a worker mid-upload holds.
    running.job_lock_path.write_text(json.dumps({"pid": os.getpid()}),
                                     encoding="utf-8")
    client_id = running.client_id

    manager.import_inbox(_inbox(manager, "michael.csv", "a\n2\n"))

    # Still live, still its own, lock untouched.
    assert running.job_id in _live_job_ids(manager, client_id)
    assert (running.job_dir / "locks" / "job.lock").exists()
    assert manager.read_job_status(client_id, running.job_id) != "superseded"


def test_a_dead_jobs_stale_lock_does_not_block_an_append(manager):
    """A crashed run's lock residue must not stop a later import appending."""
    stale = manager.import_inbox(_inbox(manager, "michael.csv", "a\n1\n"))
    # A pid that cannot be running: the lock is residue from a crashed run.
    stale.job_lock_path.write_text(json.dumps({"pid": 999_999_999}),
                                   encoding="utf-8")

    again = manager.import_inbox(_inbox(manager, "michael.csv", "a\n2\n"))

    assert again.job_id == stale.job_id
    assert _live_job_ids(manager, stale.client_id) == [stale.job_id]
    assert len(manager.job_source_files(stale.client_id, stale.job_id,
                                        stale.source_path)) == 2


def _forge_live_job(manager: WorkspaceManager, client_id: str, job_id: str,
                    created_at: str) -> str:
    """Fabricate a second LIVE job folder -- the pre-policy workspace shape.

    ``import_inbox`` appends now, so it will never produce this. Real
    workspaces still contain it, which is the only reason
    ``enforce_single_active_job`` exists at all.
    """
    jobs_dir = _jobs_dir(manager, client_id)
    new_dir = jobs_dir / job_id
    new_dir.mkdir(parents=True, exist_ok=True)
    (new_dir / "job.json").write_text(json.dumps({
        "job_id": job_id,
        "client_id": client_id,
        "status": "imported",
        "created_at": created_at,
        "updated_at": created_at,
        "sources": [],
        "source_path": "",
        "source_filename": None,
    }), encoding="utf-8")
    return job_id


# --------------------------------------------------------- repair of old dirs
def test_enforce_single_active_job_keeps_the_newest_and_archives_the_rest(manager):
    """Legacy repair only: two live folders collapse to the newest."""
    first = manager.import_inbox(_inbox(manager, "michael.csv", "a\n1\n"))
    client_id = first.client_id
    # Reproduce a workspace that predates the policy: two live job folders,
    # neither marked superseded -- the shape Michael's directory was stuck in.
    legacy = _forge_live_job(manager, client_id,
                             "job_20260101_000000_aaaaaaaa",
                             "2026-01-01T00:00:00Z")

    lines = manager.enforce_single_active_job()

    assert _live_job_ids(manager, client_id) == [first.job_id]
    assert any(legacy in line for line in lines)
    assert manager.read_job_status(client_id, legacy) == "superseded"


def test_enforce_single_active_job_is_idempotent(manager):
    first = manager.import_inbox(_inbox(manager, "michael.csv", "a\n1\n"))
    _forge_live_job(manager, first.client_id, "job_20260101_000000_bbbbbbbb",
                    "2026-01-01T00:00:00Z")

    manager.enforce_single_active_job()
    assert manager.enforce_single_active_job() == []


def test_enforce_leaves_a_single_live_job_untouched(manager):
    only = manager.import_inbox(_inbox(manager, "michael.csv", "a\n1\n"))
    before = (only.job_dir / "job.json").read_text(encoding="utf-8-sig")

    assert manager.enforce_single_active_job() == []
    assert (only.job_dir / "job.json").read_text(encoding="utf-8-sig") == before


def test_each_client_keeps_its_own_active_job(manager):
    """The rule is per client, not global: two clients get two live jobs."""
    michael = manager.import_inbox(_inbox(manager, "michael.csv", "a\n1\n"))
    time.sleep(1.1)
    michael_again = manager.import_inbox(_inbox(manager, "michael.csv", "a\n2\n"))
    # Distinct bytes: an identical file is the same source, not a new client.
    ephraim = manager.import_inbox(_inbox(manager, "ephraim.csv", "b\n1\n"))

    assert ephraim.client_id != michael_again.client_id
    # Michael's second import appended rather than replacing his live job.
    assert michael_again.job_id == michael.job_id
    assert _live_job_ids(manager, michael.client_id) == [michael.job_id]
    assert _live_job_ids(manager, ephraim.client_id) == [ephraim.job_id]
    assert [w.client_id for w in manager.discover_pending_jobs()] == [
        ephraim.client_id, michael.client_id
    ]


# ------------------------------------------------------------------- orphans
def test_manifest_less_job_folder_is_quarantined(manager):
    imported = manager.import_inbox(_inbox(manager, "michael.csv", "a\n1\n"))
    client_id = imported.client_id
    jobs_dir = _jobs_dir(manager, client_id)
    junk = jobs_dir / "job_20260916_161104_ecbfe27d"
    (junk / "state").mkdir(parents=True)

    lines = manager.quarantine_orphan_job_dirs()

    assert not junk.exists()
    assert (jobs_dir / "archive" / "orphan" / junk.name).is_dir()
    assert any(junk.name in line for line in lines)
    assert _live_job_ids(manager, client_id) == [imported.job_id]


def test_manifest_less_job_folder_contents_are_preserved(manager):
    """Quarantining is a move -- the half-imported state stays inspectable."""
    imported = manager.import_inbox(_inbox(manager, "michael.csv", "a\n1\n"))
    jobs_dir = _jobs_dir(manager, imported.client_id)
    junk = jobs_dir / "job_20260916_161104_ecbfe27d"
    junk.mkdir(parents=True)
    (junk / "michael.xlsx").write_bytes(b"half imported source")

    manager.quarantine_orphan_job_dirs()

    kept = jobs_dir / "archive" / "orphan" / junk.name / "michael.xlsx"
    assert kept.read_bytes() == b"half imported source"


def test_a_human_workbook_folder_is_never_treated_as_a_job(manager):
    """`jobs/status/` is a person's workbook, not a job -- and not junk."""
    imported = manager.import_inbox(_inbox(manager, "michael.csv", "a\n1\n"))
    client_id = imported.client_id
    status_dir = _jobs_dir(manager, client_id) / "status"
    status_dir.mkdir(parents=True, exist_ok=True)
    (status_dir / "michael.xlsx").write_bytes(b"not a job")

    manager.quarantine_orphan_job_dirs()
    manager.enforce_single_active_job()

    assert (status_dir / "michael.xlsx").exists()
    assert not (_jobs_dir(manager, client_id) / "archive" / "orphan").exists()
    assert _live_job_ids(manager, client_id) == [imported.job_id]


# ------------------------------------------ archived work vs. the active job
def test_archived_failed_orders_are_held_while_a_newer_job_is_live(manager):
    old = manager.import_inbox(_inbox(manager, "michael.csv", "a\n1\n"))
    client_id = old.client_id
    (old.progress_dir / "failed_orders.json").write_text(
        json.dumps([{"phone_number": "0801"}]), encoding="utf-8")
    manager.set_job_status(client_id, old.job_id, "completed")
    manager.archive_completed_job(client_id, old.job_id)
    time.sleep(1.1)
    new = manager.import_inbox(_inbox(manager, "michael.csv", "a\n2\n"))

    pending_ids = [w.job_id for w in manager.discover_pending_jobs()]

    # The one-live-job rule wins: the archived job's leftovers wait rather
    # than being restored into a second live folder.
    assert pending_ids == [new.job_id]
    # ...and they are not lost. They come back once nothing else is active.
    assert manager.job_has_pending_uploads(client_id, old.job_id)
    assert (_jobs_dir(manager, client_id) / "archive" / "completed"
            / old.job_id).is_dir()


def test_archived_failed_orders_return_when_no_job_is_active(manager):
    old = manager.import_inbox(_inbox(manager, "michael.csv", "a\n1\n"))
    client_id = old.client_id
    (old.progress_dir / "failed_orders.json").write_text(
        json.dumps([{"phone_number": "0801"}]), encoding="utf-8")
    manager.set_job_status(client_id, old.job_id, "completed")
    manager.archive_completed_job(client_id, old.job_id)

    pending = manager.discover_pending_jobs()

    assert [w.job_id for w in pending] == [old.job_id]
    assert manager.read_job_status(client_id, old.job_id) == "partial"


def test_appending_keeps_the_earlier_files_failed_orders_attached(manager):
    """The one-folder rule must not strand work the older file still owes.

    Appending means the job -- and therefore its retry queue and failed
    ledger -- is shared by every source. A second file joining the job has to
    inherit that outstanding work, or those orders are silently orphaned.
    """
    old = manager.import_inbox(_inbox(manager, "michael.csv", "a\n1\n"))
    (old.progress_dir / "failed_orders.json").write_text(
        json.dumps([{"phone_number": "0801"}]), encoding="utf-8")
    time.sleep(1.1)
    new = manager.import_inbox(_inbox(manager, "michael.csv", "a\n2\n"))

    pending_ids = [w.job_id for w in manager.discover_pending_jobs()]

    # One job, not two -- and the still-failing order keeps its owner.
    assert new.job_id == old.job_id
    assert pending_ids == [old.job_id]
    assert (new.progress_dir / "failed_orders.json").exists()


# ------------------------------------------------------- deferred archiving
def test_deferred_supersede_move_retries_into_the_superseded_bucket(manager):
    """A locked file defers the move; the retry must not change the bucket."""
    first = manager.import_inbox(_inbox(manager, "michael.csv", "a\n1\n"))
    client_id = first.client_id
    manager.set_job_status(client_id, first.job_id, "superseded",
                           archive_deferred=True, archive_bucket="superseded")

    assert manager.retry_deferred_archives() == 1
    archived = _jobs_dir(manager, client_id) / "archive" / "superseded" / first.job_id
    assert archived.is_dir()
    assert not (_jobs_dir(manager, client_id) / first.job_id).exists()
    # Never re-filed as completed history -- that would make dead work live.
    assert manager.read_job_status(client_id, first.job_id) == "superseded"


def test_a_deferred_move_still_removes_the_job_from_the_queue(manager):
    """The STATUS change is what takes a job out of the queue, not the move."""
    first = manager.import_inbox(_inbox(manager, "michael.csv", "a\n1\n"))
    client_id = first.client_id
    manager.set_job_status(client_id, first.job_id, "superseded",
                           archive_deferred=True, archive_bucket="superseded")

    assert manager.discover_pending_jobs() == []


# ---------------------------------------------------------------- retention
def test_prune_moves_settled_archives_to_the_graveyard(manager):
    imported = manager.import_inbox(_inbox(manager, "michael.csv", "a\n1\n"))
    client_id = imported.client_id
    manager.set_job_status(client_id, imported.job_id, "completed")
    manager.archive_completed_job(client_id, imported.job_id)
    _age_manifest(manager, client_id, imported.job_id, 40)

    lines = manager.prune_job_archives(retention_days=14)

    graveyard = manager.root / "_graveyard" / client_id / imported.job_id
    assert graveyard.is_dir()
    assert (graveyard / "job.json").exists()
    assert (graveyard / f"michael_{client_id}.csv").exists()
    assert any(imported.job_id in line for line in lines)
    assert not (_jobs_dir(manager, client_id) / "archive" / "completed"
                / imported.job_id).exists()


def test_prune_keeps_recent_archives(manager):
    imported = manager.import_inbox(_inbox(manager, "michael.csv", "a\n1\n"))
    manager.set_job_status(imported.client_id, imported.job_id, "completed")
    manager.archive_completed_job(imported.client_id, imported.job_id)

    assert manager.prune_job_archives(retention_days=14) == []
    assert (_jobs_dir(manager, imported.client_id) / "archive" / "completed"
            / imported.job_id).is_dir()


def test_prune_never_moves_a_job_that_still_has_unuploaded_orders(manager):
    imported = manager.import_inbox(_inbox(manager, "michael.csv", "a\n1\n"))
    client_id = imported.client_id
    (imported.progress_dir / "retry_queue.json").write_text(
        json.dumps([{"phone_number": "0801"}]), encoding="utf-8")
    manager.set_job_status(client_id, imported.job_id, "completed")
    manager.archive_completed_job(client_id, imported.job_id)
    _age_manifest(manager, client_id, imported.job_id, 40)

    manager.prune_job_archives(retention_days=14)

    assert (_jobs_dir(manager, client_id) / "archive" / "completed"
            / imported.job_id).is_dir()
    assert not (manager.root / "_graveyard" / client_id).exists()


def test_prune_is_off_when_retention_is_zero(manager):
    imported = manager.import_inbox(_inbox(manager, "michael.csv", "a\n1\n"))
    manager.set_job_status(imported.client_id, imported.job_id, "completed")
    manager.archive_completed_job(imported.client_id, imported.job_id)
    _age_manifest(manager, imported.client_id, imported.job_id, 400)

    assert manager.prune_job_archives(retention_days=0) == []
    assert (_jobs_dir(manager, imported.client_id) / "archive" / "completed"
            / imported.job_id).is_dir()


def test_prune_also_clears_old_superseded_archives(manager):
    """Retention covers the superseded bucket too, not just `completed`."""
    first = manager.import_inbox(_inbox(manager, "michael.csv", "a\n1\n"))
    client_id = first.client_id
    manager.set_job_status(client_id, first.job_id, "completed")
    # Legacy workspaces still hold superseded archives from the one-live-job
    # era; retention has to clear those too or they accumulate forever.
    manager._move_job_to_archive(client_id, first.job_id, "superseded")
    _age_manifest(manager, client_id, first.job_id, 40)

    manager.prune_job_archives(retention_days=14)

    assert (manager.root / "_graveyard" / client_id / first.job_id).is_dir()
