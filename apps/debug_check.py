# ==============================================================
# DEBUG FILE — Run this to find the exact error
# PATH: windwhirl/apps/debug_check.py
# ==============================================================
# Run from inside the apps/ folder:
#   cd C:\Users\Chinedu\Documents\Automations\windwhirl\apps
#   python debug_check.py
#
# This file fixes its own path first so it can find your modules,
# then checks every part of the system and prints what passes
# and what fails with the exact error message.
# ==============================================================

import sys
import traceback
from pathlib import Path

# ==============================================================
# PATH FIX — must be first
# ==============================================================
# You run this from windwhirl/apps/
# Your imports say "from apps.core..." so Python needs to find
# the apps/ folder as a package — which means the PARENT folder
# windwhirl/ must be in sys.path
#
# Path(__file__).resolve()  → windwhirl/apps/debug_check.py
# .parent                   → windwhirl/apps/
# .parent                   → windwhirl/            ← add THIS
# ==============================================================
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

print("=" * 60)
print("  WHATSAPP AUTOMATION — DEBUG CHECK")
print("=" * 60)
print(f"  Python version:  {sys.version.split()[0]}")
print(f"  Python exe:      {sys.executable}")
print(f"  Running from:    {Path.cwd()}")
print(f"  Project root:    {_PROJECT_ROOT}")
print(f"  apps/ visible:   {(_PROJECT_ROOT / 'apps').exists()}")
print()


# ==============================================================
# CHECK 1: Required packages
# ==============================================================
print("─" * 60)
print("CHECK 1: Required packages")
print("─" * 60)

packages = [
    ("playwright",  "playwright"),
    ("pandas",      "pandas"),
    ("sqlalchemy",  "sqlalchemy"),
    ("apscheduler", "apscheduler"),
    ("jinja2",      "jinja2"),
    ("openpyxl",    "openpyxl"),
    ("aiofiles",    "aiofiles"),
]

all_ok = True
for name, import_name in packages:
    try:
        mod     = __import__(import_name)
        version = getattr(mod, "__version__", "installed")
        print(f"  ✅ {name:<15} {version}")
    except ImportError as e:
        print(f"  ❌ {name:<15} NOT INSTALLED — {e}")
        all_ok = False

if not all_ok:
    print()
    print("  FIX: uv pip install -r requirements.txt")
    sys.exit(1)

print()


# ==============================================================
# CHECK 2: Folder and file structure
# ==============================================================
# Based on your actual project structure:
#
#   windwhirl/
#   └── apps/
#       ├── main.py
#       ├── debug_check.py
#       ├── __init__.py
#       ├── core/
#       │   ├── __init__.py
#       │   ├── config.py                        ← AppConfig lives here
#       │   ├── db/
#       │   │   ├── __init__.py
#       │   │   └── database.py
#       │   └── lib/
#       │       ├── __init__.py
#       │       ├── scheduler/
#       │       │   ├── __init__.py
#       │       │   └── scheduler.py
#       │       └── utils/
#       │           ├── __init__.py
#       │           ├── data_reader.py
#       │           ├── message_builder.py
#       │           ├── whatsapp_sender.py
#       │           ├── playwright_sender.py
#       │           └── reporter.py
#       ├── data/
#       │   └── customers.xlsx
#       └── templates/
#           ├── message_a.j2
#           └── message_b.j2
# ==============================================================
print("─" * 60)
print("CHECK 2: Folder and file structure")
print("─" * 60)

# Base path = windwhirl/apps/ (where this file lives)
BASE = Path(__file__).resolve().parent

required_files = [
    # __init__.py files — every folder in import path needs one
    "apps/__init__.py",
    "apps/core/__init__.py",
    "apps/core/db/__init__.py",
    "apps/core/lib/__init__.py",
    "apps/core/lib/scheduler/__init__.py",
    "apps/core/lib/utils/__init__.py",

    # Source files
    "apps/core/config.py",
    "apps/core/db/database.py",
    "apps/core/lib/utils/data_reader.py",
    "apps/core/lib/utils/message_builder.py",
    "apps/core/lib/utils/whatsapp_sender.py",
    "apps/core/lib/utils/playwright_sender.py",
    "apps/core/lib/scheduler/scheduler.py",
    "apps/core/lib/utils/reporter.py",

    # Entry point
    "apps/main.py",

    # Templates
    "apps/templates/message_a.j2",
    "apps/templates/message_b.j2",
]

structure_ok = True
for f in required_files:
    # Check relative to windwhirl/ (project root)
    p = _PROJECT_ROOT / f
    if p.exists():
        print(f"  ✅ {f}")
    else:
        print(f"  ❌ {f}  ← MISSING")
        structure_ok = False

if not structure_ok:
    print()
    print("  Some files are missing. Common fixes:")
    print("  — Missing __init__.py: create empty file at that path")
    print("    PowerShell: New-Item apps\\core\\__init__.py -ItemType File -Force")
    print("  — Missing source file: check it was saved to the right folder")

print()


# ==============================================================
# CHECK 3: Every module imports without error
# ==============================================================
print("─" * 60)
print("CHECK 3: Module imports")
print("─" * 60)

# Each tuple: (import path, class name to verify)
# These must match EXACTLY what main.py imports
modules = [
    ("apps.config",                         "AppConfig"),
    ("apps.core.db.database",                "Database"),
    ("apps.core.lib.utils.data_reader",      "DataReader"),
    ("apps.core.lib.utils.message_builder",  "MessageBuilder"),
    ("apps.core.lib.utils.whatsapp_sender",  "WhatsAppSender"),
    ("apps.core.lib.utils.playwright_sender","PlaywrightSender"),
    ("apps.core.lib.scheduler.scheduler",    "Scheduler"),
    ("apps.core.lib.utils.reporter",         "Reporter"),
]

import_ok = True
for module_path, class_name in modules:
    try:
        mod = __import__(module_path, fromlist=[class_name])
        getattr(mod, class_name)   # Confirm the class actually exists
        print(f"  ✅ {module_path}")
    except Exception as e:
        print(f"  ❌ {module_path}")
        print(f"     ERROR: {e}")
        # Print the full traceback so you can see exactly which line failed
        traceback.print_exc()
        print()
        import_ok = False

if not import_ok:
    print()
    print("  One or more imports failed.")
    print("  Read the traceback above — it shows the exact line causing the error.")
    print("  Most common causes:")
    print("  — Missing __init__.py in one of the folders")
    print("  — Syntax error inside one of the module files")
    print("  — A module imports something that doesn't exist yet")

print()


# ==============================================================
# CHECK 4: Config loads and validates
# ==============================================================
print("─" * 60)
print("CHECK 4: AppConfig")
print("─" * 60)

cfg = None
try:
    from apps.config import AppConfig
    
    cfg = AppConfig()
    print(f"  ✅ AppConfig loaded:   {cfg}")
    print(f"  ✅ Target product:     {cfg.target_product}")
    print(f"  ✅ Daily limit:        {cfg.daily_limit}")
    print(f"  ✅ Sessions:           {len(cfg.session_schedule)}")
    print(f"  ✅ Total msgs/day:     {cfg.total_daily_count()}")
    print(f"  ✅ Excel path:         {cfg.excel_path()}")
    print(f"  ✅ Excel file exists:  {cfg.excel_path().exists()}")
    print(f"  ✅ Email configured:   {cfg.has_email()}")
except Exception as e:
    print(f"  ❌ AppConfig failed: {e}")
    traceback.print_exc()
    print()
    print("  FIX: Check the CONFIG dict in apps/core/config.py")
    sys.exit(1)

print()


# ==============================================================
# CHECK 5: Database
# ==============================================================
print("─" * 60)
print("CHECK 5: Database")
print("─" * 60)

try:
    from apps.core.db.database import Database
    Path("data").mkdir(exist_ok=True)
    db = Database(cfg.database_url)
    db.init()
    stats = db.get_stats()
    print(f"  ✅ Database initialized")
    print(f"  ✅ Stats: {stats}")
except Exception as e:
    print(f"  ❌ Database failed: {e}")
    traceback.print_exc()

print()


# ==============================================================
# CHECK 6: Message templates
# ==============================================================
print("─" * 60)
print("CHECK 6: Message templates")
print("─" * 60)

try:
    from apps.core.lib.utils.message_builder import MessageBuilder
    builder    = MessageBuilder(cfg)
    sample     = {"first_name": "Titilayo"}
    msg, label = builder.build(sample)
    print(f"  ✅ Template {label} rendered ({len(msg)} chars)")
    print(f"  ✅ Opening line: {msg.splitlines()[0]}")
except Exception as e:
    print(f"  ❌ MessageBuilder failed: {e}")
    traceback.print_exc()
    print()
    print("  FIX: Check templates/message_a.j2 and templates/message_b.j2 exist")
    print("  Also check TEMPLATES_DIR in message_builder.py points to the right folder")

print()


# ==============================================================
# CHECK 7: Excel file
# ==============================================================
print("─" * 60)
print("CHECK 7: Excel file")
print("─" * 60)

excel_path = cfg.excel_path()
if not excel_path.exists():
    print(f"  ⚠️  Excel not found at: {excel_path}")
    print(f"     Drop your customers.xlsx into: {excel_path.parent}")
else:
    try:
        from apps.core.lib.utils.data_reader import DataReader
        reader    = DataReader(cfg.country_code)
        customers = reader.read_and_filter(excel_path, cfg.target_product)
        print(f"  ✅ Excel readable")
        print(f"  ✅ Matching customers: {len(customers)}")
        if customers:
            c = customers[0]
            print(f"  ✅ Sample name:  {c['first_name']}")
            print(f"  ✅ Sample phone: {c['normalized_phone']}")
            print(f"  ✅ Phone valid:  {c['phone_valid']}")
        else:
            print(f"  ⚠️  Zero customers matched '{cfg.target_product}'")
            print(f"     Check target_product in apps/core/config.py")
    except Exception as e:
        print(f"  ❌ Excel read failed: {e}")
        traceback.print_exc()

print()


# ==============================================================
# SUMMARY
# ==============================================================
print("=" * 60)
print("  DEBUG CHECK COMPLETE")
print("=" * 60)
print()
print("  If all checks show ✅ — run: python main.py --preview")
print("  If any show ❌ — paste this full output to get the fix.")
print()