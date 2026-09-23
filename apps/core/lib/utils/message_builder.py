import logging
import random
import re
from pathlib import Path
from apps.core.lib.utils.greeting_utils import get_greeting
from apps.core.lib.utils.date_utils import format_order_date
from jinja2 import Environment, FileSystemLoader, StrictUndefined, Undefined

logger = logging.getLogger(__name__)


class MessageBuilder:
    """
    Renders personalized WhatsApp messages from Jinja2 templates.

    Loads templates once on init. Renders on every build() call.
    Tracks last template used to prevent long runs of same template.

    Usage:
        builder = MessageBuilder(cfg)

        # Build one message (random A or B):
        message, label = builder.build(customer_dict)
        # message → "Hi Titilayo! 👋 ..."
        # label   → "A" or "B" — save this to DB

        # Preview both templates for dry-run:
        both = builder.preview(customer_dict)
        # both → {"A": "...", "B": "..."}
    """

    # Folder where .j2 template files live
    # Relative to the project root (where you run python from)
    TEMPLATES_DIR = Path("templates")

    def __init__(self, cfg):
        """
        Args:
            cfg: AppConfig instance — provides discount_offer and review_link.
        """
        self._cfg  = cfg
        self._log  = logging.getLogger(self.__class__.__name__)
        self._last = None  # Last template label used ('A' or 'B')

        # ── Load Jinja2 environment ─────────────────────────────
        # FileSystemLoader: loads .j2 files from the templates/ folder
        # undefined=Undefined: missing variables render as empty string
        #   (safer than StrictUndefined which raises an error if a
        #    variable is missing — a missing {{ review_link }} should
        #    just render as nothing, not crash the whole send session)
        self._env = Environment(
            loader=FileSystemLoader(str(self.TEMPLATES_DIR)),
            undefined=Undefined,
            keep_trailing_newline=True,
        )

        # Load both templates once at startup
        # Fails early with a clear error if template files are missing
        try:
            self._template_a = self._env.get_template("message_a.j2")
            self._template_b = self._env.get_template("message_b.j2")
            self._log.info(
                f"Templates loaded from: {self.TEMPLATES_DIR.resolve()}"
            )
        except Exception as e:
            raise FileNotFoundError(
                f"Could not load message templates from {self.TEMPLATES_DIR}/\n"
                f"Make sure message_a.j2 and message_b.j2 exist there.\n"
                f"Error: {e}"
            )

        self._templates = {
            "A": self._template_a,
            "B": self._template_b,
        }

    def _render(self, label: str, customer: dict) -> str:
        """
        Render one template with customer data and config values.

        Args:
            label:    "A" or "B" — which template to render
            customer: dict with at minimum {"first_name": "Titilayo"}

        Returns:
            Rendered message string, whitespace-cleaned.
        """
        template = self._templates[label]

        rendered = template.render(
            first_name    =customer.get("first_name", "Customer"),
            day_greeting  = get_greeting(customer.get("timezone", "Africa/Lagos")),
            order_date    =format_order_date(customer.get("order_date", None)),
            discount_offer=self._cfg.discount_offer,
            review_link   =self._cfg.review_link,
        )

        # ── Clean up whitespace ────────────────────────────────
        # Jinja2 {% if %} blocks can leave extra blank lines.
        # Collapse 3+ consecutive newlines into 2 (one blank line).
        # This keeps the message looking clean in WhatsApp.
        rendered = re.sub(r"\n{3,}", "\n\n", rendered)

        return rendered.strip()

    def build(self, customer: dict) -> tuple:
        """
        Choose a template, render it, return (message, label).

        Selection logic:
          1. Random choice between A and B
          2. If same as last template → flip to the other one
             This prevents the same template being sent to
             3+ consecutive customers without variation
          3. Update self._last

        Args:
            customer: dict from db.get_pending() or DataReader output.
                      Must have "first_name" key at minimum.

        Returns:
            Tuple of (rendered_message_string, template_label)
            template_label is "A" or "B" — save this to the DB.

        Example:
            message, label = builder.build({"first_name": "Blessing"})
            # message → "Hi Blessing 🙏\n\nQuick one ..."
            # label   → "B"
        """
        # Random choice, but flip if it matches the last one used
        # choice = random.choice(["A", "B"])
        # if choice == self._last:
        #     choice = "B" if choice == "A" else "A"
        # self._last = choice
        
        choice = "A"

        message = self._render(choice, customer)

        self._log.debug(
            f"Built Template {choice} for {customer.get('first_name', '?')} "
            f"({len(message)} chars)"
        )

        return message, choice

    def preview(self, customer: dict) -> dict:
        """
        Render BOTH templates for one customer. Used by --dry-run.

        Returns both versions so the user can read them before
        committing to the full send run.

        Args:
            customer: dict with "first_name" key at minimum.

        Returns:
            {"A": "Hi Titilayo! 👋 ...", "B": "Hi Titilayo 🙏 ..."}
        """
        return {
            "A": self._render("A", customer),
            "B": self._render("B", customer),
        }