# Production Migration Audit Report

## Repository
- **Root**: `C:\Users\Chinedu\Documents\Automation\windwhirl-sniper-ng-beta-`
- **Entry point**: `apps/sniper/run_upload.py`
- **Python version**: 3.12.3 (via `.venv`)
- **Git branch**: main

---

## 1. Verified Current Architecture

### Execution Pipeline (`run_upload.py`)

```
main() -> _run_upload() -> login_with_session_cache() -> _load_catalog()
                              -> load_orders() -> planning.build_execution_plan()
                              -> per-order: is_done() ? skip : process_order()
                              -> success: mark_done() | failure: record_failure()
                              -> _drain_retry_queue() -> _drain_failed_ledger()
                              -> browser shutdown (finally block)
```

### CLI Arguments (verified present)
| Argument | Default | Purpose |
|---|---|---|
| `--client` | None | Canonical client ID |
| `--source` | None | Source file path (Excel) or spreadsheet ID (Sheets) |
| `--job` | None | Resume existing job |
| `--dry-run` | false | Validate without browser/uploads |
| `--lock` | false | Persistent retry mode |
| `--easy` | false | One-pass mode |
| `--workers` | 1 | Workers per client |
| `--max-parallel` | 2 | Global browser slots |
| `--production` | false | **NEW**: Production mode (Turso + Sheets) |
| `--source-type` | excel | **NEW**: excel \| sheets |

### Dry-Run Safety (verified)
- Guard at `run_upload.py` line ~2187: `if dry_run: print(...); return {}`
- Prevents: browser login, order submission, DB writes, retry/ledger modifications, external requests
- Returns `{}` early — idempotent, safe to call any number of times

---

## 2. Tracking Implementations (Verified)

### ProgressTracker (`product_upload/progress_tracker.py`)
| Property | Value |
|---|---|
| Library | `sqlite3` (stdlib) |
| Default path | `customers/progress/processed_orders.db` |
| Per-client path | `clients/<client_id>/state/client_progress.db` |
| Pragmas | `journal_mode=WAL`, `synchronous=FULL` |
| Table | `succeeded_orders(order_key TEXT PRIMARY KEY, ...)` |
| `is_done(order)` | Computes `order_key(order)`, queries `WHERE order_key=?` |
| `mark_done(order)` | `INSERT OR IGNORE`, commits, read-back verification |
| Timeout | 30 seconds |

### RetryCache (`product_upload/retry_cache.py`)
| Property | Value |
|---|---|
| Format | JSON, atomic `os.replace` save |
| Max attempts | `WW_MAX_RETRY_ATTEMPTS` env var (default: 5) |
| `record_failure()` | Increments attempts, saves to disk |
| `should_give_up()` | `entry.attempts >= MAX_RETRY_ATTEMPTS` |

### FailedLedger (`product_upload/failed_ledger.py`)
| Property | Value |
|---|---|
| Format | JSON, atomic save |
| `record_failure()` | Increments attempts, updates `last_attempt_at` |
| Same-error-skip | Same reason → permanent skip |

### Order Key Algorithm (`progress_tracker.py` lines 44-75)
```python
parts = [customer_name.lower(), phone, product.lower(), price]
raw = "|".join(parts)
return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
```
- **Does NOT include client_id** — collision risk across clients
- Fields: `customer_name`, `phone_number` (fallback `phone`), `product`, `price`
- 16-char hex truncation — deterministic but not unique across clients

---

## 3. Existing Local Behavior That Must Remain Unchanged

- Local SQLite database files at `clients/<client_id>/state/*.db`
- JSON tracking files (`retry_queue.json`, `failed_orders.json`)
- Excel source files via pandas `read_excel()`
- `.env` loading via `python-dotenv` (`auth.py` calls `load_dotenv()`)
- Playwright browser automation with local session state
- Client profiles under `clients/*/profile/client.yaml`
- All existing CLI arguments and behavior
- `.gitignore` exclusions (`.env`, `*.db`, `*.xlsx`, `clients/*/state/`, etc.)

---

## 4. Confirmed Client IDs and Credential Prefixes

| Client ID | name_key | credential_reference | crm_account_id | enabled |
|---|---|---|---|---|
| client_jane_97a9624c | jane | env:JANE_97A9624C | client_jane_97a9624c | true |
| client_alex_fd856e4c | alex | env:ALEX_FD856E4C | client_alex_fd856e4c | true |
| client_chisom_7254e7b4 | chisom | env:CHISOM_7254E7B4 | client_chisom_7254e7b4 | true |
| client_ephraim_1cc2c1f4 | ephraim | env:EPHRAIM_1CC2C1F4 | client_ephraim_1cc2c1f4 | true |
| client_fransisca_28a701dd | fransisca | env:FRANSISCA_28A701DD | client_fransisca_28a701dd | true |
| client_michael_0c05e53b | michael | env:MICHAEL_0C05E53B | client_michael_0c05e53b | true |
| client_precious_a492616c | precious | env:PRECIOUS_A492616C | client_precious_a492616c | true |
| client_valentine_8232c8bd | valentine | env:VALENTINE_8232C8BD | client_valentine_8232c8bd | true |

- Registry (`clients/_registry.json`): Only `jane` → `client_jane_97a9624c` registered
- No duplicate or ambiguous IDs

---

## 5. Existing SQLite Database Locations

| Client | Path Pattern |
|---|---|
| All clients | `clients/<client_id>/state/client_progress.db` |
| Legacy | `customers/progress/processed_orders.db` |
| Legacy JSON | `customers/progress/processed_orders.json` |

All `*.db`, `*.db-*` files are gitignored.

---

## 6. Retry/Failure Storage

| Store | Format | Path | Scope |
|---|---|---|---|
| RetryCache | JSON | `clients/<client_id>/state/retry_queue.json` | Per-client |
| FailedLedger | JSON | `clients/<client_id>/state/failed_orders.json` | Per-client |

Both are gitignored and NOT persistent between GitHub Actions runner instances.

---

## 7. Order-Key Algorithm

```
SHA1(client_id|customer_name|phone|product|price)[:16]  # NEW: client-scoped
SHA1(customer_name|phone|product|price)[:16]            # EXISTING: not client-scoped
```

**Risk**: Existing `order_key()` does NOT include client_id. Same logical order for different clients produces the same key → cross-client dedup collision in shared Turso DB.

**Fix**: Production uses `production_order_key(client_id, order)` which prefixes client_id before hashing.

---

## 8. Google Sheets Integration Point

**No existing integration.** New adapter created at `apps/sniper/sheets_adapter.py`.

Expected order fields (from `run_upload.py` `COLUMN_MAP`):
| Sheet Column | Internal Field | Required |
|---|---|---|
| Customer Name | customer_name | Yes |
| Phone Number | phone_number | Yes |
| Address | address | Yes |
| Product | product | Yes |
| Price (₦) | price | Yes |

Phone numbers must be preserved as text (leading zeros, no float conversion).

---

## 9. Turso Driver

**No existing references.** Verified `libsql` is the correct Python client for Turso.

- Package: `libsql==0.5.0`
- Connection: `libsql.connect(url=TURSO_URL, auth_token=TURSO_AUTH)`
- Supports: parameterized SQL, transactions, `INSERT OR IGNORE`, `ON CONFLICT`
- Windows runner compatible

---

## 10. Identified Risks

| Risk | Severity | Mitigation |
|---|---|---|
| `order_key` not client-scoped | High | Production uses `production_order_key()` with client_id prefix |
| No existing Turso/Sheets integration | Medium | New adapter + tracker modules |
| `.env` gitignored | Medium | GitHub Secrets injected as env vars |
| Local Excel unavailable on runner | High | Production uses Google Sheets adapter |
| Session state not shareable | Medium | Fresh login each production run |
| JSON tracking not persistent on runner | High | Production uses Turso DB for retry/ledger |
| Windows runner required | Low | `windows-latest` specified in workflow |

---

## 11. Files Added

| File | Purpose |
|---|---|
| `apps/sniper/production_tracker.py` | Turso-backed client-isolated tracker |
| `apps/sniper/sheets_adapter.py` | Google Sheets order loader |
| `apps/sniper/migrate_to_turso.py` | SQLite → Turso migration utility |
| `apps/sniper/maintenance.py` | DB maintenance/reset utility |
| `.github/workflows/sniper-upload.yml` | GitHub Actions workflow |
| `docs/PRODUCTION_MIGRATION_AUDIT.md` | This file |
| `docs/PRODUCTION_SETUP.md` | Setup guide |
| `docs/PRODUCTION_OPERATIONS.md` | Operations guide |

---

## 12. Files Modified

| File | Change | Reason |
|---|---|---|
| `apps/sniper/run_upload.py` | Added `--production`, `--source-type` args; production mode branching in `_run_upload()` | Enable production mode selection |
| `apps/sniper/run_upload.py` | `main()` accepts `production`, `source_type` parameters | Pass production config through |
| `apps/sniper/run_upload.py` | `is_done()` uses `production_order_key()` in production | Client-scoped dedup keys |
| `apps/sniper/run_upload.py` | `mark_done()` passes `(key, order)` in production | Client-scoped success recording |
| `apps/sniper/run_upload.py` | `job_progress.mark_done()` skipped in production | No local job_progress DB in production |
| `requirements.txt` | Added `libsql`, `gspread`, `google-auth` | Production dependencies |

---

## 13. Test and Migration Plan

### Pre-Implementation (read-only)
1. Verify `run_upload.py --dry-run` works locally for each client
2. Verify `client_manager.py doctor` passes for all clients
3. Provision Turso database; note `TURSO_URL` and `TURSO_AUTH`

### Migration Steps (execute before first production run)
1. Set `TURSO_URL` and `TURSO_AUTH` environment variables
2. For each client: `python migrate_to_turso.py --client <client_id> --dry-run`
3. For each client: `python migrate_to_turso.py --client <client_id>`
4. Verify counts match between local SQLite and Turso

### Production Execution
1. Configure GitHub Secrets (see `docs/PRODUCTION_SETUP.md`)
2. Run workflow_dispatch with `dry_run=true` → validates without uploads
3. Run workflow_dispatch with `dry_run=false` → first real upload
4. Monitor tracking persists across separate executions
