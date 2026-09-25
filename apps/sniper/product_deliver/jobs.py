"""Per-client job tracking for deliver/scan runs (reuses WorkspaceManager).

The upload flow already owns the job lifecycle:

    clients/<id>/jobs/<job_id>/          job.json, source.<ext>,
                                         state/, runs/current/, locks/
    clients/<id>/jobs/archive/completed/<job_id>/     <- once done

The deliver flow used NONE of it, so a deliver job could not be tracked per
client. This consign attaches a deliver/scan run to the SAME job that owns
the input sheet -- found by source hash, so re-runs reuse it instead of
spamming new job directories -- and finishes it properly: manifest status,
plus the move into the completed archive once there is genuinely nothing
left to do ("done" is never claimed early).
"""
from __future__ import annotations

import hashlib
import json
import shutil
import time
from pathlib import Path

from apps.sniper.client_workspace import WorkspaceManager
from apps.sniper.product_deliver.common import atomic_write_json


JOB_SCHEMA_VERSION = 1


def _utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def file_sha256(path: Path) -> str:
    """sha256 of a source file (the job's identity, like the upload flow)."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def find_job_by_digest(manager: WorkspaceManager, client_id: str,
                       digest: str) -> str:
    """job_id of an existing job whose source hash matches, else ''.

    Searches the live jobs directory first, then the completed archive --
    the same order WorkspaceManager._candidate_job_dirs uses.
    """
    jobs_dir = manager.client_paths(client_id).jobs_dir
    for base in (jobs_dir, jobs_dir / "archive" / "completed"):
        if not base.is_dir():
            continue
        for manifest_path in sorted(base.glob("*/job.json")):
            try:
                manifest = json.loads(
                    manifest_path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            if manifest.get("source_sha256") == digest:
                return str(manifest.get("job_id") or manifest_path.parent.name)
    return ""


def ensure_deliver_job(manager: WorkspaceManager, client_id: str,
                       source_path: Path) -> tuple[str, Path, bool]:
    """(job_id, job_dir, created) for this input sheet.

    Reuses the job that already owns an identical source (a re-run never
    creates a second job for the same sheet); otherwise creates
    `job_<UTC ts>_<sha8>` -- the upload flow's naming -- with the same
    manifest schema, the source copied in, and the state/runs/locks
    sub-directories. Also makes sure `jobs/archive/completed/` exists, so
    every client has the tracking folder before its first completion.
    """
    paths = manager.client_paths(client_id)
    paths.jobs_dir.mkdir(parents=True, exist_ok=True)
    (paths.jobs_dir / "archive" / "completed").mkdir(parents=True,
                                                     exist_ok=True)
    digest = file_sha256(source_path)
    existing = find_job_by_digest(manager, client_id, digest)
    if existing:
        return existing, paths.jobs_dir / existing, False

    job_id = (f"job_{time.strftime('%Y%m%d_%H%M%S', time.gmtime())}"
              f"_{digest[:8]}")
    job_dir = paths.jobs_dir / job_id
    (job_dir / "state").mkdir(parents=True, exist_ok=True)
    (job_dir / "runs" / "current").mkdir(parents=True, exist_ok=True)
    (job_dir / "locks").mkdir(parents=True, exist_ok=True)
    suffix = source_path.suffix.lower()
    try:
        shutil.copyfile(source_path, job_dir / f"source{suffix}")
    except OSError:
        pass                       # a read-only source never blocks the run
    atomic_write_json(job_dir / "job.json", {
        "schema_version": JOB_SCHEMA_VERSION,
        "client_id": client_id,
        "job_id": job_id,
        "name_key": manager.name_key_for(client_id),
        "source_original_name": source_path.name,
        "source_extension": suffix,
        "source_sha256": digest,
        "status": "imported",
        "run_id": "current",
        "created_at": _utc_now(),
        "updated_at": _utc_now(),
    })
    return job_id, job_dir, True


def copy_artifacts(job_dir: Path, artifacts) -> list:
    """Copy run artifacts into the job's runs/current so an archived job is
    self-contained (report + duplicates CSV travel with it)."""
    target = job_dir / "runs" / "current"
    copied: list = []
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError:
        return copied
    for source in artifacts or []:
        source = Path(source)
        if not source.is_file():
            continue
        try:
            shutil.copyfile(source, target / source.name)
            copied.append(str(target / source.name))
        except OSError:
            continue
    return copied


def finish_deliver_job(manager: WorkspaceManager, client_id: str, job_id: str,
                       *, remaining: int, summary: str = "",
                       artifacts=(), keep: bool = False) -> Path | None:
    """Record the job's outcome; archive it when it is really finished.

    remaining > 0 -> manifest status "delivering" with the remaining count
    (the job stays live and obviously open); remaining == 0 -> status
    "completed" plus the move into jobs/archive/completed/<job_id>/ (unless
    keep=True). Returns the archive path, or None when it stays live.
    """
    job_dir = manager.client_paths(client_id).jobs_dir / job_id
    copied = copy_artifacts(job_dir, artifacts)
    if remaining > 0:
        manager.set_job_status(client_id, job_id, "delivering",
                               deliver_remaining=int(remaining),
                               deliver_summary=summary,
                               deliver_artifacts=copied,
                               deliver_updated_at=_utc_now())
        return None
    manager.set_job_status(client_id, job_id, "completed",
                           deliver_remaining=0,
                           deliver_summary=summary,
                           deliver_artifacts=copied,
                           deliver_completed_at=_utc_now())
    if keep:
        return None
    try:
        return manager.archive_completed_job(client_id, job_id)
    except FileExistsError:
        return (manager.client_paths(client_id).jobs_dir / "archive"
                / "completed" / job_id)

