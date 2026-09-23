from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Optional

from apps.oms.sync.sync_models import InboundSyncSummary, RowSyncResult, TriggerSource
from apps.oms.sync.google_provider import GoogleSheetsProvider, DELETE_VALUE, ROW_KEY_COLUMN, ACTION_COLUMN
from apps.oms.shared.logger import get_logger

log = get_logger(__name__)


class InboundSyncTrigger:
    '''
    Decides WHEN an inbound sync should run. Never runs the sync
    itself — just calls the given async callback when conditions
    are met, then goes back to polling.

    Rules (from our conversation):
      - Manual: user sets the sheet's trigger cell to REQUESTED.
        We still wait a short EDIT_COOLDOWN after the last detected
        edit before actually running, so a manual click right after
        typing doesn't sync a half-finished row.
      - Auto: fires ~AUTO_SYNC_AFTER after the last edit — measured
        from the last edit, not a fixed wall-clock interval — and
        only if the sheet has actually changed since the last sync.
    '''

    POLL_INTERVAL_SECONDS = 15
    EDIT_COOLDOWN_SECONDS = 60          # let a burst of edits settle
    AUTO_SYNC_AFTER        = timedelta(minutes=30)

    def __init__(self, provider: GoogleSheetsProvider):
        self._provider        = provider
        self._last_sync_time  = datetime.now()
        self._running         = False

    async def poll_loop(self, on_trigger) -> None:
        '''
        on_trigger: async callable(TriggerSource) -> InboundSyncSummary
        '''
        self._running = True
        log.info("InboundSyncTrigger: polling started.")

        while self._running:
            try:
                await asyncio.sleep(self.POLL_INTERVAL_SECONDS)

                flag           = await self._provider.read_trigger_flag()
                last_edit_time = await self._provider.read_last_edit_time()
                now            = datetime.now()

                edits_are_settled = (
                    last_edit_time is not None
                    and (now - last_edit_time).total_seconds() >= self.EDIT_COOLDOWN_SECONDS
                )
                nothing_changed_since_last_sync = (
                    last_edit_time is not None and last_edit_time <= self._last_sync_time
                )

                if flag == "REQUESTED" and edits_are_settled:
                    await self._fire(on_trigger, TriggerSource.MANUAL)
                    await self._provider.clear_trigger_flag()
                    continue

                if flag == "REQUESTED" and not edits_are_settled:
                    # User clicked sync while still typing — wait, don't rush it.
                    log.debug("InboundSyncTrigger: manual sync requested but edits not settled yet.")
                    continue

                due_for_auto_sync = (now - self._last_sync_time) >= self.AUTO_SYNC_AFTER
                if due_for_auto_sync and edits_are_settled and not nothing_changed_since_last_sync:
                    await self._fire(on_trigger, TriggerSource.AUTO_INACTIVITY)
                    continue

            except asyncio.CancelledError:
                break
            except Exception as e:
                log.error(f"InboundSyncTrigger: poll loop error: {e}", exc_info=True)
                await asyncio.sleep(5)

        log.info("InboundSyncTrigger: polling stopped.")

    async def _fire(self, on_trigger, source: TriggerSource) -> None:
        log.info(f"InboundSyncTrigger: firing sync ({source.value})")
        await on_trigger(source)
        self._last_sync_time = datetime.now()

    def stop(self) -> None:
        self._running = False


class InboundSyncProcessor:
    '''
    Applies sheet edits to the DB, one row at a time, safely.
    This is where every rule from our conversation is enforced.
    '''

    def __init__(self, provider: GoogleSheetsProvider, order_repo):
        self._provider = provider
        self._repo     = order_repo

    async def run_once(self, trigger: TriggerSource = TriggerSource.STARTUP_CATCHUP) -> InboundSyncSummary:
        summary = InboundSyncSummary(trigger=trigger)

        # Rule: schema/header changes HALT the run. Never sync partial data.
        headers_ok, missing = await self._provider.validate_headers()
        if not headers_ok:
            summary.halted     = True
            summary.halt_reason = f"Missing/renamed columns: {missing}"
            summary.finished_at = datetime.now()
            log.error(f"InboundSyncProcessor: HALTED — {summary.halt_reason}")
            return summary

        rows = await self._provider.pull_all_rows()

        for row in rows:
            # Rule: one bad row never kills the batch.
            try:
                result = await self._process_row(row)
            except Exception as e:
                result = RowSyncResult(
                    sheet_row_number=row.get("_sheet_row_number", -1),
                    row_key=row.get(ROW_KEY_COLUMN),
                    outcome="error",
                    note=f"⚠️ error: {e}",
                )
                log.error(f"InboundSyncProcessor: row {result.sheet_row_number} failed: {e}")

            summary.results.append(result)
            await self._provider.write_row_status(result.sheet_row_number, result.note)

        summary.finished_at = datetime.now()
        log.info(
            f"InboundSyncProcessor: run complete "
            f"(synced={summary.synced}, archived={summary.archived}, "
            f"flagged={summary.flagged}, errors={summary.errored})"
        )
        return summary

    async def _process_row(self, row: dict) -> RowSyncResult:
        sheet_row_number = row["_sheet_row_number"]
        row_key           = (row.get(ROW_KEY_COLUMN) or "").strip()
        action             = (row.get(ACTION_COLUMN) or "").strip().upper()

        # Rule: never trust the visible "database_id" column. Resolve by
        # hidden row_key only. Missing/unknown key → skip and flag,
        # never guess which DB record it might be.
        record = await self._repo.get_by_row_key(row_key) if row_key else None
        if record is None:
            return RowSyncResult(sheet_row_number, row_key or None, "flagged", "⚠️ needs review — unmatched row")

        # Rule: real delete only on an explicit action, and it's a soft
        # delete (archive) — never triggered just because a row vanished.
        if action == DELETE_VALUE:
            await self._repo.mark_archived(record.order_id)
            return RowSyncResult(sheet_row_number, row_key, "archived", "🗑 archived")

        # Otherwise: apply the sheet's edits to the DB record.
        ignored_fields = await self._repo.apply_sheet_edits(record.order_id, row)

        if "scheduled_date" in ignored_fields:
            note = "⚠️ synced, but 'Scheduled' needs a valid Date + Time — status not changed"
        elif ignored_fields:
            # Row still syncs — just this one field's bad value didn't stick.
            note = f"✅ synced (ignored invalid: {', '.join(ignored_fields)})"
        else:
            note = "✅ synced"
        return RowSyncResult(sheet_row_number, row_key, "synced", note)
