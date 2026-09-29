"""Client/job-scoped filesystem lifecycle for the Sniper uploader.

This module owns paths and import state only. It deliberately contains no
Playwright or order-upload logic.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

# The repo root has to be importable before the absolute `apps.sniper.*`
# import below. Two ways this module gets loaded:
#   * as a package module  -- `from apps.sniper.client_workspace import ...`
#     (run_upload, product_deliver, the test suite): the root is already on
#     sys.path.
#   * as a top-level module -- `python client_manager.py init` run from inside
#     apps/sniper, where client_manager's `from client_workspace import ...`
#     fallback puts ONLY apps/sniper on sys.path, and `apps` does not resolve.
# That second path is how the CLI is documented to be run, so this module has
# to bootstrap the root itself rather than assume a caller did it. Same
# _PROJECT_ROOT idiom as run_upload.py / run_deliver.py / migrate_progress_to_db.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from apps.sniper.conditional_wait import poll_until  # noqa: E402

ORDER_FILE_EXTENSIONS = (".csv", ".xlsx", ".xls")
# How long a lock file that exists but won't parse is given to become
# readable. The owner writes its pid in a single os.write right after the
# atomic create, so this only has to cover that write; anything still
# unreadable after it is treated as a genuinely corrupt lock (stale).
_LOCK_WRITE_GRACE_S = 0.5
# Total budget for acquiring the client-registry lock. Identical to the old
# 100 x 0.05s loop it replaces; only the retry granularity changed.
_REGISTRY_LOCK_WAIT_S = 5.0
_DUPLICATE_SUFFIX_RE = re.compile(
    r"(\s*\(\d+\)\s*$)|(\s*-\s*copy\s*\d*\s*$)|(\s*_copy\s*\d*\s*$)",
    re.IGNORECASE,
)
_ENV_VAR_RE = re.compile(r"^([A-Z0-9_]+)_(EMAIL|PASSWORD)=", re.MULTILINE)

# --- single-active-job policy -------------------------------------------------
# Every client keeps EXACTLY ONE live job folder. A job leaves the live jobs
# directory through exactly one of three archive buckets, and each bucket is
# terminal -- nothing in `archive/` is ever picked up as work again unless it
# is a *completed* job whose durable state still holds un-uploaded orders
# (see discover_pending_jobs).
#   completed  -- the job finished; history, may be restored if uploads remain
#   superseded -- a newer inbox file replaced it; never restored
#   orphan     -- a job_* directory with no manifest (import crashed); junk
ARCHIVE_BUCKETS = ("completed", "superseded", "orphan")
# A live directory only counts as a job when it carries this prefix AND a
# job.json. Both guards matter: `jobs/status/` holds a human workbook and must
# never be mistaken for a job or swept into the orphan bucket.
MANAGED_JOB_GLOB = "job_*"
# Archived jobs with nothing left to upload are moved out of the client tree
# into `clients/_graveyard/` once they are this old. A move, never a delete --
# the immutable source copy inside a job is data, and retention should make
# history out of the way, not destroy it.
ARCHIVE_RETENTION_DAYS = 14
GRAVEYARD_DIRNAME = "_graveyard"


def _read_json_file(path: Path):
    """Defensive JSON reader for durable state files: returns the parsed
    value, or None for a missing/unreadable/corrupt file. Callers only
    use the result's truthiness and dict-ness, so the distinction
    between "absent" and "corrupt" collapses to None."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _job_order_key(job_dir: Path, manifest: dict) -> tuple:
    """Sort key putting a client's jobs oldest-first, so "the newest one" is
    always simply the last entry of `live_jobs()`.

    Prefers the manifest's created_at (stable, survives a directory copy) and
    falls back to directory mtime for manifests written before the field
    existed; ties break on the job id, which embeds its own timestamp.
    """
    created = str(manifest.get("created_at") or "")
    stamp = _parse_utc_stamp(created)
    if stamp is None:
        try:
            return (job_dir.stat().st_mtime, job_dir.stat().st_mtime,
                    str(manifest.get("job_id") or job_dir.name))
        except OSError:
            return (0.0, 0.0, str(manifest.get("job_id") or job_dir.name))
    try:
        # Second-resolution created_at is the primary key, but two files can
        # be imported inside the same second; the directory's sub-second
        # mtime is what actually separates them, so the newest import still
        # wins instead of an arbitrary digest order.
        fine = job_dir.stat().st_mtime
    except OSError:
        fine = stamp
    return (stamp, fine, str(manifest.get("job_id") or job_dir.name))


def _parse_utc_stamp(value: str) -> Optional[float]:
    """Epoch seconds for the manifests' `%Y-%m-%dT%H:%M:%SZ` timestamps."""
    if not value:
        return None
    try:
        return time.mktime(time.strptime(value, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, OverflowError):
        return None


def _archive_timestamp(job_dir: Path, manifest: dict) -> Optional[float]:
    """When a job last changed state -- used only by archive retention."""
    for key in ("updated_at", "archived_at", "created_at"):
        stamp = _parse_utc_stamp(str(manifest.get(key) or ""))
        if stamp is not None:
            return stamp
    try:
        return job_dir.stat().st_mtime
    except OSError:
        return None


def _job_lock_is_held(job_dir: Path) -> bool:
    """True when a live process currently owns this job's lock.

    An unparseable lock is treated as held: the only thing worse than leaving
    one stale job folder around is yanking a directory out from under a run
    that is mid-upload.
    """
    lock = job_dir / "locks" / "job.lock"
    if not lock.exists():
        return False
    return _lock_owner_verdict(lock) is not False


class AmbiguousClientError(ValueError):
    pass


class DuplicateJobError(ValueError):
    pass


def _lock_owner_verdict(path: Path) -> Optional[bool]:
    """Decide whether a lock file's owner is alive.

    Returns:
      True  -- the file parsed and its pid is running: the lock is real.
      False -- the file parsed and its pid is gone: definitively stale.
      None  -- the file does not parse *yet*; give it a moment.

    The three-way result is what makes the dynamic wait safe. A live owner
    writes its pid in a single os.write right after the atomic create, so
    "exists but unparseable" is almost always "caught mid-write". The old
    code slept a fixed 100ms and re-read exactly ONCE, so a lock that
    finished writing even a microsecond later was still misread as corrupt
    and stolen from a live owner. Polling only the ambiguous None case
    closes that window, while still answering True/False the instant it is
    knowable -- a definitively dead owner never pays the grace period.
    """
    try:
        owner = json.loads(path.read_text(encoding="utf-8"))
        pid = int(owner.get("pid", 0))
    except (OSError, ValueError, TypeError, AttributeError,
            json.JSONDecodeError):
        return None          # unreadable: ambiguous, keep waiting
    return _pid_alive(pid)


def _pid_alive(pid: int) -> bool:
    """Portable liveness check for lock-file owners.

    os.kill(pid, 0) works on POSIX but raises SystemError (not OSError)
    on some Windows builds, which used to escape JobLock's stale-owner
    recovery and loop forever. This helper catches everything and falls
    back to a native OpenProcess probe on Windows.
    """
    try:
        if pid == os.getpid():
            return True
    except Exception:
        pass
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
    except Exception:
        pass
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x00100000, False, int(pid))
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True
    except Exception:
        return True  # cannot verify: err on the side of "still running"


class JobLock:
    """Exclusive process lock for one client job."""
    def __init__(self, path: Path):
        self.path = path
        self.fd: Optional[int] = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except FileExistsError as exc:
            verdict = _lock_owner_verdict(self.path)
            if verdict is None:
                # Exists but unreadable -- almost certainly the owner is still
                # mid-write. DYNAMIC WAIT (was a flat time.sleep(0.1) followed
                # by exactly ONE re-read, which could still lose the race and
                # steal a lock from a live owner). Only the ambiguous case is
                # polled; a live or a dead owner is decided immediately.
                verdict = poll_until(lambda: _lock_owner_verdict(self.path),
                                     _LOCK_WRITE_GRACE_S)
            # Only a *live* owner means "already running". False and None are
            # both recoverable: a dead owner and a file that never became
            # parseable both fall through to the stale-takeover below, exactly
            # as the old single-re-read logic did.
            if verdict is True:
                raise RuntimeError(f"job is already running: {self.path}") from exc
            self.path.unlink(missing_ok=True)
            self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
        os.write(self.fd, json.dumps({
            "pid": os.getpid(),
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }).encode("utf-8"))
        return self

    def __exit__(self, *_):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


def name_key_from_filename(filename: str) -> str:
    stem = _DUPLICATE_SUFFIX_RE.sub("", Path(filename).stem).strip().lower()
    stem = re.sub(r"[^a-z0-9]+", "_", stem)
    stem = re.sub(r"_+", "_", stem).strip("_")
    if not stem:
        raise ValueError(f"Could not derive a client name from {filename!r}")
    return stem


def derive_credential_var_prefix(name_key: str, canonical_client_id: str) -> str:
    """Create the unique, human-readable prefix once per client."""
    suffix = canonical_client_id.rsplit("_", 1)[-1]
    if not re.fullmatch(r"[0-9a-fA-F]{8}", suffix):
        raise ValueError(f"Canonical client ID has no valid suffix: {canonical_client_id}")
    readable = re.sub(r"[^A-Za-z0-9]+", "_", name_key).strip("_").upper()
    if not readable:
        raise ValueError(f"Cannot derive credential prefix from name key: {name_key!r}")
    return f"{readable}_{suffix.upper()}"


def _atomic_json_write(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _manifest_source_digests(manifest: dict) -> set[str]:
    """Every content digest a job manifest claims to own.

    A job may cover several order files (see ``import_inbox``), so the legacy
    single ``source_sha256`` field is only the FIRST source. Reading just that
    field would let the same file be imported twice once a second source is
    appended, so every consumer of "has this file already been imported?"
    must ask this helper instead.
    """
    digests: set[str] = set()
    primary = manifest.get("source_sha256")
    if primary:
        digests.add(primary)
    for entry in manifest.get("sources") or []:
        if isinstance(entry, dict) and entry.get("sha256"):
            digests.add(entry["sha256"])
    return digests


def _manifest_source_names(manifest: dict) -> list[str]:
    """Ordered, stored filenames of every source a job covers, oldest first.

    Falls back to the single legacy ``source_filename`` for manifests written
    before multi-source jobs existed, so old jobs keep resolving without a
    migration pass.
    """
    names: list[str] = []
    for entry in manifest.get("sources") or []:
        if isinstance(entry, dict) and entry.get("filename"):
            names.append(entry["filename"])
    if not names and manifest.get("source_filename"):
        names.append(manifest["source_filename"])
    return names


def _unique_destination(job_dir: Path, stem: str, suffix: str) -> Path:
    """A free, descriptive source filename inside ``job_dir``.

    The first file of a job keeps the historical ``<name>_<client_id><ext>``
    name so existing reports and the delivery side keep resolving it. Every
    appended file gets its digest appended, which is unique per content and
    therefore idempotent: re-importing the same content is rejected earlier by
    the digest check rather than colliding here.
    """
    candidate = job_dir / f"{stem}{suffix}"
    if not candidate.exists():
        return candidate
    n = 2
    while (job_dir / f"{stem}_{n}{suffix}").exists():
        n += 1
    return job_dir / f"{stem}_{n}{suffix}"


@dataclass(frozen=True)
class ClientPaths:
    client_id: str
    root: Path
    profile_path: Path
    auth_dir: Path
    catalog_dir: Path
    state_dir: Path
    client_progress_db: Path
    jobs_dir: Path
    browser_dir: Path


@dataclass(frozen=True)
class ClientWorkspace:
    root: Path
    client_id: str
    job_id: str
    job_dir: Path
    source_path: Path
    auth_dir: Path
    catalog_dir: Path
    progress_dir: Path
    client_progress_db: Path
    diagnostics_dir: Path
    run_dir: Path
    job_lock_path: Path
    # Every order file this job covers, oldest first. `source_path` is the
    # FIRST entry and stays authoritative for anything that still treats the
    # job as single-file (plan/report headers, the delivery side, the CRM
    # page title). The upload runner is the one consumer that must read the
    # whole tuple -- see load_orders_multi. Kept as a tuple so the frozen
    # dataclass stays hashable and callers cannot mutate it behind our back.
    source_paths: tuple = ()


class WorkspaceManager:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or Path(__file__).resolve().parent / "clients").resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        for name in ("_inbox", "_rejected", "_needs_disambiguation"):
            (self.root / name).mkdir(exist_ok=True)
        (self.root / "_inbox" / ".claimed").mkdir(parents=True, exist_ok=True)
        self.registry_path = self.root / "_registry.json"
        self.registry_lock = self.root / "_registry.lock"

    def _load_registry(self) -> dict:
        if not self.registry_path.exists():
            return {"schema_version": 1, "name_keys": {}}
        # Accept registries written by Windows editors that include a UTF-8 BOM.
        data = json.loads(self.registry_path.read_text(encoding="utf-8-sig"))
        if not isinstance(data, dict):
            raise ValueError(f"Invalid client registry: {self.registry_path}")
        data.setdefault("schema_version", 1)
        data.setdefault("name_keys", {})
        return data

    def _with_registry_lock(self):
        # Windows-compatible best-effort exclusive lock without a new
        # dependency. The lock file is created atomically and removed by the
        # owner. Stale locks are recoverable through explicit cleanup.
        class _Lock:
            def __init__(self, path: Path):
                self.path = path
                self.fd = None

            def __enter__(self):
                # DYNAMIC WAIT (was `for _ in range(100): ... time.sleep(0.05)`
                # -- a flat 50ms tick, up to 100 of them). The lock is released
                # by unlinking the file, which happens whenever it happens, so
                # the old loop kept sleeping in 50ms quanta after the
                # contention was already over. poll_until retries on a short
                # backing-off schedule and returns the instant the create
                # succeeds.
                #
                # Unchanged: the same 100 x 0.05s = 5s budget, and the same
                # TimeoutError with the same message when it expires.
                got = poll_until(self._try_create, _REGISTRY_LOCK_WAIT_S)
                if got is None:
                    raise TimeoutError(
                        f"Timed out acquiring registry lock {self.path}")
                return self

            def _try_create(self):
                """One non-blocking attempt: returns self on success, None if
                the lock is currently held by someone else."""
                try:
                    self.fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
                except FileExistsError:
                    return None
                return self

            def __exit__(self, *_):
                if self.fd is not None:
                    os.close(self.fd)
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
        return _Lock(self.registry_lock)

    def resolve_canonical_client_id(self, name_key: str, allow_create: bool = True) -> str:
        with self._with_registry_lock():
            registry = self._load_registry()
            values = registry["name_keys"].get(name_key, [])
            if isinstance(values, str):
                values = [values]
            if len(values) > 1:
                raise AmbiguousClientError(
                    f"name_key {name_key!r} maps to multiple clients: {values}"
                )
            if len(values) == 1:
                return values[0]
            if not allow_create:
                raise KeyError(f"No client registered for {name_key!r}")
            client_id = f"client_{name_key}_{secrets.token_hex(4)}"
            registry["name_keys"][name_key] = [client_id]
            _atomic_json_write(self.registry_path, registry)
            return client_id

    def register_client(self, name_key: str, client_id: str) -> str:
        """Register an explicit stable ID, rejecting conflicting mappings."""
        with self._with_registry_lock():
            registry = self._load_registry()
            values = registry["name_keys"].get(name_key, [])
            if isinstance(values, str):
                values = [values]
            if values and client_id not in values:
                raise AmbiguousClientError(
                    f"name_key {name_key!r} is already mapped to {values}"
                )
            registry["name_keys"][name_key] = [client_id]
            _atomic_json_write(self.registry_path, registry)
        return client_id

    def client_paths(self, client_id: str) -> ClientPaths:
        root = (self.root / client_id).resolve()
        if root.parent != self.root:
            raise ValueError("Invalid client ID/path")
        return ClientPaths(
            client_id=client_id,
            root=root,
            profile_path=root / "profile" / "client.yaml",
            auth_dir=root / "auth",
            catalog_dir=root / "catalog",
            state_dir=root / "state",
            client_progress_db=root / "state" / "client_progress.db",
            jobs_dir=root / "jobs",
            browser_dir=root / "browser",
        )

    def shared_credentials_env_path(self) -> Path:
        return self.root.parent.parent / ".env"

    def _append_credentials_block(self, client_id: str, name_key: str, prefix: str) -> None:
        env_path = self.shared_credentials_env_path()
        lock_path = self.root / "_credentials.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except FileExistsError as exc:
            raise RuntimeError(f"credentials file is being updated: {env_path}") from exc
        try:
            existing = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
            existing_vars = set(_ENV_VAR_RE.findall(existing))
            need_email = (prefix, "EMAIL") not in existing_vars
            need_password = (prefix, "PASSWORD") not in existing_vars
            if not need_email and not need_password:
                return
            block = "\n".join((
                "",
                "# -------------------------------------------------------------------",
                f"# Client: {client_id}",
                f"# Name key: {name_key}",
                f"# Credential reference: env:{prefix}",
                "# Fill CHANGE_ME values manually. Keep this file private.",
                "# -------------------------------------------------------------------",
            ))
            missing_lines = []
            if need_email:
                missing_lines.append(f"{prefix}_EMAIL=CHANGE_ME")
            if need_password:
                missing_lines.append(f"{prefix}_PASSWORD=CHANGE_ME")
            block = block + "\n" + "\n".join(missing_lines) + "\n"
            env_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = env_path.with_name(env_path.name + ".tmp")
            tmp.write_text(existing.rstrip() + block, encoding="utf-8")
            os.replace(tmp, env_path)
        finally:
            os.close(fd)
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass

    def ensure_client_provisioned(self, client_id: str, name_key: Optional[str] = None,
                                  credential_reference: Optional[str] = None) -> ClientPaths:
        paths = self.client_paths(client_id)
        effective_name_key = name_key or client_id
        existing_reference = self.credential_reference(client_id)
        if existing_reference and existing_reference.startswith("env:"):
            prefix = existing_reference[4:]
        else:
            prefix = derive_credential_var_prefix(effective_name_key, client_id)
            if credential_reference and credential_reference.startswith("env:"):
                requested = credential_reference[4:]
                if requested != prefix:
                    raise ValueError(
                        f"Credential reference must be env:{prefix} for {client_id}; "
                        f"received {credential_reference}"
                    )
        for directory in (
            paths.profile_path.parent, paths.auth_dir, paths.catalog_dir,
            paths.state_dir, paths.jobs_dir, paths.browser_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        if not paths.profile_path.exists():
            created = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            paths.profile_path.write_text(
                "\n".join((
                    f"client_id: {client_id}",
                    f"name_key: {name_key or client_id}",
                    f"display_name: {name_key or client_id}",
                    f"created_at: \"{created}\"",
                    "enabled: true",
                    f"credential_reference: env:{prefix}",
                    "crm_account_id: null",
                    "# Configure crm_account_id before uploading.",
                    "",
                )),
                encoding="utf-8",
            )
        self._append_credentials_block(client_id, effective_name_key, prefix)
        return paths

    def credential_reference(self, client_id: str) -> Optional[str]:
        """Read only the non-secret credential reference from client.yaml."""
        profile = self.client_paths(client_id).profile_path
        if not profile.exists():
            return None
        for line in profile.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition(":")
            if separator and key.strip() == "credential_reference":
                value = value.strip().strip('"').strip("'")
                return value or None
        return None

    def profile_values(self, client_id: str) -> dict[str, str]:
            profile = self.client_paths(client_id).profile_path
            values: dict[str, str] = {}
            if profile.exists():
                for line in profile.read_text(encoding="utf-8").splitlines():
                    key, separator, value = line.partition(":")
                    if separator:
                        values[key.strip()] = value.strip().strip('"').strip("'")
            return values

    def credential_audit(self, client_id: str, refresh: bool = False) -> list[str]:
        """Validate credentials and refresh the local JSON login cache.

        Refresh requires the shared .env so a deliberate refresh cannot
        overwrite a working cache with missing values.
        """
        from dotenv import dotenv_values

        values = self.profile_values(client_id)
        reference = values.get("credential_reference", "")
        problems = []
        env_path = self.shared_credentials_env_path()
        if not reference.startswith("env:"):
            return [f"{client_id}: invalid credential_reference"]
        prefix = reference[4:].strip()
        cache_path = self.client_paths(client_id).auth_dir / "credentials.json"
        env_values = dotenv_values(env_path) if env_path.exists() else {}
        email = str(env_values.get(f"{prefix}_EMAIL") or "").strip()
        password = str(env_values.get(f"{prefix}_PASSWORD") or "").strip()
        usable_env = bool(email and password and email != "CHANGE_ME" and password != "CHANGE_ME")
        if refresh:
            if not env_path.exists():
                return [f"{client_id}: shared credentials file is missing: {env_path}"]
            if not usable_env:
                return [
                    f"{client_id}: missing usable {prefix}_EMAIL or {prefix}_PASSWORD "
                    f"in {env_path}"
                ]
            self._write_credential_cache(client_id, reference, email, password)
            return []
        if usable_env:
            if not cache_path.exists():
                self._write_credential_cache(client_id, reference, email, password)
            return []
        if cache_path.exists():
            try:
                cached = json.loads(cache_path.read_text(encoding="utf-8-sig"))
                cached_email = str(cached.get("email") or "").strip()
                cached_password = str(cached.get("password") or "").strip()
                if cached.get("credential_reference") == reference and cached_email and cached_password:
                    return []
            except (OSError, json.JSONDecodeError, AttributeError):
                pass
        return [f"{client_id}: no usable .env credentials or validated credentials.json cache"]

    def _write_credential_cache(self, client_id: str, reference: str,
                                email: str, password: str) -> None:
        cache_path = self.client_paths(client_id).auth_dir / "credentials.json"
        _atomic_json_write(cache_path, {
            "schema_version": 1,
            "credential_reference": reference,
            "email": email,
            "password": password,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        fingerprint_path = self.client_paths(client_id).auth_dir / "credential_fingerprint.json"
        _atomic_json_write(fingerprint_path, {
            "credential_reference": reference,
            "email_sha256": hashlib.sha256(email.encode("utf-8")).hexdigest(),
            "password_sha256": hashlib.sha256(password.encode("utf-8")).hexdigest(),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })

    def audit_credential_variables(self) -> list[str]:
        """Find client credential references missing from or misplaced in the shared .env."""
        from dotenv import dotenv_values

        env_path = self.shared_credentials_env_path()
        if not env_path.exists():
            return [f"shared credentials file is missing: {env_path}"]
        env_values = dotenv_values(env_path)
        expected = set()
        problems = []
        registry = self._load_registry().get("name_keys", {})
        for ids in registry.values():
            for client_id in ids if isinstance(ids, list) else [ids]:
                reference = self.credential_reference(client_id)
                if reference and reference.startswith("env:"):
                    prefix = reference[4:]
                    expected.update((f"{prefix}_EMAIL", f"{prefix}_PASSWORD"))
                    problems.extend(self.credential_audit(client_id))
        for key in env_values:
            if key and (key.endswith("_EMAIL") or key.endswith("_PASSWORD")):
                if key.startswith(("SNIPER_", "OMS_")):
                    continue
                if key not in expected:
                    problems.append(f"orphan or misplaced credential variable: {key}")
        return problems

    def validate_client_ready(self, client_id: str) -> None:
        values = self.profile_values(client_id)
        if values.get("enabled", "").lower() != "true":
            raise RuntimeError(f"Client {client_id!r} is not enabled.")
        if not values.get("crm_account_id") or values["crm_account_id"].lower() == "null":
            # FIX (Sep 2026, "handle this gracefully in the right flow, not
            # editing the id manually each time"): a missing crm_account_id
            # no longer blocks the run. The canonical client ID IS the
            # default CRM account identity -- the documented default
            # (RUN_COMMANDS_AND_CREDENTIALS.md: "refresh assigns the unique
            # canonical client ID as its CRM account ID ... the default
            # local account identity") and what every configured client
            # already uses -- so assign it, persist it, and continue. A
            # real, separate CRM account ID can still be written into
            # profile/client.yaml at any time and will be respected from
            # then on; only genuinely broken things (disabled profile,
            # missing credentials) keep raising below.
            default_account_id = client_id
            self.set_crm_account_id(client_id, default_account_id)
            values["crm_account_id"] = default_account_id
            print(
                f"[profile] client {client_id}: crm_account_id was not set -- "
                f"assigned the default CRM account identity {default_account_id!r} "
                f"and saved to profile/client.yaml (the same default every "
                f"configured client uses; edit profile/client.yaml only if this "
                f"client's CRM account uses a separate ID).",
                flush=True,
            )
        reference = values.get("credential_reference", "")
        if not reference.startswith("env:"):
            raise RuntimeError(
                f"Client {client_id!r} has no valid credential_reference."
            )
        credential_problems = self.credential_audit(client_id)
        if credential_problems:
            raise RuntimeError("; ".join(credential_problems))

    def set_credential_reference(self, client_id: str, reference: str) -> None:
        paths = self.ensure_client_provisioned(client_id, credential_reference=reference)
        lines = paths.profile_path.read_text(encoding="utf-8").splitlines()
        lines = [line for line in lines if not line.startswith("credential_reference:")]
        lines.append(f"credential_reference: {reference}")
        paths.profile_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def set_crm_account_id(self, client_id: str, account_id: str) -> None:
        paths = self.ensure_client_provisioned(client_id)
        lines = paths.profile_path.read_text(encoding="utf-8").splitlines()
        lines = [line for line in lines if not line.startswith("crm_account_id:")]
        lines.append(f"crm_account_id: {account_id}")
        paths.profile_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def set_job_status(self, client_id: str, job_id: str, status: str,
                       **extra: object) -> None:
        job_manifest = self.client_paths(client_id).jobs_dir / job_id / "job.json"
        if not job_manifest.exists():
            return
        data = json.loads(job_manifest.read_text(encoding="utf-8-sig"))
        data["status"] = status
        data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        data.update(extra)
        _atomic_json_write(job_manifest, data)

    def archive_completed_job(self, client_id: str, job_id: str) -> Path | None:
        """Move a completed job out of the active queue into visible history.

        FIX (Sep 2026, "file in use" / authorization noise at completion):
        this used to be a single shutil.move() whose PermissionError made
        the caller mark the fully-succeeded job "failed". Now the move is
        retried with a short backoff and, if it still cannot land, the
        manifest is flagged archive_deferred -- see _move_job_to_archive.
        """
        return self._move_job_to_archive(client_id, job_id, "completed")

    def _move_job_to_archive(self, client_id: str, job_id: str,
                             bucket: str) -> Path | None:
        """Atomically move one live job directory into jobs/archive/<bucket>.

        Returns the destination, or None when the filesystem refused the
        move -- in which case the manifest is flagged archive_deferred so
        retry_deferred_archives() finishes the job on a later run without
        ever opening a browser. A filesystem hiccup never downgrades a
        succeeded job to "failed" and never loses a byte of it.
        """
        paths = self.client_paths(client_id)
        job_dir = paths.jobs_dir / job_id
        if not job_dir.is_dir():
            return None
        archive_dir = paths.jobs_dir / "archive" / bucket
        archive_dir.mkdir(parents=True, exist_ok=True)
        destination = archive_dir / job_id
        if destination.exists():
            raise FileExistsError(f"Job archive already exists: {destination}")
        last_exc: Exception | None = None
        for delay in (0.0, 0.25, 0.5, 1.0, 2.0):
            if delay:
                time.sleep(delay)
            try:
                if job_dir.resolve().anchor == destination.resolve().anchor:
                    # Same volume (the normal case): an atomic rename IS the
                    # whole move -- it either lands completely or not at all.
                    # Deliberately NOT shutil.move here: when a file inside
                    # the job directory is still locked, shutil.move's
                    # copytree+rmtree fallback copies everything to the
                    # archive, fails the delete, and leaves the live job
                    # gutted (its files deleted one by one until the locked
                    # one stops it) with a half-populated archive -- exactly
                    # the corrupting half-move the defer logic must avoid.
                    os.rename(str(job_dir), str(destination))
                else:
                    # Cross-volume: copy + delete, and roll the copy back on
                    # ANY failure so the next attempt starts clean.
                    shutil.copytree(job_dir, destination)
                    shutil.rmtree(job_dir)
                return destination
            except (OSError, shutil.Error) as exc:
                last_exc = exc
                if destination.exists():
                    shutil.rmtree(destination, ignore_errors=True)
        # The move failed, but the job's STATUS was already decided by the
        # caller (completed / superseded), so re-write the manifest with the
        # deferral flags and leave the status exactly as it is. Retrying the
        # archive later must never resurrect or reclassify the job.
        current = _read_json_file(job_dir / "job.json")
        current_status = (current.get("status", "imported")
                          if isinstance(current, dict) else "imported")
        self.set_job_status(client_id, job_id, current_status,
                            archive_deferred=True, archive_bucket=bucket,
                            archive_error=repr(last_exc))
        return None

    def live_jobs(self, client_id: str) -> list[tuple[Path, dict]]:
        """Live (non-archived) job directories for one client, oldest first.

        Only `job_*` directories carrying a parseable job.json count -- that
        is what keeps `jobs/status/` (a human workbook) and half-written
        import residue out of the queue.
        """
        jobs_dir = self.client_paths(client_id).jobs_dir
        found: list[tuple[Path, dict]] = []
        if not jobs_dir.exists():
            return found
        for job_dir in jobs_dir.glob(MANAGED_JOB_GLOB):
            manifest = _read_json_file(job_dir / "job.json")
            if isinstance(manifest, dict):
                found.append((job_dir, manifest))
        return sorted(found, key=lambda item: _job_order_key(item[0], item[1]))

    def supersede_other_jobs(self, client_id: str, keep_job_id: str,
                             reason: str) -> list[str]:
        """Enforce one-live-job-per-client: archive every live job except
        `keep_job_id` as "superseded", returning human-readable log lines.

        FIX (Sep 2026, "Michael had two job folders and the run picked the
        stale CSV while his fresh xlsx sat idle"): import_inbox only
        de-duplicated by content hash, so re-sending a corrected file simply
        ADDED a second live job. Both stayed discoverable, the scheduler took
        the one that sorted first, and the client's newest spreadsheet never
        ran. The newest import now wins outright:

          * the loser's status becomes the terminal "superseded", so
            discover_pending_jobs can never hand it out again;
          * its whole directory -- immutable source, state/, progress.db,
            reports -- moves to jobs/archive/superseded/, so nothing the
            client already uploaded is rewritten, and nothing is deleted;
          * a job whose lock is held by a live process is NEVER pulled out
            from under that run (it is reported as busy instead);
          * if the filesystem refuses the move, the job still becomes
            "superseded" (that alone removes it from the queue) and
            retry_deferred_archives finishes the move on a later run.
        """
        lines: list[str] = []
        for job_dir, _manifest in self.live_jobs(client_id):
            job_id = job_dir.name
            if job_id == keep_job_id:
                continue
            if _job_lock_is_held(job_dir):
                lines.append(f"job {job_id} is still running (lock held) -- "
                             f"left live; it will supersede once its run ends")
                continue
            pending = self.job_has_pending_uploads(client_id, job_id)
            self.set_job_status(
                client_id, job_id, "superseded",
                superseded_by=keep_job_id,
                superseded_reason=reason,
                superseded_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )
            moved = self._move_job_to_archive(client_id, job_id, "superseded")
            lines.append(
                f"job {job_id} superseded by {keep_job_id} "
                f"[{'live, archive deferred' if moved is None else 'archived'}]"
                + (", un-uploaded orders travelled with it to the archive"
                   if pending else ""))
        return lines

    def enforce_single_active_job(self) -> list[str]:
        """Sweep every client back to at most one live job (newest wins).

        Called before job discovery so the rule also repairs workspaces that
        accumulated several live jobs before the policy existed -- exactly
        Michael's directory, where a stale CSV and a fresh xlsx lived side by
        side.
        """
        lines: list[str] = []
        for client_id in self.known_client_ids():
            live = [job_dir.name for job_dir, manifest in self.live_jobs(client_id)
                    if manifest.get("status", "imported") != "superseded"]
            if len(live) < 2:
                continue
            lines.extend(
                f"client {client_id}: {line}"
                for line in self.supersede_other_jobs(
                    client_id, live[-1],
                    "a newer job folder is active for this client"))
        return lines

    def quarantine_orphan_job_dirs(self) -> list[str]:
        """Move `job_*` directories that have no manifest into archive/orphan.

        An import that died between mkdir and the manifest write left a folder
        that looked like a job to a human and like nothing at all to the
        queue -- permanently invisible clutter sitting in the client's
        directory forever (Michael's `job_20260916_161104_ecbfe27d`).
        """
        lines: list[str] = []
        for client_id in self.known_client_ids():
            jobs_dir = self.client_paths(client_id).jobs_dir
            if not jobs_dir.exists():
                continue
            for job_dir in sorted(jobs_dir.glob(MANAGED_JOB_GLOB)):
                if not job_dir.is_dir() or (job_dir / "job.json").exists():
                    continue
                destination = jobs_dir / "archive" / "orphan" / job_dir.name
                try:
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if destination.exists():
                        continue
                    shutil.move(str(job_dir), str(destination))
                except OSError as exc:
                    lines.append(f"could not quarantine orphan {job_dir}: {exc}")
                    continue
                lines.append(f"quarantined manifest-less job folder "
                             f"{job_dir.name} -> {destination}")
        return lines

    def prune_job_archives(self,
                           retention_days: int = ARCHIVE_RETENTION_DAYS,
                           now: Optional[float] = None) -> list[str]:
        """Move settled archived jobs into clients/_graveyard/.

        "Settled" means archived longer than `retention_days` AND holding
        nothing left to upload (no retry queue, no failed ledger), so pruning
        can never throw away work in progress. The move is reversible by
        design: the client's jobs tree gets quiet, but no source file is ever
        deleted -- the immutable copy inside a job is the record of what the
        client sent us.
        """
        if not retention_days or retention_days <= 0:
            return []
        cutoff = (now if now is not None else time.time()) - retention_days * 86400
        graveyard = self.root / GRAVEYARD_DIRNAME
        lines: list[str] = []
        for client_id in self.known_client_ids():
            jobs_dir = self.client_paths(client_id).jobs_dir
            for bucket in ("completed", "superseded"):
                archive_dir = jobs_dir / "archive" / bucket
                if not archive_dir.exists():
                    continue
                for job_dir in sorted(archive_dir.iterdir()):
                    manifest = _read_json_file(job_dir / "job.json")
                    if not isinstance(manifest, dict):
                        continue
                    stamp = _archive_timestamp(job_dir, manifest)
                    if stamp is None or stamp > cutoff:
                        continue
                    if self.job_has_pending_uploads(client_id, job_dir.name):
                        continue
                    destination = graveyard / client_id / job_dir.name
                    if destination.exists():
                        destination = destination.parent / f"{job_dir.name}.2"
                    try:
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.move(str(job_dir), str(destination))
                    except OSError as exc:
                        lines.append(f"could not prune {job_dir.name}: {exc}")
                        continue
                    lines.append(f"pruned archived job {job_dir.name} "
                                 f"-> {destination}")
        return lines

    def known_client_ids(self) -> list[str]:
        """Every client id the registry knows about, sorted."""
        registry = self._load_registry().get("name_keys", {})
        return sorted({
            client_id
            for values in registry.values()
            for client_id in (values if isinstance(values, list) else [values])
        })

    def resolve_client_id(self, client: str) -> str:
        """Canonical client id for `client` -- direct directory name or a
        unique registry name match, the same resolution workspace() uses,
        factored out for the read-only helpers below."""
        if (self.root / client).exists():
            return client
        registry = self._load_registry()
        matches = [
            ids[0] if isinstance(ids, list) else ids
            for key, ids in registry.get("name_keys", {}).items()
            if key.lower() == client.lower()
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise AmbiguousClientError(f"Ambiguous client name: {client}")
        return client

    def _candidate_job_dirs(self, client_id: str, job_id: str) -> list[Path]:
        paths = self.client_paths(client_id)
        return [paths.jobs_dir / job_id,
                *(paths.jobs_dir / "archive" / bucket / job_id
                  for bucket in ARCHIVE_BUCKETS)]

    def read_job_status(self, client_id: str, job_id: str) -> Optional[str]:
        """Status string from the job manifest, or None when the job does
        not exist (searched live first, then the completed archive)."""
        for job_dir in self._candidate_job_dirs(client_id, job_id):
            manifest = _read_json_file(job_dir / "job.json")
            if isinstance(manifest, dict):
                return manifest.get("status", "imported")
        return None

    def job_has_pending_uploads(self, client_id: str, job_id: str) -> bool:
        """True when this job's durable state still holds orders that have
        NOT been uploaded: a non-empty retry queue or a non-empty
        failed-orders ledger. Works for live and archived job directories
        alike."""
        for job_dir in self._candidate_job_dirs(client_id, job_id):
            state_dir = job_dir / "state"
            for name in ("retry_queue.json", "failed_orders.json"):
                data = _read_json_file(state_dir / name)
                if data:
                    return True
        return False

    def completed_job_summary(self, client: str, job_id: str) -> Optional[str]:
        """One human-readable line describing a completed job (live or
        archived), or None when the job is not completed. Used to log
        completed jobs without ever opening a browser."""
        try:
            client_id = self.resolve_client_id(client)
        except AmbiguousClientError:
            return None
        for job_dir in self._candidate_job_dirs(client_id, job_id):
            manifest = _read_json_file(job_dir / "job.json")
            if not isinstance(manifest, dict):
                continue
            if manifest.get("status", "imported") != "completed":
                return None
            counts = ", ".join(
                f"{label}={manifest[label]}"
                for label in ("succeeded", "failed", "pending_failed")
                if label in manifest
            )
            counts_part = f", {counts}" if counts else ""
            archived = ("archived at" if "archive" in job_dir.parts
                        else "not yet archived:")
            deferred = (" | archive deferred (will retry)"
                        if manifest.get("archive_deferred") else "")
            return (f"job {job_id} for client {client_id}: completed at "
                    f"{manifest.get('updated_at', '?')}{counts_part} -- "
                    f"{archived} {job_dir}{deferred}")
        return None

    def job_source_files(
        self, client_id: str, job_id: str, fallback: Path
    ) -> tuple:
        """Every order file this job covers, in manifest order.

        The manifest is the authority on what a job owns, so it is read first
        and each entry is resolved on disk. ``fallback`` (the workspace's
        single ``source_path``) is used for a job written before multi-source
        support, and as the safety net if the manifest is unreadable or names
        files that are no longer there -- a job must never silently shrink to
        fewer orders than it was imported with, and it must never report zero
        sources when a real file is in hand.

        Order is manifest order, i.e. oldest import first, so a job is always
        read the same way regardless of how many files were appended.
        """
        job_dir = self.client_paths(client_id).jobs_dir / job_id
        manifest = _read_json_file(job_dir / "job.json")
        if not isinstance(manifest, dict):
            return (fallback,)
        entries = manifest.get("sources")
        resolved: list[Path] = []
        seen: set[str] = set()
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                name = entry.get("filename")
                if not name:
                    continue
                candidate = job_dir / str(name)
                # Guard against a manifest that tries to escape job_dir.
                if candidate.parent != job_dir or not candidate.is_file():
                    continue
                if candidate.name in seen:
                    continue
                seen.add(candidate.name)
                resolved.append(candidate)
        if not resolved:
            return (fallback,)
        # Manifest order, oldest import first, exactly as the manifest records
        # it. `fallback` (== source_path, the file the caller resolved) is only
        # appended if the manifest somehow did not list it; it is not forced to
        # the front, because a job must be read the same way on every run no
        # matter which file the caller happened to pass. source_path is a
        # separate field for any legacy consumer that needs that one file.
        ordered = list(resolved)
        if fallback not in ordered:
            ordered.append(fallback)
        return tuple(ordered)

    def _job_paths(
        self,
        client_id: str,
        job_id: str,
        source_path: Path,
        run_id: str,
        source_paths: tuple = (),
    ) -> ClientWorkspace:
        paths = self.ensure_client_provisioned(client_id)
        job_dir = paths.jobs_dir / job_id
        run_dir = job_dir / "runs" / run_id
        return ClientWorkspace(
            root=self.root,
            client_id=client_id,
            job_id=job_id,
            job_dir=job_dir,
            source_path=source_path,
            source_paths=source_paths or (source_path,),
            auth_dir=paths.auth_dir,
            catalog_dir=paths.catalog_dir,
            progress_dir=job_dir / "state",
            client_progress_db=paths.client_progress_db,
            diagnostics_dir=run_dir / "diagnostics",
            run_dir=run_dir,
            job_lock_path=job_dir / "locks" / "job.lock",
        )

    def completed_job_summaries(self) -> list[str]:
        """One line per completed job across every registered client,
        covering BOTH the live jobs directory and the completed archive --
        the "log of all completed jobs" shown on every batch run."""
        summaries: list[str] = []
        registry = self._load_registry().get("name_keys", {})
        client_ids = {
            client_id
            for values in registry.values()
            for client_id in (values if isinstance(values, list) else [values])
        }
        for client_id in sorted(client_ids):
            paths = self.client_paths(client_id)
            for search_dir in (paths.jobs_dir, paths.jobs_dir / "archive" / "completed"):
                if not search_dir.exists():
                    continue
                for job_dir in sorted(search_dir.iterdir()):
                    manifest = _read_json_file(job_dir / "job.json")
                    if not isinstance(manifest, dict):
                        continue
                    if manifest.get("status", "imported") != "completed":
                        continue
                    counts = ", ".join(
                        f"{label}={manifest[label]}"
                        for label in ("succeeded", "failed", "pending_failed")
                        if label in manifest
                    )
                    counts_part = f", {counts}" if counts else ""
                    deferred = (" | archive deferred (will retry)"
                                if manifest.get("archive_deferred") else "")
                    summaries.append(
                        f"job {job_dir.name} (client {client_id}): completed at "
                        f"{manifest.get('updated_at', '?')}{counts_part} -- "
                        f"{job_dir}{deferred}")
        return summaries

    def retry_deferred_archives(self) -> int:
        """Best-effort re-archive of jobs whose completion move was
        previously deferred (manifest flag archive_deferred). Runs in the
        batch flow -- no browser, never raises. Returns the number of
        jobs successfully archived now."""
        archived_now = 0
        for client_id in self.known_client_ids():
            jobs_dir = self.client_paths(client_id).jobs_dir
            if not jobs_dir.exists():
                continue
            for job_dir in sorted(jobs_dir.iterdir()):
                manifest = _read_json_file(job_dir / "job.json")
                if not isinstance(manifest, dict):
                    continue
                if not manifest.get("archive_deferred"):
                    continue
                # Resume into the bucket the job was headed for. A superseded
                # job whose move was deferred must NEVER be retried into the
                # completed bucket -- that would make dead work live again.
                bucket = manifest.get("archive_bucket") or "completed"
                if bucket not in ARCHIVE_BUCKETS:
                    bucket = "completed"
                if self._move_job_to_archive(client_id, job_dir.name,
                                             bucket) is not None:
                    archived_now += 1
                    print(f"[archive] deferred job {job_dir.name} archived now.",
                          flush=True)
        return archived_now

    def prepare_run(self, workspace: ClientWorkspace) -> None:
        """Replace the generated artifacts for this job's current run.

        The caller must hold the job lock. Durable job state remains outside
        this directory, so retrying a job cannot erase upload progress.
        """
        workspace.run_dir.mkdir(parents=True, exist_ok=True)
        for child in tuple(workspace.run_dir.iterdir()):
            try:
                if child.is_dir():
                    shutil.rmtree(child)
                else:
                    child.unlink()
            except FileNotFoundError:
                continue
        self._remove_empty_run_containers(workspace.job_dir)

    def cleanup_run(self, workspace: ClientWorkspace) -> None:
        """Remove empty generated run directories without affecting reports."""
        run_dir = workspace.run_dir
        try:
            if run_dir.exists() and run_dir.is_dir() and not any(run_dir.iterdir()):
                run_dir.rmdir()
        except (FileNotFoundError, OSError):
            return
        self._remove_empty_run_containers(workspace.job_dir)

    @staticmethod
    def _remove_empty_run_containers(job_dir: Path) -> None:
        runs_dir = job_dir / "runs"
        try:
            if runs_dir.exists() and runs_dir.is_dir() and not any(runs_dir.iterdir()):
                runs_dir.rmdir()
        except (FileNotFoundError, OSError):
            return

    def import_inbox(self, inbox_file: str | Path, client_id: Optional[str] = None) -> ClientWorkspace:
        source = Path(inbox_file).resolve()
        if not source.is_file() or source.suffix.lower() not in ORDER_FILE_EXTENSIONS:
            raise ValueError(f"Unsupported inbox file: {source}")
        original_name = source.name.split("__", 1)[1] if "__" in source.name else source.name
        resolved_client_id = client_id
        if resolved_client_id and not (self.root / resolved_client_id).exists():
            resolved_client_id = self.resolve_canonical_client_id(resolved_client_id, allow_create=False)
        key = name_key_from_filename(original_name)
        if resolved_client_id is None:
            resolved_client_id = self.client_id_from_filename(original_name)
        self.validate_source_identity(original_name, resolved_client_id)
        if resolved_client_id:
            profile_key = self.profile_values(resolved_client_id).get("name_key")
            if profile_key:
                key = profile_key
        resolved_client_id = resolved_client_id or self.resolve_canonical_client_id(key)
        paths = self.ensure_client_provisioned(resolved_client_id, key)
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        live_job_dirs = [
            job_dir
            for job_dir, _manifest in self.live_jobs(resolved_client_id)
            if job_dir.parent == paths.jobs_dir
        ]
        # The live job that may still be worked on, if any. Settled history
        # (completed/failed_final/cancelled/superseded) is NOT reopened -- its
        # counts and report must stay stable -- so only a genuinely open job is
        # a candidate to receive an appended file.
        open_job = None
        for job_dir in live_job_dirs:
            manifest = _read_json_file(job_dir / "job.json")
            if not isinstance(manifest, dict):
                continue
            if manifest.get("status", "imported") in (
                "imported", "running", "partial", "failed", "interrupted",
                "manual_review",
            ):
                open_job = (job_dir, manifest)
                break
        # FIX (Sep 2026, "init shouldn't keep duplicating jobs"): re-running
        # `client_manager.py init` with the SAME workbook imported it as a
        # brand-new job folder every time, because the only identity check was
        # the timestamp+digest job_id, and a re-exported workbook has a new
        # digest. The client then owned several job folders and the upload run
        # had no principled way to choose -- which is how a stale CSV could win
        # over the current one.
        #
        # A client now has exactly ONE live job folder. A new file is APPENDED
        # to that job's manifest as an additional source, and the run reads the
        # whole set. Re-importing a file the job already owns is a NO-OP that
        # returns the same job, so `init` is genuinely idempotent; a genuinely
        # new file never creates a second folder.
        #
        # A job that is already settled is NOT reopened. Such a client gets a
        # fresh job for the new file -- the only case that still creates one.
        def _owned_source_path(job_dir: Path, manifest: dict) -> Optional[Path]:
            """The already-imported file in this job whose digest we just saw."""
            for entry in manifest.get("sources") or []:
                if not isinstance(entry, dict):
                    continue
                if entry.get("sha256") != digest:
                    continue
                candidate = job_dir / str(entry.get("filename") or "")
                if candidate.is_file():
                    return candidate
            return None

        if open_job is not None:
            open_dir, open_manifest = open_job
            already = _owned_source_path(open_dir, open_manifest)
            if already is not None:
                print(
                    f"[workspace] client {resolved_client_id}: {original_name} "
                    f"already imported in job {open_dir.name} -- no change",
                    flush=True,
                )
                return self.workspace(resolved_client_id, open_dir.name, already)
        # The digest is in the workspace but NOT in the open job: it is either
        # owned by a settled job of THIS client, or by another client entirely.
        # Both are real conflicts -- silently moving on would upload the same
        # customers twice -- so both stay hard errors.
        existing_owner = self._find_source_owner(digest)
        if existing_owner is not None:
            existing_client, existing_job = existing_owner
            if existing_client != resolved_client_id:
                raise DuplicateJobError(
                    f"Source already belongs to client {existing_client} "
                    f"as job {existing_job}; refusing cross-client reuse"
                )
        for job_dir in live_job_dirs:
            manifest = _read_json_file(job_dir / "job.json")
            if not isinstance(manifest, dict):
                continue
            if digest in _manifest_source_digests(manifest):
                raise DuplicateJobError(
                    f"Source already imported as settled job {job_dir.name}"
                )
        # FIX (Sep 2026, "init shouldn't keep duplicating jobs"): re-running
        # `client_manager.py init` with the SAME workbook re-imported it as a
        # brand-new job folder every time, because the only identity check was
        # the timestamp+digest job_id, and a re-exported workbook has a new
        # digest. The client then owned several job folders and the upload run
        # had no principled way to choose, which is how a stale CSV could win
        # over the current one.
        #
        # A client now has exactly ONE live job folder. A new file is APPENDED
        # to that job's manifest as an additional source, and the run reads the
        # whole set. Re-importing an identical file is still refused
        # (DuplicateJobError), so this is idempotent, and a genuinely new
        # file never creates a second folder.
        #
        # A job that is already finished is NOT reopened: it is settled
        # history, and its counts/report must stay stable. Such a client gets a
        # fresh job for the new file, which is the only case that still
        # creates a folder.
        open_job = None
        for job_dir in live_job_dirs:
            manifest = _read_json_file(job_dir / "job.json")
            if not isinstance(manifest, dict):
                continue
            if manifest.get("status", "imported") in (
                "imported", "running", "partial", "failed", "interrupted",
                "manual_review",
            ):
                open_job = (job_dir, manifest)
                break
        if open_job is not None:
            job_dir, manifest = open_job
            job_id = job_dir.name
            safe_name = re.sub(r"[^A-Za-z0-9]+", "_", key).strip("_").lower()
            stem = f"{safe_name}_{resolved_client_id}_{digest[:8]}"
            destination = _unique_destination(job_dir, stem, source.suffix.lower())
            shutil.move(str(source), str(destination))
            sources = [
                dict(entry) for entry in (manifest.get("sources") or [])
                if isinstance(entry, dict)
            ]
            sources.append({
                "filename": destination.name,
                "sha256": digest,
                "original_name": original_name,
                "added_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            })
            manifest["sources"] = sources
            manifest.setdefault("source_sha256", sources[0]["sha256"])
            manifest["source_filename"] = sources[0]["filename"]
            manifest["updated_at"] = time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
            )
            _atomic_json_write(job_dir / "job.json", manifest)
            print(
                f"[workspace] client {resolved_client_id}: appended "
                f"{original_name} to existing job {job_id} "
                f"({len(sources)} source(s)) -- no new job folder",
                flush=True,
            )
            return self.workspace(resolved_client_id, job_id, destination)
        job_id = f"job_{time.strftime('%Y%m%d_%H%M%S')}_{digest[:8]}"
        job_dir = paths.jobs_dir / job_id
        if job_dir.exists():
            raise DuplicateJobError(f"Job already exists: {job_id}")
        (job_dir / "locks").mkdir(parents=True)
        (job_dir / "state").mkdir()
        (job_dir / "runs").mkdir()
        safe_name = re.sub(r"[^A-Za-z0-9]+", "_", key).strip("_").lower()
        destination = job_dir / (
            f"{safe_name}_{resolved_client_id}{source.suffix.lower()}"
        )
        shutil.move(str(source), str(destination))
        (job_dir / "source.sha256").write_text(digest, encoding="utf-8")
        # The first file is recorded as a `sources` entry, exactly like every
        # file appended later. Writing the legacy single-source fields only
        # would leave the first file invisible to job_source_files(), so the
        # run would upload the second and third file while silently dropping
        # the first -- the exact data loss this rewrite exists to prevent.
        _atomic_json_write(job_dir / "job.json", {
            "schema_version": 1, "client_id": resolved_client_id, "name_key": key,
            "job_id": job_id, "source_original_name": original_name,
            "source_sha256": digest, "source_extension": source.suffix.lower(),
            "source_filename": destination.name,
            "sources": [{
                "filename": destination.name,
                "sha256": digest,
                "original_name": original_name,
                "added_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }],
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "status": "imported",
        })
        # --- one active job per client -------------------------------------
        # Retire everything older ONLY after the new job is fully
        # materialised (source moved in + manifest written). Superseding first
        # would leave the client with zero live jobs if anything in between
        # raised, and the whole point of this rule is that a client always has
        # exactly one folder holding the work we are doing right now.
        for line in self.supersede_other_jobs(
                resolved_client_id, job_id,
                f"replaced by {original_name}"):
            print(f"[workspace] client {resolved_client_id}: {line}", flush=True)
        return self.workspace(resolved_client_id, job_id, destination)

    def validate_source_identity(self, filename: str, client_id: Optional[str]) -> None:
        """Reject a file whose embedded canonical ID identifies another client."""
        stem = Path(filename).stem.lower()
        registry = self._load_registry().get("name_keys", {})
        known_ids = {
            item.lower()
            for values in registry.values()
            for item in (values if isinstance(values, list) else [values])
        }
        embedded = [item for item in known_ids if item in stem]
        if not embedded:
            return
        if client_id is None:
            if len(embedded) > 1:
                raise ValueError(
                    f"Source {filename!r} contains multiple client IDs: {embedded}"
                )
            return
        expected = client_id.lower()
        wrong = [item for item in embedded if item != expected]
        if wrong:
            raise ValueError(
                f"Source {filename!r} is marked for {wrong[0]}, "
                f"but was assigned to {client_id}"
            )

    def client_id_from_filename(self, filename: str) -> Optional[str]:
        stem = Path(filename).stem.lower()
        registry = self._load_registry().get("name_keys", {})
        known_ids = [
            item
            for values in registry.values()
            for item in (values if isinstance(values, list) else [values])
            if item.lower() in stem
        ]
        if len(known_ids) > 1:
            raise ValueError(f"Source {filename!r} contains multiple client IDs: {known_ids}")
        return known_ids[0] if known_ids else None

    def refresh_job_source_names(self) -> list[str]:
        """Repair managed job source names and report unsafe conflicts."""
        problems = []
        registry = self._load_registry().get("name_keys", {})
        client_ids = {
            item
            for values in registry.values()
            for item in (values if isinstance(values, list) else [values])
        }
        for client_id in client_ids:
            jobs_dir = self.client_paths(client_id).jobs_dir
            if not jobs_dir.exists():
                continue
            for job_dir in jobs_dir.iterdir():
                manifest_path = job_dir / "job.json"
                if not job_dir.is_dir() or not manifest_path.exists():
                    continue
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
                    key = str(manifest["name_key"])
                    sources = [
                        item for item in job_dir.iterdir()
                        if item.is_file() and item.name != "source.sha256"
                        and item.suffix.lower() in ORDER_FILE_EXTENSIONS
                    ]
                    if len(sources) != 1:
                        problems.append(
                            f"{client_id}/{job_dir.name}: expected one source file, "
                            f"found {len(sources)}"
                        )
                        continue
                    source = sources[0]
                    extension = str(
                        manifest.get("source_extension") or source.suffix
                    ).lower()
                    if source.suffix.lower() != extension:
                        problems.append(
                            f"{client_id}/{job_dir.name}: source extension mismatch "
                            f"({source.suffix.lower()} vs {extension})"
                        )
                        continue
                    safe_name = re.sub(r"[^A-Za-z0-9]+", "_", key).strip("_").lower()
                    expected = job_dir / f"{safe_name}_{client_id}{extension}"
                    if source != expected:
                        if expected.exists():
                            problems.append(
                                f"{client_id}/{job_dir.name}: cannot rename {source.name}; "
                                f"{expected.name} already exists"
                            )
                            continue
                        source.rename(expected)
                    manifest["source_filename"] = expected.name
                    _atomic_json_write(manifest_path, manifest)
                except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    problems.append(f"{client_id}/{job_dir.name}: {exc}")
        return problems

    def _find_source_owner(self, digest: str) -> Optional[tuple[str, str]]:
        """Find a source hash anywhere in managed client jobs."""
        registry = self._load_registry().get("name_keys", {})
        client_ids = {
            client_id
            for values in registry.values()
            for client_id in (values if isinstance(values, list) else [values])
        }
        for client_id in client_ids:
            jobs_dir = self.client_paths(client_id).jobs_dir
            if not jobs_dir.exists():
                continue
            for manifest_path in jobs_dir.glob("*/job.json"):
                try:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
                except (OSError, json.JSONDecodeError):
                    continue
                if manifest.get("source_sha256") == digest:
                    return client_id, str(manifest.get("job_id", manifest_path.parent.name))
        return None

    def claim_inbox_file(self, path: Path) -> Optional[Path]:
        claimed_dir = self.root / "_inbox" / ".claimed"
        claimed_dir.mkdir(parents=True, exist_ok=True)
        claimed = claimed_dir / f"{secrets.token_hex(16)}__{path.name}"
        try:
            path.rename(claimed)
            return claimed
        except FileNotFoundError:
            return None

    def recover_claimed_files(self) -> None:
        claimed_dir = self.root / "_inbox" / ".claimed"
        rejected = self.root / "_rejected"
        for path in claimed_dir.iterdir():
            if path.is_file():
                reason = rejected / f"{path.name}.explain.txt"
                reason.write_text(
                    "Source was claimed but not finalized; treated as interrupted "
                    "during import.\n",
                    encoding="utf-8",
                )
                shutil.move(str(path), str(rejected / path.name))

    def workspace(self, client: str, job_id: str, source_path: Optional[Path] = None) -> ClientWorkspace:
        client_id = client
        if not (self.root / client_id).exists():
            registry = self._load_registry()
            matches = [
                ids[0] if isinstance(ids, list) else ids
                for key, ids in registry.get("name_keys", {}).items()
                if key.lower() == client.lower()
            ]
            if len(matches) == 1:
                client_id = matches[0]
            elif len(matches) > 1:
                raise AmbiguousClientError(f"Ambiguous client name: {client}")
        client_root = self.client_paths(client_id).root
        job_dir = (client_root / "jobs" / job_id).resolve()
        if job_dir.parent.parent != client_root:
            raise ValueError("Invalid job path")
        if not job_dir.exists():
            raise FileNotFoundError(f"Unknown job: {job_id}")
        source = source_path
        if source is None:
            candidates = [
                p for p in job_dir.glob("source.*")
                if p.is_file() and p.name != "source.sha256"
            ]
            if not candidates:
                candidates = [
                    p for p in job_dir.iterdir()
                    if p.is_file() and p.name != "source.sha256"
                    and p.suffix.lower() in ORDER_FILE_EXTENSIONS
                ]
            if not candidates:
                raise FileNotFoundError(f"No immutable source in {job_dir}")
            source = candidates[0]
        if source.name == f"source{source.suffix.lower()}":
            manifest_path = job_dir / "job.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
            key = manifest.get("name_key", client_id)
            safe_name = re.sub(r"[^A-Za-z0-9]+", "_", str(key)).strip("_").lower()
            descriptive = job_dir / f"{safe_name}_{client_id}{source.suffix.lower()}"
            if not descriptive.exists():
                source.rename(descriptive)
                source = descriptive
        # One replaceable run workspace per job prevents empty timestamped
        # directories and unbounded diagnostic/report accumulation.
        run_id = "current"
        workspace = self._job_paths(
            client_id, job_id, source, run_id,
            source_paths=self.job_source_files(client_id, job_id, source),
        )
        workspace.run_dir.mkdir(parents=True, exist_ok=True)
        (workspace.job_dir / "locks").mkdir(parents=True, exist_ok=True)
        return workspace

    def discover_inbox(self) -> list[Path]:
        intake_dirs = [
            self.root / "_inbox",
            self.root.parent / "customers" / "data" / "waiting",
        ]
        discovered = []
        for intake_dir in intake_dirs:
            if not intake_dir.exists():
                continue
            discovered.extend(
                p for p in intake_dir.iterdir()
                if p.is_file() and p.suffix.lower() in ORDER_FILE_EXTENSIONS
            )
        return sorted(discovered, key=lambda p: p.stat().st_mtime, reverse=True)

    def select_latest_inbox_files(self, files: list[Path]) -> tuple[list[Path], list[Path]]:
        """Keep the newest valid source for each known client/name key."""
        selected = []
        discarded = []
        seen: set[str] = set()
        for source in sorted(files, key=lambda p: p.stat().st_mtime, reverse=True):
            try:
                client_id = self.client_id_from_filename(source.name)
            except (KeyError, ValueError):
                client_id = None
            key = client_id or name_key_from_filename(source.name)
            if key.lower() in seen:
                discarded.append(source)
            else:
                seen.add(key.lower())
                selected.append(source)
        return selected, discarded

    def discover_pending_jobs(self) -> list[ClientWorkspace]:
        """Return immutable jobs that still need an upload attempt.

        FIX (Sep 2026, "even if it's completed, it should retry the failed
        customers on every fresh run"): this used to scan only the live
        jobs directory for pending statuses, so a job that had been marked
        completed (and archived) while failed customers were still
        un-uploaded simply vanished from every future run. Now a
        completed job with a non-empty failed-orders ledger or retry
        queue -- live OR archived -- is returned as pending too.
        """
        pending_statuses = {
            "imported", "running", "partial", "failed", "interrupted",
            "manual_review",
        }
        # FIX (Sep 2026, one active job per client): sweep the housekeeping
        # first so the queue this returns can only ever hold one job per
        # client -- orphan folders that were never jobs get quarantined, and
        # any client that somehow has two live jobs keeps the newest.
        for line in self.quarantine_orphan_job_dirs():
            print(f"[workspace] {line}", flush=True)
        for line in self.enforce_single_active_job():
            print(f"[workspace] {line}", flush=True)
        pending = []
        for client_id in self.known_client_ids():
            paths = self.client_paths(client_id)
            jobs_dir = paths.jobs_dir
            if not jobs_dir.exists():
                continue
            live_pending = {
                job_dir.name
                for job_dir, manifest in self.live_jobs(client_id)
                if manifest.get("status", "imported") in pending_statuses
            }
            search_dirs = [jobs_dir, jobs_dir / "archive" / "completed"]
            for search_dir in search_dirs:
                if not search_dir.exists():
                    continue
                for job_dir in sorted(search_dir.iterdir()):
                    manifest_path = job_dir / "job.json"
                    if not job_dir.is_dir() or not manifest_path.exists():
                        continue
                    try:
                        manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
                    except (OSError, json.JSONDecodeError) as exc:
                        raise ValueError(f"Invalid job manifest: {manifest_path}") from exc
                    status = manifest.get("status", "imported")
                    if status in pending_statuses:
                        # FIX (Sep 2026, archived job with a pending status):
                        # this branch used to call workspace(client_id,
                        # job_dir.name) directly, but workspace() resolves
                        # jobs_dir/<job_id> -- the LIVE path. An archived job
                        # with status "imported" (Valentine's
                        # archive/completed/job_20260917_200743_d644f0ce was
                        # exactly this) therefore raised
                        #   FileNotFoundError: Unknown job: job_20260917_...
                        # and killed the whole batch before any client was
                        # touched. A pending status in the archive is simply
                        # unfinished work that has not been brought home, so
                        # restore it to the live jobs directory first and
                        # address it there. Same rule as the completed-with-
                        # failures branch below: never resurrect a second
                        # folder for a client that already has a live job.
                        if "archive" in job_dir.parts:
                            if live_pending:
                                print(
                                    f"[workspace] client {client_id}: archived "
                                    f"job {job_dir.name} is still {status} but "
                                    f"{sorted(live_pending)[0]} is the active "
                                    f"job -- held in the archive",
                                    flush=True)
                                continue
                            restored_dir = jobs_dir / job_dir.name
                            if restored_dir.exists():
                                # A live folder of the same name already won
                                # the race; the archived copy stays put rather
                                # than being merged blindly.
                                continue
                            shutil.move(str(job_dir), str(restored_dir))
                            job_dir = restored_dir
                            live_pending.add(job_dir.name)
                        live_pending.add(job_dir.name)
                        pending.append(self.workspace(client_id, job_dir.name))
                    elif status == "completed" and self.job_has_pending_uploads(
                            client_id, job_dir.name):
                        # FIX (Sep 2026): completed-with-failures jobs come
                        # back as pending so their failed customers get
                        # retried on this run. An ARCHIVED one is moved back
                        # into the live jobs directory first (workspace()
                        # can only address live jobs) and its status is set
                        # to "partial" so the run's completion gate decides
                        # again whether it may be re-archived.
                        if "archive" in job_dir.parts:
                            # FIX (Sep 2026, one active job per client): an
                            # archived job may only come back while the client
                            # has no live job. Otherwise restoring it would
                            # recreate the two-folders situation this whole
                            # policy exists to prevent. The leftover orders
                            # are NOT lost -- they sit in this job's retry
                            # queue and are restored on the first run after
                            # the newer job completes.
                            if live_pending:
                                print(
                                    f"[workspace] client {client_id}: archived "
                                    f"job {job_dir.name} still has un-uploaded "
                                    f"orders but {sorted(live_pending)[0]} is "
                                    f"the active job -- held in the archive",
                                    flush=True)
                                continue
                            restored_dir = jobs_dir / job_dir.name
                            if restored_dir.exists():
                                continue
                            shutil.move(str(job_dir), str(restored_dir))
                            self.set_job_status(client_id, job_dir.name,
                                                "partial", pending_failed=True)
                            job_dir = restored_dir
                            live_pending.add(job_dir.name)
                        pending.append(self.workspace(client_id, job_dir.name))
        return pending
