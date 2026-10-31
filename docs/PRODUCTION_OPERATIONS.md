# Production Operations Guide

## Overview

Operations reference for the Windwhirl Sniper NG Beta production system.
Covers daily usage, monitoring, maintenance, and troubleshooting.

## Daily Operations

### Running a Production Upload

1. Go to GitHub Actions → "Sniper Upload Production"
2. Click "Run workflow"
3. Configure:
   - **Client**: Select the client to process
   - **Dry run**: `true` for validation, `false` for real upload
   - **Source type**: `sheets` (Google Sheets)
4. Click "Run workflow"

**Always start with `dry_run: true`** to validate before real uploads.

### Checking Results

After a run completes:
1. Check the Actions run status (success/failed)
2. Review logs for any errors or warnings
3. If failure, check uploaded artifacts (logs/reports retained for 1 day)

## Maintenance Commands

All maintenance commands require `TURSO_URL` and `TURSO_AUTH` environment variables.

### Inspect Database

```bash
python maintenance.py inspect
```

Shows aggregate statistics: record counts by table, by client, date ranges, retry queue size, failed orders, run history.

### Retention Cleanup

```bash
# Dry-run first (recommended)
python maintenance.py retention --max-days 90 --dry-run

# Then execute
python maintenance.py retention --max-days 90
```

Removes old run_history and failed_ledger entries older than max_days.
**Never deletes** succeeded_orders or active retry_queue entries.

### Reset One Client

```bash
python maintenance.py reset-client --client client_jane_97a9624c --confirm RESET_CLIENT
```

**WARNING**: Deletes all tracking data for the client. Cleared succeeded orders can cause old orders to be processed again.

### Full Database Reset

```bash
python maintenance.py reset-all --confirm RESET_ALL_PRODUCTION_TRACKING
```

**WARNING**: Deletes ALL tracking data from ALL clients. Requires double confirmation.

## Migration Utility

### Dry-Run Migration Report

```bash
python migrate_to_turso.py --client client_jane_97a9624c --dry-run
```

Shows how many records would be migrated without making changes.

### Actual Migration

```bash
python migrate_to_turso.py --client client_jane_97a9624c
```

Migrates historical successful orders from local SQLite to shared Turso DB.
Run once per client before first production use.

## Monitoring Checklist

After each production run, verify:

- [ ] Workflow completed with "success" status
- [ ] No authentication errors in logs
- [ ] Succeeded orders count increased as expected
- [ ] No unexpected retry queue growth
- [ ] Failed orders have clear reasons
- [ ] Dedup is working (re-running doesn't resubmit completed orders)

## Emergency Procedures

### Turso Database Unavailable

Production mode is fail-closed: if Turso credentials are missing or the connection fails, the runner exits with an error. No orders are submitted.

To recover:
1. Verify `TURSO_URL` and `TURSO_AUTH` secrets are correct
2. Check Turso dashboard for database status
3. Re-run the workflow

### Google Sheets Access Failure

If the service account cannot read the sheet:
1. Verify the sheet is shared with the service account email
2. Verify `GOOGLE_SHEETS_SPREADSHEET_ID` is correct
3. Verify Sheets API is enabled in Google Cloud Console

### Duplicate Orders Detected

If the same order is submitted twice:
1. Check that `order_key` includes `client_id` prefix (production_tracker.py)
2. Verify no two clients share the same `client_id`
3. Check Turso `succeeded_orders` table for duplicate `(client_id, order_key)` rows

## Security Notes

- **Never** commit `.env` files or credentials to the repository
- **Never** print or log service account JSON, database tokens, or passwords
- **Never** upload browser session state or credential files as artifacts
- All secrets are injected via GitHub Secrets at runtime

## Support

For issues with the production system:
1. Check `docs/PRODUCTION_MIGRATION_AUDIT.md` for architecture details
2. Check `docs/PRODUCTION_SETUP.md` for setup steps
3. Review GitHub Actions run logs for error messages
4. Use `python maintenance.py inspect` to check database state
