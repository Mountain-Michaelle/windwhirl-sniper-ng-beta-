
import asyncio
import logging
import random

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from apps.core.lib.utils.whatsapp_sender import WhatsAppSender, SendResult
from apps.core.lib.utils.distraction import DistractionEngine

logger = logging.getLogger(__name__)


class Scheduler:
    """
    Orchestrates the full sending day across 6 APScheduler sessions.

    Connects all modules together:
        AppConfig   → session timing, delay settings, daily limit
        Database    → fetch pending customers, record results
        Sender      → WhatsApp Web automation (or future API)
        Builder     → personalized message per customer
        Reporter    → end-of-day summary generation

    The Scheduler never imports PlaywrightSender directly.
    It only knows about the WhatsAppSender interface.
    This makes swapping to CloudAPISender a one-line change in main.py.
    """

    def __init__(self, cfg, db, sender: WhatsAppSender, builder, reporter):
        """
        Args:
            cfg:      AppConfig instance
            db:       Database instance
            sender:   Any WhatsAppSender implementation
            builder:  MessageBuilder instance
            reporter: Reporter instance
        """
        self._cfg       = cfg
        self._db        = db
        self._sender    = sender
        self._builder   = builder
        self._reporter  = reporter
        self._scheduler = AsyncIOScheduler()
        self._log       = logging.getLogger(self.__class__.__name__)   
        self._distraction = None

        # Index of the last session — used to trigger end-of-day tasks
        self._last_session_idx = len(cfg.session_schedule) - 1

    async def run_session(self, session_idx: int, session_count: int):
        """
        Execute one sending session.
        Called by APScheduler at the scheduled time, or by run_now() directly.

        Args:
            session_idx:   Index of this session (0-based).
                           Used to detect the last session of the day.
            session_count: How many messages to send in this session.
        """
        self._log.info(
            f"\n{'=' * 50}\n"
            f"Session {session_idx + 1} of {len(self._cfg.session_schedule)} "
            f"— {session_count} messages planned\n"
            f"{'=' * 50}"
        )

        # ── Verify connection before starting ──────────────────
        # The session from --setup may have expired (phone logged out,
        # browser was restarted, etc.) Check and reconnect if needed.
        if not await self._sender.is_connected():
            self._log.warning(
                "WhatsApp session appears disconnected. "
                "Attempting to reconnect..."
            )
            try:
                await self._sender.connect()
                self._log.info("Reconnected successfully.")
            except Exception as e:
                self._log.error(
                    f"Reconnect failed: {e}\n"
                    f"Skipping session {session_idx + 1}. "
                    f"Will retry at next scheduled session."
                )
                return

        # ── Fetch next batch of customers ──────────────────────
        customers = self._db.get_pending(
            limit=session_count,
            order=self._cfg.send_order
        )

        if not customers:
            self._log.info("No pending customers for this session.")
            # Still trigger end-of-day if this is the last session
            if session_idx == self._last_session_idx:
                await self._end_of_day()
            return

        self._log.info(f"Fetched {len(customers)} customers for this session.")
        
        
         # Initialize distraction engine if not done yet
        # Uses the personal_whatsapp number for self-chat visits
        if self._distraction is None:
            self._distraction = DistractionEngine(
                cfg=self._cfg,
                page=self._sender._page,
                own_number=getattr(self._cfg, 'personal_whatsapp', '')
            )
            self._log.debug("Distraction engine initialized.")

        # ── Per-session counters ───────────────────────────────
        sent_count    = 0
        failed_count  = 0
        invalid_count = 0
        long_pause_used = False   # Only one long pause per session

        d = self._cfg.delays   # DelayConfig shorthand

        for i, customer in enumerate(customers):
            order_id = customer["order_id"]
            phone    = customer["normalized_phone"]
            name     = customer["first_name"]
            is_last  = (i == len(customers) - 1)

            # ── Deduplication check ────────────────────────────
            # Check again just before sending. Another session running
            # concurrently (rare but possible) may have already sent.
            if self._db.already_sent(order_id):
                self._log.info(f"  Skip (already sent): {name} [{order_id}]")
                continue

            # ── Build personalized message ─────────────────────
            message, template = self._builder.build(customer)

            self._log.info(
                f"  [{i + 1}/{len(customers)}] "
                f"{name} — Template {template} — +{phone}"
            )

            # ── Send (image or text) ───────────────────────────
            # Decide which send method to use based on config
            if (
                self._cfg.image_path
                and __import__("pathlib").Path(self._cfg.image_path).exists()
            ):
                result = await self._sender.send_image(
                    phone=phone,
                    image_path=self._cfg.image_path,
                    caption=message,
                    order_id=order_id
                )
            else:
                result = await self._sender.send_text(
                    phone=phone,
                    message=message,
                    order_id=order_id
                )

            # ── Update database with result ────────────────────
            if result.status == "SENT":
                self._db.mark_sent(
                    order_id,
                    message,
                    template,
                    result.screenshot_path
                )
                sent_count += 1

            elif result.status == "INVALID_NUMBER":
                self._db.mark_invalid(order_id)
                invalid_count += 1

            else:  # FAILED
                self._db.mark_failed(order_id, result.error_message)
                failed_count += 1

            # ── Apply delays ───────────────────────────────────
            # Skip delays after the last message in this session —
            # no point waiting when there's nothing next to send.
            if is_last:
                continue

            # Base delay between messages with random jitter
            base   = random.uniform(
                d.between_messages_min,
                d.between_messages_max
            )
            
             # ── Human distraction between messages ─────────────
            # Probabilistic — not every message triggers distraction.
            # The engine decides internally based on streak length.
            # Skip distraction after ALREADY_CONTACTED (no msg sent
            # so no need to simulate post-send behaviour)
            if result.status != "ALREADY_CONTACTED" and not is_last:
                await self._distraction.maybe_distract()
            
            jitter = random.uniform(d.jitter_min, d.jitter_max)
            delay  = max(30, base + jitter)   # Never below 30 seconds

            self._log.info(f"  Waiting {delay:.0f}s before next message...")
            await asyncio.sleep(delay)

            # Burst pause after every burst_size messages
            # burst_size=4: pause after messages 4, 8, 12...
            if (i + 1) % d.burst_size == 0:
                burst = random.uniform(d.after_burst_min, d.after_burst_max)
                self._log.info(
                    f"  Burst pause after {d.burst_size} messages — "
                    f"waiting {burst:.0f}s ({burst / 60:.1f} min)..."
                )
                await asyncio.sleep(burst)

            # Long pause once per session (30% chance)
            # Only fires in the middle of the session, not near the end
            if (
                d.long_pause_enabled
                and not long_pause_used
                and random.random() < 0.30
                and i < len(customers) - 3   # Not near end of session
            ):
                long_p = random.uniform(d.long_pause_min, d.long_pause_max)
                self._log.info(
                    f"  Long pause — waiting {long_p:.0f}s "
                    f"({long_p / 60:.1f} min)..."
                )
                await asyncio.sleep(long_p)
                long_pause_used = True   # Only one per session

        # ── Session summary ────────────────────────────────────
        self._log.info(
            f"Session {session_idx + 1} complete — "
            f"{sent_count} sent, "
            f"{failed_count} failed, "
            f"{invalid_count} invalid."
        )

        # ── End-of-day tasks after the last session ────────────
        if session_idx == self._last_session_idx:
            await self._end_of_day()

    async def _end_of_day(self):
            """
            Run after the final session of the day.
            Generates both reports and sends Excel via WhatsApp.
            """
            self._log.info("All sessions complete. Running end-of-day tasks...")

            # Run the full report flow (text + Excel + WhatsApp send)
            await self._reporter.run_end_of_day(self._db, self._sender)

            # Report retry-eligible customers
            retries = self._db.get_retry_eligible()
            if retries:
                self._log.info(
                    f"{len(retries)} message(s) eligible for retry. "
                    f"Run: python main.py --reset-failed"
                )
            else:
                self._log.info("No messages need retry.")

    def start(self):
        """
        Register all 6 sessions as APScheduler cron jobs and start.
        Prints the full schedule before starting.
        Blocks the process until all sessions complete or Ctrl+C.

        Called by: python main.py --run
        """
        jobs = self._cfg.session_jobs()

        # Print schedule so the user knows what's coming
        print("\n" + "=" * 50)
        print("  TODAY'S SENDING SCHEDULE")
        print("=" * 50)
        for i, job in enumerate(jobs):
            print(
                f"  Session {i + 1}:  "
                f"{job['hour']:02d}:{job['minute']:02d}  →  "
                f"{job['count']} messages"
            )
        print(f"\n  Total planned:  {self._cfg.total_daily_count()} messages")
        print("=" * 50)
        print("\n  Keep this window open and your laptop on.")
        print("  Do NOT open WhatsApp Web manually while running.")
        print("  Press Ctrl+C to stop.\n")

        # Register each session as a cron job
        for i, job in enumerate(jobs):
            self._scheduler.add_job(
                self.run_session,
                trigger="cron",
                hour=job["hour"],
                minute=job["minute"],
                args=[i, job["count"]],
                id=f"session_{i}",
                name=f"Session {i + 1} ({job['count']} msgs)",
                misfire_grace_time=300,   # Allow 5-minute late start
            )

        self._scheduler.start()

        # Run the event loop until Ctrl+C or all sessions complete
        loop = asyncio.get_event_loop()
        try:
            loop.run_forever()
        except KeyboardInterrupt:
            self._log.info("Scheduler stopped by user (Ctrl+C).")
        finally:
            self._scheduler.shutdown(wait=False)

    async def run_now(self, count: int):
        """
        Run one immediate session without waiting for APScheduler.
        Used for testing: python main.py --run --now --count 3

        Args:
            count: Number of messages to send in this immediate session.
        """
        self._log.info(
            f"Immediate session — sending {count} message(s) now..."
        )
        await self.run_session(session_idx=0, session_count=count)
        
        
        