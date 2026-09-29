"""Throwaway end-to-end probe for the append-instead-of-duplicate change.

Copies the real clients/ tree to a temp dir, then drives WorkspaceManager
against the copy so no live job state is touched. Read-only against reality.
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from client_workspace import WorkspaceManager  # noqa: E402

tmp = Path(tempfile.mkdtemp(prefix="ww_probe_"))
shutil.copytree(Path(__file__).resolve().parent / "clients", tmp / "clients")
m = WorkspaceManager(root=tmp)

print("== 1. discover_pending_jobs (the crash the user hit) ==")
for w in m.discover_pending_jobs():
    print(f"   {w.client_id:28} {w.job_id}  {len(w.source_paths)} src")

print("\n== 2. idempotent re-init: same file twice ==")
val = tmp / "inbox"
val.mkdir(exist_ok=True)
f = val / "michael.csv"
f.write_text("customer_name,phone_number,product,address,price\na,1,p,addr,100\n",
             encoding="utf-8")
j1 = m.import_inbox(f)
before = len(m.discover_pending_jobs())
j2 = m.import_inbox(f)
after = len(m.discover_pending_jobs())
print(f"   job1={j1.job_id}")
print(f"   job2={j2.job_id}  same={j1.job_id == j2.job_id}")
print(f"   pending jobs {before} -> {after} (must not grow)")

print("\n== 3. second DIFFERENT file appends to the SAME job ==")
f2 = val / "michael.xlsx"
f2.write_bytes(b"PK\x03\x04fake xlsx payload")
j3 = m.import_inbox(f2)
print(f"   job3={j3.job_id}  same={j1.job_id == j3.job_id}")
files = m.job_source_files(j3.client_id, j3.job_id, f2)
print(f"   sources tracked = {len(files)}")
for p in files:
    print(f"     {p.name}")

print("\n== 4. the Michael.xlsx + michael.csv real-world pair ==")
mj = next((w for w in m.discover_pending_jobs()
           if w.client_id == "client_michael_0c05e53b"), None)
if mj:
    x = val / "Michael.xlsx"
    shutil.copy2(tmp / "clients" / mj.client_id / mj.job_dir.name
                 / (mj.source_paths[0].name), x) if mj.source_paths else None
    if x.exists():
        mj2 = m.import_inbox(x)
        print(f"   job {mj.job_id} -> {mj2.job_id} same={mj.job_id == mj2.job_id}")
        print(f"   sources now: {[p.name for p in m.job_source_files(mj2.client_id, mj2.job_id, x)]}")

shutil.rmtree(tmp, ignore_errors=True)
print("\nPROBE OK")
