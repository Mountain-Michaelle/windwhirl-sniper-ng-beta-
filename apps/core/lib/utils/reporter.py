
import asyncio
import logging
from datetime import datetime, date
from pathlib import Path

logger = logging.getLogger(__name__)


class Reporter:
    """
    Generates daily reports and delivers them.

    Produces two report files in reports/:
      1. send_report_YYYY-MM-DD.xlsx  — full customer list with status
      2. daily_report_YYYY-MM-DD.txt  — summary counts

    Delivery:
      Sends the Excel file to personal WhatsApp number.
      Retries twice on failure.
      If both attempts fail: file stays in reports/ only.
      No email. No other delivery method.

    Usage:
        reporter = Reporter(cfg)
        await reporter.run_end_of_day(db, sender)
    """

    def __init__(self, cfg):
        """
        Args:
            cfg: AppConfig instance
        """
        self._cfg = cfg
        self._log = logging.getLogger(self.__class__.__name__)

    async def run_end_of_day(self, db, sender):
        """
        Master end-of-day method. Called by Scheduler after last session.

        Flow:
          1. Generate text summary → reports/daily_report_YYYY-MM-DD.txt
          2. Generate Excel report → reports/send_report_YYYY-MM-DD.xlsx
          3. Send Excel to personal WhatsApp (2 attempts max)
          4. Log outcome

        Args:
            db:     Database instance
            sender: PlaywrightSender instance (already connected)
        """
        self._log.info("Running end-of-day report generation...")

        # ── Step 1: Text summary ───────────────────────────────
        try:
            summary_text = self.generate_text_report(db)
            print("\n" + summary_text)
        except Exception as e:
            self._log.error(f"Text report failed: {e}", exc_info=True)
            summary_text = None

        # ── Step 2: Excel report ───────────────────────────────
        excel_path = None
        try:
            from apps.core.lib.utils.excel_reporter import ExcelReporter
            excel_reporter = ExcelReporter(self._cfg)
            excel_path     = excel_reporter.generate(db)
            self._log.info(f"Excel report ready: {excel_path}")
        except Exception as e:
            self._log.error(f"Excel report failed: {e}", exc_info=True)

        # ── Step 3: Send Excel via WhatsApp ────────────────────
        if excel_path and self._cfg.has_personal_whatsapp():
            await self._send_via_whatsapp(sender, excel_path)
        elif excel_path:
            self._log.info(
                "personal_whatsapp not configured — "
                f"Excel report saved to {excel_path} only."
            )
        else:
            self._log.warning(
                "Excel report was not generated — nothing to send."
            )

    async def _send_via_whatsapp(self, sender, file_path: Path):
        """
        Send the Excel report to personal WhatsApp number.
        Retries once on failure. Gives up after 2 total attempts.

        The file always stays in reports/ regardless of outcome.
        WhatsApp send is best-effort — failure is logged but
        does not raise an exception.

        Args:
            sender:    PlaywrightSender instance (already connected)
            file_path: Path to the Excel file to send
        """
        phone    = self._cfg.personal_whatsapp
        caption  = (
            f"📊 Nabeau Store — Daily Send Report\n"
            f"Date: {date.today().strftime('%d %B %Y')}\n"
            f"File: {file_path.name}"
        )

        max_attempts = 2

        for attempt in range(1, max_attempts + 1):
            self._log.info(
                f"Sending report to personal WhatsApp "
                f"+{phone} (attempt {attempt}/{max_attempts})..."
            )
            try:
                result = await sender.send_file_to_number(
                    phone=phone,
                    file_path=str(file_path),
                    caption=caption,
                    order_id=f"REPORT_{date.today().strftime('%Y%m%d')}"
                )

                if result.success:
                    self._log.info(
                        f"✅ Report sent to WhatsApp +{phone} successfully."
                    )
                    return   # Success — exit retry loop

                # Send returned a result but with failure status
                self._log.warning(
                    f"Attempt {attempt} failed: {result.error_message}"
                )

            except Exception as e:
                self._log.warning(
                    f"Attempt {attempt} exception: {e}"
                )

            # Wait before retry (only if there is a next attempt)
            if attempt < max_attempts:
                self._log.info("Waiting 30s before retry...")
                await asyncio.sleep(30)

        # Both attempts failed
        self._log.warning(
            f"WhatsApp report delivery failed after {max_attempts} attempts.\n"
            f"Report is saved locally at: {file_path}\n"
            f"Open it manually from the reports/ folder."
        )

    def generate_text_report(self, db) -> str:
        """
        Generate plain text summary report and save to reports/.
        Returns the report string for printing to console.

        Args:
            db: Database instance

        Returns:
            Report as a plain text string.
        """
        summary   = db.get_daily_summary()
        today_str = date.today().strftime("%Y-%m-%d")

        lines = [
            f"Nabeau Store — WhatsApp Send Report — {today_str}",
            "=" * 52,
            "",
            "SUMMARY",
            f"  Sent:             {summary.get('SENT', 0)}",
            f"  Failed:           {summary.get('FAILED', 0)}",
            f"  Failed (final):   {summary.get('FAILED_FINAL', 0)}",
            f"  Invalid number:   {summary.get('INVALID_NUMBER', 0)}",
            f"  Pending:          {summary.get('PENDING', 0)}",
            "",
            "TEMPLATE PERFORMANCE (today)",
            f"  Template A sent:  {summary.get('template_A', 0)}",
            f"  Template B sent:  {summary.get('template_B', 0)}",
            "",
        ]

        # Failed customer details
        failed = summary.get("failed_details", [])
        if failed:
            lines.append("FAILED — run --reset-failed to retry tomorrow")
            lines.append("-" * 40)
            for item in failed:
                lines.append(
                    f"  {item['name']}  |  "
                    f"+{item['phone']}  |  "
                    f"{item['error']}"
                )
        else:
            lines.append("FAILED: None ✅")

        lines.append("")

        # Invalid number details
        invalid = summary.get("invalid_details", [])
        if invalid:
            lines.append("INVALID NUMBERS — not on WhatsApp")
            lines.append("-" * 40)
            for item in invalid:
                lines.append(f"  {item['name']}  |  {item['phone']}")
        else:
            lines.append("INVALID NUMBERS: None ✅")

        lines += [
            "",
            "=" * 52,
            f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        ]

        report_text = "\n".join(lines)

        # Save text report
        Path("reports").mkdir(exist_ok=True)
        report_path = Path("reports") / f"daily_report_{today_str}.txt"
        report_path.write_text(report_text, encoding="utf-8")
        self._log.info(f"Text report saved: {report_path}")

        return report_text

    # ── Keep this for backward compatibility with CLI --report ─
    def generate_report(self, db) -> str:
        """Alias for generate_text_report. Used by --report CLI command."""
        return self.generate_text_report(db)