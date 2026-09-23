
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


# ==============================================================
# SEND RESULT — Standard return type from every send operation
# ==============================================================
# Using a dataclass ensures every sender implementation returns
# the same structure. The Scheduler reads result.status to decide
# which DB update to make — it never checks which sender was used.
# ==============================================================

@dataclass
class SendResult:
    """
    The outcome of one send attempt.
    Returned by every method in every WhatsAppSender implementation.

    Fields:
        success:         True if the message was delivered.
        status:          One of: 'SENT' | 'FAILED' | 'INVALID_NUMBER'
                         Matches the SendStatus enum values in database.py
                         so the Scheduler can call db.mark_sent() etc.
        error_message:   What went wrong. Empty string on success.
        screenshot_path: Path to proof screenshot. Empty string if none taken.
        timestamp:       When this result was determined.

    Usage by Scheduler:
        result = await sender.send_text(phone, message, order_id)
        if result.status == "SENT":
            db.mark_sent(order_id, message, template, result.screenshot_path)
        elif result.status == "INVALID_NUMBER":
            db.mark_invalid(order_id)
        else:
            db.mark_failed(order_id, result.error_message)
    """
    success:         bool
    status:          str             # 'SENT' | 'FAILED' | 'INVALID_NUMBER'
    error_message:   str = ""        # Empty on success
    screenshot_path: str = ""        # Empty if no screenshot taken
    timestamp:       datetime = field(default_factory=datetime.now)


# ==============================================================
# WHATSAPP SENDER — Abstract interface
# ==============================================================
# Defines the contract that every sender implementation must fulfill.
# The ABC (Abstract Base Class) pattern in Python means:
#   - Any class that extends WhatsAppSender MUST implement all
#     @abstractmethod methods or Python will raise TypeError
#   - This catches missing implementations at startup, not mid-send
# ==============================================================

class WhatsAppSender(ABC):
    """
    Abstract base class for all WhatsApp message senders.

    Current implementation:
        PlaywrightSender (Day 3) — controls Chrome browser via Playwright
        to automate WhatsApp Web. All 8 stealth layers live there.

    Future implementations:
        CloudAPISender — calls the official WhatsApp Business Cloud API.
            To migrate: write CloudAPISender(WhatsAppSender), implement
            all abstract methods below, change one import in main.py.
            Every other file stays identical.

        MockSender — returns fake SendResult objects for testing.
            Useful for running the full scheduling logic without
            needing a real WhatsApp connection or customer data.

    Usage by Scheduler (same regardless of which implementation):
        sender = PlaywrightSender(cfg)       # or CloudAPISender(cfg)
        await sender.connect()
        result = await sender.send_text(phone, message, order_id)
        await sender.disconnect()
    """

    @abstractmethod
    async def connect(self) -> bool:
        """
        Establish connection to WhatsApp.

        For PlaywrightSender:
            Opens Chromium browser, loads saved session or shows QR code.
            Returns True once the WhatsApp chat list is visible.

        For CloudAPISender (future):
            Validates API credentials, confirms access token is active.
            Returns True if API responds with 200 OK.

        Returns:
            True if connected and ready to send.
            False or raises ConnectionError if connection failed.
        """
        pass

    @abstractmethod
    async def disconnect(self) -> None:
        """
        Close the connection cleanly.

        For PlaywrightSender:
            Closes the browser context. Does NOT delete .sessions/ folder
            — the saved login must persist for the next run.

        For CloudAPISender (future):
            Revokes or releases any session-scoped resources.

        Called automatically by main.py on Ctrl+C or after --run completes.
        Always called in a finally block so it runs even on crash.
        """
        pass

    @abstractmethod
    async def send_text(
        self,
        phone:    str,
        message:  str,
        order_id: str
    ) -> SendResult:
        """
        Send a plain text message to one WhatsApp number.

        Args:
            phone:    Normalized E.164 phone (no +), e.g. "2348038365784"
            message:  The full message string to send (rendered from template)
            order_id: Customer's order ID — used for screenshot filename
                      and for correlating logs to DB records

        Returns:
            SendResult with status one of:
                "SENT"           — message confirmed delivered
                "FAILED"         — error occurred, eligible for retry
                "INVALID_NUMBER" — phone not registered on WhatsApp, never retry

        Note:
            Must never raise an exception that crashes the caller.
            All errors should be caught internally and returned
            as SendResult(success=False, status="FAILED", error_message=...).
        """
        pass

    @abstractmethod
    async def send_image(
        self,
        phone:      str,
        image_path: str,
        caption:    str,
        order_id:   str
    ) -> SendResult:
        """
        Send an image file with a text caption.

        Args:
            phone:      Normalized E.164 phone (no +)
            image_path: Local file path to the image, e.g. "data/product.jpg"
            caption:    Text to display with the image (rendered from template)
            order_id:   For screenshot naming and log correlation

        Returns:
            Same SendResult as send_text().

        Note:
            Only called when cfg.image_path is not None and the file exists.
            The Scheduler checks this before deciding which method to call.
        """
        pass

    @abstractmethod
    async def is_connected(self) -> bool:
        """
        Check whether the current session is still alive.

        For PlaywrightSender:
            Queries the page for the WhatsApp chat list element.
            Returns False if the session expired (user logged out,
            browser crashed, etc.).

        For CloudAPISender (future):
            Makes a lightweight API call to verify token is still valid.

        Called by the Scheduler at the start of each session.
        If False: Scheduler attempts to reconnect before proceeding.

        Returns:
            True  → session is alive, safe to send
            False → session needs reconnection (call connect() again)
        """
        pass



    async def send_file_to_number(
            self,
            phone:     str,
            file_path: str,
            caption:   str,
            order_id:  str
        ) -> "SendResult":
            """
            Send a file (Excel report) to a WhatsApp number as a document.
            Used by Reporter to deliver the daily Excel report.

            Sends as Document (not Image) so the .xlsx file is received
            as a downloadable file, not converted to an image preview.

            Args:
                phone:     13-digit normalized phone e.g. "2348XXXXXXXXX"
                file_path: Full local path to the file e.g. "reports/send_report_2025-06-28.xlsx"
                caption:   Short description text sent with the file
                order_id:  For logging (use "REPORT_YYYYMMDD" format)

            Returns:
                SendResult with status SENT | FAILED | INVALID_NUMBER
            """
            import random
            import asyncio
            from pathlib import Path

            self._log.info(
                f"→ Sending file to +{phone} [{order_id}]\n"
                f"  File: {file_path}"
            )

            # Verify the file actually exists before attempting send
            if not Path(file_path).exists():
                self._log.error(f"File not found: {file_path}")
                return SendResult(
                    success=False,
                    status="FAILED",
                    error_message=f"File not found: {file_path}"
                )

            try:
                await self._rotate_tab()

                # Navigate to the chat — same clean URL as text sends
                # (STEALTH 4: no ?text= parameter)
                await self._page.goto(
                    f"https://web.whatsapp.com/send?phone={phone}",
                    wait_until="domcontentloaded",
                    timeout=50_000
                )

                # Wait for chat to load
                try:
                    await self._page.wait_for_selector(
                        self.SEL["msg_input"],
                        timeout=100_000
                    )
                except Exception:
                    if await self._check_invalid_number():
                        return SendResult(
                            success=False,
                            status="INVALID_NUMBER",
                            error_message="Phone not registered on WhatsApp"
                        )
                    raise

                if await self._check_invalid_number():
                    return SendResult(
                        success=False,
                        status="INVALID_NUMBER",
                        error_message="Phone not registered on WhatsApp"
                    )

                # STEALTH 2: Pre-action pause
                await asyncio.sleep(random.uniform(2, 4))

                # Click the attachment (paperclip) button
                await self._page.click(self.SEL["attach_btn"])
                await asyncio.sleep(random.uniform(0.8, 1.5))

                # ── Select "Document" attachment type ───────────────
                # WhatsApp Web shows options: Photos, Camera, Document, etc.
                # We need Document to send .xlsx as a downloadable file.
                # If we use the image input it will try to render xlsx as image.
                document_input_selector = 'input[accept*="*/*"], input[type="file"]'

                try:
                    # Try clicking the Document option in the attachment menu
                    doc_option = await self._page.query_selector(
                        'li[data-testid="mi-attach-document"], '
                        'span[data-icon="attach-document"]'
                    )
                    if doc_option:
                        await doc_option.click()
                        await asyncio.sleep(0.5)
                except Exception:
                    pass  # Fall through to direct file input

                # ── Upload the file via file chooser ────────────────
                try:
                    async with self._page.expect_file_chooser(
                        timeout=120_000
                    ) as fc_info:
                        # Try clicking any visible file input
                        file_inputs = await self._page.query_selector_all(
                            'input[type="file"]'
                        )
                        if file_inputs:
                            await file_inputs[-1].click()
                        else:
                            # Last resort: press Enter on the attachment menu
                            await self._page.keyboard.press("Enter")

                    file_chooser = await fc_info.value
                    await file_chooser.set_files(file_path)
                    self._log.debug(f"  File uploaded to chooser: {file_path}")

                except Exception as e:
                    self._log.error(f"  File chooser failed: {e}")
                    return SendResult(
                        success=False,
                        status="FAILED",
                        error_message=f"File upload failed: {e}"
                    )

                # Wait for file preview to appear in WhatsApp
                await asyncio.sleep(2.0)

                # ── Type the caption ─────────────────────────────────
                # Caption input for documents is different from image caption
                caption_selectors = [
                    'div[aria-label="Add a caption"]',
                    'div[aria-label="Type a message"]',
                    'div[data-tab="10"]',
                ]

                caption_typed = False
                for sel in caption_selectors:
                    try:
                        caption_el = await self._page.query_selector(sel)
                        if caption_el:
                            await caption_el.click()
                            await asyncio.sleep(0.5)
                            # Type caption character by character (STEALTH 1)
                            await self._type_human(sel, caption)
                            caption_typed = True
                            break
                    except Exception:
                        continue

                if not caption_typed:
                    self._log.warning("  Caption input not found — sending without caption")

                await asyncio.sleep(0.5)

                # Send the file
                await self._page.keyboard.press("Enter")
                self._log.debug("  File sent. Waiting for confirmation...")

                # Wait for delivery tick
                try:
                    await self._page.wait_for_selector(
                        self.SEL["sent_tick"],
                        timeout=30_000   # Files take longer than text
                    )
                    self._log.info(f"  ✅ File delivered to +{phone}")
                except Exception:
                    self._log.warning(
                        f"  ⚠ Tick timeout for file send to +{phone} — "
                        "likely sent but unconfirmed"
                    )

                # Screenshot as proof
                screenshot_path = await self._take_screenshot(order_id)
                self._msgs_on_tab += 1

                return SendResult(
                    success=True,
                    status="SENT",
                    screenshot_path=screenshot_path
                )

            except Exception as e:
                self._log.error(
                    f"  ✗ File send failed for +{phone}: {e}",
                    exc_info=True
                )
                return SendResult(
                    success=False,
                    status="FAILED",
                    error_message=str(e)
                )
                
                
            