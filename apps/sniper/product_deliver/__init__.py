"""Shared modules for run_deliver.py (Pending -> Delivered automation).

Lives beside product_upload/ per convention:
  - store.py: SQLite-backed per-client deliver progress store
  - inbox.py: status-inbox -> client job/status file distribution
  - scan.py + duplicates.py: the read-only duplicate-upload scan
    (`--scan-duplicate`) and its per-client fingerprint ledger
"""
