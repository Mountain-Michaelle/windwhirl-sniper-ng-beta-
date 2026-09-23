from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from apps.oms.sync.google_provider import GoogleSheetsProvider, ROW_KEY_COLUMN
from apps.oms.shared.logger import get_logger

log = get_logger(__name__)


@dataclass
class ReconciliationReport:
    checked_at:          datetime = field(default_factory=datetime.now)
    missing_from_sheet:  list[str] = field(default_factory=list)  # active in DB, gone from sheet
    duplicate_in_sheet:  list[str] = field(default_factory=list)  # same row_key appears twice+


class ReconciliationService:
    '''
    Run this periodically (e.g. once a day, or right after every
    inbound sync) — NOT on every single sync, since it reads the
    entire sheet and full active DB set.
    '''

    def __init__(self, provider: GoogleSheetsProvider, order_repo):
        self._provider = provider
        self._repo     = order_repo

    async def run(self) -> ReconciliationReport:
        report = ReconciliationReport()

        db_keys = set(await self._repo.list_active_row_keys())

        sheet_rows = await self._provider.pull_all_rows()
        sheet_keys = [r.get(ROW_KEY_COLUMN) for r in sheet_rows if r.get(ROW_KEY_COLUMN)]

        seen = set()
        for k in sheet_keys:
            if k in seen:
                report.duplicate_in_sheet.append(k)
            seen.add(k)

        report.missing_from_sheet = sorted(db_keys - seen)

        if report.missing_from_sheet:
            log.warning(
                f"ReconciliationService: {len(report.missing_from_sheet)} active "
                f"record(s) have no matching row in the sheet — possible accidental "
                f"row deletion. Not auto-actioned; review required."
            )
        if report.duplicate_in_sheet:
            log.warning(
                f"ReconciliationService: {len(report.duplicate_in_sheet)} row_key(s) "
                f"appear more than once in the sheet — possible copy-paste duplication."
            )

        return report
