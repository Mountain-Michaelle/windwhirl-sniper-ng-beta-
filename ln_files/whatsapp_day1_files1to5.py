# ==============================================================
# WHATSAPP REVIEW AUTOMATION — DAY 1 BUILD
# ==============================================================
# This file contains the first 5 foundational files.
# Build them in order. Test after each one.
#
# FILES IN THIS DOCUMENT:
#   FILE 1 → requirements.txt
#   FILE 2 → .gitignore
#   FILE 3 → src/__init__.py
#   FILE 4 → src/config.py
#   FILE 5 → src/database.py
#
# HOW TO USE:
#   Read the banner above each section.
#   Create the file at the path shown.
#   Paste the code block below it.
#   Save and move to the next file.
#
# PROJECT FOLDER STRUCTURE (create this manually first):
#
#   whatsapp_automation/       ← your root folder
#   ├── requirements.txt       ← FILE 1
#   ├── .gitignore             ← FILE 2
#   └── src/
#       ├── __init__.py        ← FILE 3
#       ├── config.py          ← FILE 4
#       └── database.py        ← FILE 5
#
# AFTER ALL 5 FILES:
#   uv venv
#   source .venv/bin/activate        (Mac/Linux)
#   .venv\Scripts\activate            (Windows)
#   uv pip install -r requirements.txt
#   python -c "from src.config import AppConfig; print(AppConfig())"
#   → Should print: AppConfig(product='sadoer', limit=50, email=not configured)
# ==============================================================


# ==============================================================
# ================================================================
#  FILE 1
#  PATH:  whatsapp_automation/requirements.txt
#  TYPE:  Plain text — NOT a Python file
# ================================================================
# PURPOSE:
#   Lists every Python package the system needs.
#   Install ALL of them with one command:
#     uv pip install -r requirements.txt
#
# WHY uv AND NOT pip:
#   uv is significantly faster than pip (seconds vs minutes).
#   The command is identical format — only the tool name changes.
#
# IMPORTANT — DO NOT ADD:
#   asyncio → it is built into Python 3.12, adding it breaks things
#   Any other standard library module (os, re, logging, etc.)
#
# PLAYWRIGHT BROWSER NOTE:
#   uv pip install installs the Playwright Python package only.
#   The actual Chromium browser is installed separately AFTER:
#     playwright install chromium
#   This is the one step that does NOT use uv.
# ==============================================================

# ── COPY EVERYTHING BELOW THIS LINE INTO: requirements.txt ────

 
# ── END OF FILE 1 ─────────────────────────────────────────────


# ==============================================================
# ================================================================
#  FILE 2
#  PATH:  whatsapp_automation/.gitignore
#  TYPE:  Plain text — NOT a Python file
# ================================================================
# PURPOSE:
#   Prevents sensitive files from being accidentally pushed to
#   GitHub or shared with anyone. This is a security file.
#
# MOST IMPORTANT LINE:
#   .sessions/ → contains your WhatsApp login cookies.
#   If someone gets this folder they can access your WhatsApp.
#   This file ensures it is NEVER tracked by Git.
#
# WHEN DOES THIS MATTER:
#   If you ever run: git init, git add ., git push
#   Without this file: your WhatsApp session, customer database,
#   and email password would all be uploaded publicly.
# ==============================================================

# ── COPY EVERYTHING BELOW THIS LINE INTO: .gitignore ──────────


# ── END OF FILE 2 ─────────────────────────────────────────────


# ==============================================================
# ================================================================
#  FILE 3
#  PATH:  whatsapp_automation/src/__init__.py
#  TYPE:  Python file
# ================================================================
# PURPOSE:
#   This file marks the src/ folder as a Python package.
#   Without it, Python cannot find modules inside src/.
#
# WHY THIS IS NEEDED:
#   When main.py does: from src.config import AppConfig
#   Python looks for a __init__.py inside src/ to confirm
#   that src/ is a package and not just a regular folder.
#   Without this file, that import fails with ModuleNotFoundError.
#
# CONTENT:
#   Intentionally empty. Its existence is the only thing that matters.
#
# TEST:
#   After creating this file, run from project root:
#     python -c "import src; print('src package OK')"
#   Should print: src package OK
# ==============================================================

# ── COPY EVERYTHING BELOW THIS LINE INTO: src/__init__.py ─────

# ── END OF FILE 3 ─────────────────────────────────────────────


# ==============================================================
# ================================================================
#  FILE 4
#  PATH:  whatsapp_automation/src/config.py
#  TYPE:  Python file
# ================================================================
# PURPOSE:
#   The single source of truth for ALL system settings.
#   Every other module imports AppConfig — none of them have
#   hardcoded values. If you need to change something, you
#   change it here and it applies everywhere automatically.
#
# HOW IT WORKS:
#   1. CONFIG dict at the top holds all raw settings
#   2. AppConfig class wraps it with types + validation
#   3. Every other module receives an AppConfig instance
#   4. If a required field is missing: clear error on startup,
#      not a cryptic crash later during sending
#
# WHAT TO EDIT:
#   Search for "EDIT ME" in the CONFIG dict below.
#   Those are the 7 fields you must fill in before running.
#   Everything else has sensible defaults.
#
# TEST AFTER SAVING:
#   python -c "from src.config import AppConfig; print(AppConfig())"
#   Should print config summary without errors.
# ==============================================================

# ── COPY EVERYTHING BELOW THIS LINE INTO: src/config.py ───────

# ── END OF FILE 4 ─────────────────────────────────────────────


# ==============================================================
# ================================================================
#  FILE 5
#  PATH:  whatsapp_automation/src/database.py
#  TYPE:  Python file
# ================================================================
# PURPOSE:
#   All database logic lives in this one file.
#   No other module reads or writes the database directly.
#   Everything goes through the Database class methods below.
#
# TWO TABLES:
#   customers  → one row per imported customer from Excel
#   send_log   → one row per send attempt (tracks status + retries)
#
# WHY SQLALCHEMY ORM:
#   Writing raw SQL everywhere is error-prone and hard to maintain.
#   ORM lets us work with Python objects (Customer, SendLog) instead.
#   Switching from SQLite to PostgreSQL = change one URL in config.py.
#   Nothing in this file needs to change.
#
# STATUS FLOW:
#   PENDING → (send attempt) → SENT (success, never resend)
#                            → FAILED (retry eligible if attempt < 2)
#                            → INVALID_NUMBER (never retry)
#   FAILED  → (retry)       → SENT or FAILED_FINAL (max retries reached)
#
# TEST AFTER SAVING:
#   python -c "
#   from src.config import AppConfig
#   from src.database import Database
#   cfg = AppConfig()
#   db = Database(cfg.database_url)
#   db.init()
#   print(db.get_stats())
#   "
#   Should print: {'total': 0, 'pending': 0, 'sent': 0, ...}
# ==============================================================

# ── COPY EVERYTHING BELOW THIS LINE INTO: src/database.py ─────


# ── END OF FILE 5 ─────────────────────────────────────────────


# ==============================================================
# DAY 1 VERIFICATION COMMANDS
# ==============================================================
# After creating all 5 files, run these from your project root.
# All should pass before moving to Day 2.
#
# 1. Activate your environment:
#      uv venv
#      source .venv/bin/activate    (Mac/Linux)
#      .venv\Scripts\activate        (Windows)
#
# 2. Install packages:
#      uv pip install -r requirements.txt
#
# 3. Test FILE 3 — src package loads:
#      python -c "import src; print('OK: src package')"
#
# 4. Test FILE 4 — config loads and validates:
#      python -c "
#      from src.config import AppConfig
#      cfg = AppConfig()
#      print(cfg)
#      print('Sessions:', cfg.session_jobs())
#      print('Total msgs:', cfg.total_daily_count())
#      "
#      Expected output:
#        AppConfig(product='sadoer', limit=50, email=not configured)
#        Sessions: [{'hour': 8, 'minute': 15, 'count': 8}, ...]
#        Total msgs: 50
#
# 5. Test FILE 5 — database creates tables:
#      python -c "
#      from src.config import AppConfig
#      from src.database import Database
#      cfg = AppConfig()
#      db  = Database(cfg.database_url)
#      db.init()
#      print(db.get_stats())
#      "
#      Expected output:
#        Database tables ready.
#        {'total': 0, 'pending': 0, 'sent': 0, 'invalid': 0, 'invalid_phones': 0}
#      Also check: a file data/automation.db was created
#
# IF ALL 5 PASS — you are ready for Day 2:
#   FILE 6 → src/data_reader.py   (reads Excel, normalizes phones)
#   FILE 7 → templates/message_a.j2
#   FILE 8 → templates/message_b.j2
#   FILE 9 → src/message_builder.py
#   FILE 10 → src/whatsapp_sender.py  (abstract interface)
# ==============================================================
