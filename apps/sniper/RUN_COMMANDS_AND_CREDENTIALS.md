# Sniper Run Commands and Client Credentials

This document describes the commands currently available in the Sniper
automation codebase and explains how client workspaces, Excel files, progress,
browser sessions, email, and passwords are handled.

All examples below assume the project virtual environment is already active and the terminal is already inside:

```text
C:\Users\Chinedu\Documents\Automations\ww\apps\sniper
```

If the environment is not active yet, activate it and enter the Sniper
directory once:

```powershell
cd C:\Users\Chinedu\Documents\Automations\ww
.venv\Scripts\Activate.ps1
cd apps\sniper
```

After activation, use `python` directly. Do not use a system Python installation when the project `.venv` is available.

---

## Runtime storage and cleanup

Client jobs keep immutable source files and durable upload state under the
client workspace. Generated reports and diagnostics are deliberately
replaceable:

```text
clients\<client_id>\jobs\<job_id>\
    state\                  # durable progress and retry state
    runs\current\           # latest generated reports/diagnostics only
```

Each job has one `runs\current` directory rather than a new timestamped
directory for every attempt. Before a new attempt starts, the worker holds
the job lock and replaces the previous generated contents. This prevents
empty run directories and unbounded accumulation while preserving the
immutable source, job manifest, progress database, retry queue, and client
authentication/catalog state.

After an attempt finishes, empty `runs\current` and `runs` directories are
removed automatically. If a run produces reports or diagnostics, the latest
contents remain available in `runs\current` and are replaced by the next
attempt. Cleanup is limited to generated run artifacts and does not remove
upload progress or source data.

The repository also ignores generated runtime data, browser state, reports,
recordings, diagnostics, databases, customer input/output files, and client
job state. Keep source code, profiles, manifests, and documentation outside
those generated locations.

---

## 1. Main upload command

The main upload entry point is:

```powershell
python run_upload.py
```

The existing order-upload logic remains in
`apps\sniper\product_upload\add_products.py`. `run_upload.py` is the
orchestrator: it loads the source data, loads or refreshes the catalog, opens
the browser, logs in, processes orders, records success, queues transient
failures, exports reports, and closes the browser.

### Show all upload options

```powershell
python run_upload.py --help
```

---

## 2. Upload mode options

### Normal one-pass mode

```powershell
python run_upload.py --easy
```

`--easy` runs one normal upload pass. It does not continuously relaunch the
browser after an unexpected session-level failure.

Omitting both `--easy` and `--lock` also uses the normal one-pass behavior:

```powershell
python run_upload.py
```

With managed clients registered, this command processes new files in
`clients\_inbox` (or the compatible legacy drop folder
`customers\data\waiting`) and resumes pending immutable jobs already stored
under each client's `jobs` directory. If both intake folders are empty and no
pending jobs exist, it stops with an explanatory message; it does not
silently switch to the old single-client `customers\data` processing
workflow.
To intentionally run that legacy workflow, use:

```powershell
python run_upload.py --legacy
```

Existing managed jobs are read only from their own immutable source file
inside `clients\<client_id>\jobs\<job_id>`, never from the shared legacy
customers directory.

To run all pending jobs for one client, use only the client ID:

```powershell
python run_upload.py --client client_michael_0c05e53b --lock
```

The command finds that client's pending jobs from its managed workspace. You
do not need to provide the Excel path or job ID. Use `--job` only when you
need to target one specific job:

```powershell
python run_upload.py --client client_michael_0c05e53b --job <job_id> --lock
```

### Persistent retry/lock mode

```powershell
python run_upload.py --lock
```

`--lock` means persistent recovery mode. If the browser or a session-level
operation fails, the current browser session is closed and a fresh browser
session is started. Durable progress and the retry queue allow the new session
to resume without resubmitting completed orders.

Press `Ctrl+C` to stop persistent lock mode.

Important distinction:

- `--lock` is the upload engine's browser/session recovery mode.
- `job.lock` and `auth.lock` are filesystem process locks that prevent
  conflicting workers from using the same job/client resources.

### Diagnostics: OFF by default, opt in only to debug

```powershell
python run_upload.py --enable-diagnostics
```

Diagnostics are OFF unless you ask for them, because they cost real time on
every single action:

| | default (diagnostics off) | with `--enable-diagnostics` |
|---|---|---|
| Playwright tracing (a DOM snapshot + screenshot per action) | not started | recorded, rotated into bounded `.zip` trace windows |
| Per-wait timing report (`[wait-profile] ...`) | not collected | collected and printed (same as `WW_WAIT_PROFILE=1`) |
| Per-order phase timings (`[metrics] browser phases ms: ...`) | **still printed** | still printed |
| Closure evidence on an unexpected dialog close (screenshot + state `.txt`/`.json`) | **still written** | still written |

Turning diagnostics off therefore does NOT cost you your failure evidence or
your per-order `[metrics]` breakdown -- only the extra trace `.zip` and the
wait-timing report. Turn it on for a run you are investigating, not for a
normal upload.

`--no-diagnostics` is still accepted, but it is now a no-op (diagnostics are
already off), so every existing command keeps working unchanged.

`WW_WAIT_PROFILE` remains a fine-grained override: `WW_WAIT_PROFILE=1` turns
the wait timings on for one run without enabling traces, and
`WW_WAIT_PROFILE=0` forces them off even under `--enable-diagnostics`.

When tracing is on, the open trace chunk is rotated every 25 order attempts
into a two-file ring (`trace_window_0.zip` / `trace_window_1.zip` under the
run's diagnostics directory), so a long debug run cannot grow unbounded in
memory, and the window just before a crash is still on disk.

### Refresh a client's catalog

```powershell
python run_upload.py --refresh-catalog
```

With a client selected:

```powershell
python run_upload.py --client client_acme --refresh-catalog
```

This ignores the client's cached catalog and scrapes the live catalog again.
Use it when the product list or sellable options have changed.

### Validate without opening a browser

```powershell
python run_upload.py --client client_acme --source C:\orders\acme.xlsx --dry-run
```

`--dry-run` loads and validates the source file but does not start Playwright
or upload orders.

### Skip already completed orders

```powershell
python run_upload.py --skip-done
```

This is the default behavior. The flag is retained for explicitness. Orders
already present in the durable client progress database are not resubmitted.

---

## 2b. Speed, parallelism and diagnostics flags

These flags decide how much of your machine the upload uses and how much
evidence a run records. None of them change what an order contains.

| Flag | Default | What it does | When to change it |
|---|---|---|---|
| `--workers N` | `1` | Splits ONE client job across N separate browser processes. All N pull rows from one shared claim queue (whichever worker is free next gets the next row), so a row is never submitted twice. Per-order time is unchanged; total run time drops by roughly N times. Each worker is its own Chromium (about 300-500 MB RAM plus CPU). | The single biggest lever on total run time. Start at 4, then try 6-8 while watching for CRM-side throttling. |
| `--max-parallel N` | `2` | The GLOBAL ceiling on how many browsers may be open at once across all client jobs, used by the inbox/batch run. `0` means no ceiling. | Raise it whenever you raise `--workers`. See the warning below - this is the flag that silently cancels your speed-up. |
| `--auto-scale-workers` | off | Batch mode only: divides the `--max-parallel` budget among however many DIFFERENT clients have work ready right now (1 client gets all slots, 2 clients get half each). Never opens more browsers than `--max-parallel` allows. | Only useful when several clients are queued at once. If you always name one client, use `--workers` instead. |
| `--housekeeping-only` | off | Opens no browser and logs in nowhere. Only repairs the job folders: finishes deferred archive moves, quarantines job folders with no manifest, enforces one live job per client (older ones move to `jobs\archive\superseded` with their state), and prunes old archives. | When `clients` has accumulated junk, or when you want to see which job is currently live. |
| `--enable-diagnostics` | off | Records the slow debugging extras: full Playwright tracing plus wait-timing accounting. | Only for a run you are investigating. See the diagnostics section above. |

### WARNING: `--workers` alone is not enough in inbox/batch mode

In the mode where you do NOT name a client (the run that drains `_inbox` and
every pending job), any `--workers` value above `--max-parallel` is reduced to
`--max-parallel`:

```text
[batch] reducing per-client --workers 8 to --workers 2 to respect --max-parallel 2.
```

So `--workers 8` becomes 2 unless you also pass `--max-parallel 0` (no ceiling)
or a matching number. Every run now prints the number it is really using, so
this can never pass unnoticed again:

```text
[batch] effective --workers 8 per job, 1 job(s) scheduled.
[run] diagnostics=off  --workers=4  shard 1/4
[shard] sharing 283 row(s) through one claim queue across 4 worker(s) -- ...
```

If `[batch] effective --workers` shows a smaller number than the one you
passed, the cap did it.

Note the two modes behave differently: `--client X --job Y` runs use your
`--workers` exactly as given (no cap is applied), while the inbox/batch run
applies `--max-parallel`.

### Recipes

Normal fast run of one job (diagnostics are already off by default):

```powershell
python run_upload.py --client client_michael_0c05e53b --job <job_id> --lock --workers 4
```

Inbox/batch run across every pending job, with the ceiling lifted:

```powershell
python run_upload.py --lock --workers 4 --max-parallel 0
```

One job while you debug it (slower on purpose):

```powershell
python run_upload.py --client client_michael_0c05e53b --job <job_id> --lock --workers 1 --enable-diagnostics
```

Repair and report only (no browser, no uploads):

```powershell
python run_upload.py --housekeeping-only
```

### A note on `--skip-done`

`--skip-done` is on by default and currently has no off-switch, so it cannot
be disabled from the command line. That is deliberate today (an order already
in the durable progress database must never be resubmitted), which also means
passing `--skip-done` changes nothing.

---

## 3. Client and job commands

The administration entry point requires a subcommand. To see the available
subcommands, run:

```powershell
python client_manager.py --help
```

Do not use `--init`; the correct syntax is `init <name>`. The name may be
omitted to initialize all clients represented by Excel files currently in
`clients\_inbox`.

```powershell
python client_manager.py init <client-name>
```

Initialize every client in the inbox in one idempotent pass:

```powershell
python client_manager.py init
```

To re-read the shared `.env`, validate every registered client credential
variable, and refresh each client's non-secret credential fingerprints, use:

```powershell
python client_manager.py init --refresh
```

The refresh command reports errors for missing or misplaced client variables,
orphan credential variables, invalid profile references, and a missing shared
`.env`. It also repairs legacy job source names such as `source.xlsx` to the
canonical `<client_name>_<client_id>.<extension>` format and reports source
conflicts instead of guessing. It never prints email addresses or passwords.
When a registered profile has `crm_account_id: null`, refresh assigns the
unique canonical client ID as its CRM account ID. This is the default local
account identity; replace it manually if your CRM uses a separate account ID.

For each supported Excel file in `clients\_inbox`, it derives the client name
from the filename, creates the registry/profile/workspace only when that
client is not already registered, and imports the file into an immutable job
under that client's folder. The inbox file is therefore no longer present
in `_inbox` after a successful import; it is preserved as:

```text
clients\<client_id>\jobs\<job_id>\<client_name>_<client_id>.xlsx
```

The canonical source filename must contain the client ID. If an incoming
filename already contains a registered client ID, that ID is authoritative.
If it conflicts with the client inferred from the filename or an explicit
assignment, initialization rejects the file for human review rather than
letting it enter another client's job.

Running the command again is safe: already-registered clients are skipped,
and the imported source remains in its job directory. Use
`python client_manager.py list` to see the resulting client mappings and
inspect each client's `jobs` directory to find its Excel source.

Every imported file is deduplicated by its full SHA-256 hash across all client
jobs. If the same file is placed under another client name or intake location,
it is rejected instead of being processed for the wrong client. Files with
different contents remain separate jobs even when their extensions differ.

All management commands also accept an optional alternate workspace root:

```powershell
python client_manager.py --root C:\some\other\clients list
```

Normally, do not provide `--root`; the default is:

```text
apps\sniper\clients\
```

### Create/provision a client

```powershell
python client_manager.py init acme --id client_acme --crm-account-id CRM_ACME
```

This creates the client workspace and registers:

```text
name_key: acme
canonical client ID: client_acme
```

It creates the client directories and appends a labeled credential block to
the shared `apps\.env` file. The generated email and password values are
`CHANGE_ME`; replace them manually. It does not store a password in the
client profile.

The `--crm-account-id` value is required before an upload can run. If it was
omitted, edit this generated file:

```text
apps\sniper\clients\client_acme\profile\client.yaml
```

Change `crm_account_id: null` to the real CRM account ID.

The credential reference is generated automatically from the client name and
canonical ID suffix. For example, a generated profile may contain:

```yaml
credential_reference: env:ACME_1CCE89E8
```

The matching shared `.env` entries are:

```dotenv
ACME_1CCE89E8_EMAIL=CHANGE_ME
ACME_1CCE89E8_PASSWORD=CHANGE_ME
```

Each client has a local credential cache and an audit fingerprint:

```text
clients\<client_id>\auth\credentials.json
clients\<client_id>\auth\credential_fingerprint.json
```

`credentials.json` contains the usable client email and password for local
runtime fallback and is ignored by Git. It is used only when the client's
`.env` variables are unavailable. `credential_fingerprint.json` contains
SHA-256 audit fingerprints only and is not used for login. Run
`python client_manager.py init --refresh` after changing a credential; this
re-reads `.env`, validates both values, and replaces both cache files.
Normal initialization also creates the cache when valid `.env` values exist.
If both `.env` and the validated JSON cache are unavailable, readiness blocks
that client before a browser is launched.

If `--id` is omitted, the code generates a canonical ID similar to:

```text
client_acme_1cce89e8
```

The exact suffix is generated automatically and may differ.

The explicit-name form above is optional. Use it when you want to initialize
one client that is not represented by an inbox file or when you need to
provide a specific client ID.

### Check client configuration

```powershell
python client_manager.py doctor
```

Run this before uploading. It checks enabled clients for CRM account IDs,
credential references, and duplicate CRM account IDs.

### List registered clients

```powershell
python client_manager.py list
```

This reads:

```text
apps\sniper\clients\_registry.json
```

and displays each filename/name key and its canonical client ID.

### Import an order file using filename identity

```powershell
python client_manager.py import C:\orders\acme.xlsx
```

The importer:

1. Validates the file extension.
2. Derives a normalized name key from the filename.
3. Resolves or provisions the corresponding canonical client.
4. Calculates the full SHA-256 hash.
5. Checks for an already imported source with the same hash.
6. Creates an immutable job directory.
7. Copies the source into the job.
8. Writes `job.json` and `source.sha256`.
9. Removes the original inbox/source copy only after finalization.

The command prints the created job ID.

### Assign/import a file for an explicit client

```powershell
python client_manager.py assign C:\orders\acme.xlsx client_acme
```

This bypasses filename-based identity and assigns the source directly to
`client_acme`.

The source is copied into the shared inbox first, then imported into the
client's immutable job directory.

### Delete a client

```powershell
python client_manager.py delete client_acme
```

This deletes the entire client directory and removes its registry mapping.
It is destructive. Back up the client directory before using this command.

---

## 4. Run a specific client with a new Excel file

```powershell
python run_upload.py --client client_acme --source C:\orders\acme.xlsx --lock
```

The source is copied into the client's job area and is no longer read from
the original location during the upload. The upload reads the immutable job
source.

The resulting layout is similar to:

```text
apps\sniper\clients\client_acme\
â”œâ”€â”€ profile\
â”‚   â””â”€â”€ client.yaml
â”œâ”€â”€ auth\
â”‚   â”œâ”€â”€ session_state.json
â”‚   â”œâ”€â”€ metadata.json
â”‚   â””â”€â”€ auth.lock
â”œâ”€â”€ catalog\
â”‚   â””â”€â”€ catalog_cache.json
â”œâ”€â”€ state\
â”‚   â””â”€â”€ client_progress.db
â”œâ”€â”€ browser\
â””â”€â”€ jobs\
    â””â”€â”€ job_<timestamp>_<hash-prefix>\
        â”œâ”€â”€ source.xlsx
        â”œâ”€â”€ source.sha256
        â”œâ”€â”€ job.json
        â”œâ”€â”€ locks\
        â”‚   â””â”€â”€ job.lock
        â”œâ”€â”€ state\
        â”‚   â”œâ”€â”€ progress.db
        â”‚   â””â”€â”€ retry_queue.json
        â””â”€â”€ runs\
            â””â”€â”€ current\
                â”œâ”€â”€ failed_exported.csv
                â”œâ”€â”€ fuzzy_matches_for_review.csv
                â”œâ”€â”€ skipped_incomplete_rows.csv
                â””â”€â”€ diagnostics\
```

The permanent deduplication database is client-level:

```text
client_acme\state\client_progress.db
```

This allows a later job for the same client to recognize orders successfully
uploaded by an earlier job.

The retry queue is job-level:

```text
client_acme\jobs\<job_id>\state\retry_queue.json
```

This keeps transient failures belonging to one Excel job separate from every
other job.

---

## 5. Resume an existing job

```powershell
python run_upload.py --client client_acme --job job_20260913_123659_fbf6b301 --lock
```

The job source is loaded from its immutable job directory. The original Excel
file does not need to remain in its old location.

The uploader:

- Skips orders already recorded as successful.
- Retries entries still in that job's retry queue.
- Processes unfinished rows.
- Writes new run artifacts under a new run directory.
- Closes the browser for that job before returning.

Two terminals cannot safely process the same job simultaneously because the
job lock rejects the second process.

---

## 6. Process all pending inbox files

Place order files in the preferred intake folder:

```text
apps\sniper\clients\_inbox\
```

Files placed in the existing legacy waiting folder are also discovered:

```text
apps\sniper\customers\data\waiting\
```

The preferred location is `clients\_inbox`; the waiting folder is supported
so existing drop-folder workflows continue to work during migration. Both
locations are claimed and imported into the same isolated client job layout.
The scanner accepts `.xlsx`, `.xls`, and `.csv` case-insensitively; it does
not stop after the first extension or ignore later files with another
supported extension.

Then run:

```powershell
python run_upload.py --lock
```

The batch flow imports all inbox files first, then starts one isolated worker
process and Chrome browser per client/job. Each worker receives its own
workspace, credentials, progress, reports, and browser lifecycle, so client
jobs run concurrently.

If one job fails, its manifest is marked failed and the next job continues.
If a source cannot be imported, it is moved to:

```text
apps\sniper\clients\_rejected\
```

### Skip clients or inbox filename stems

```powershell
python run_upload.py --lock --skip client_acme,client_beta
```

The skip list can match a canonical client ID or a normalized inbox filename
stem. Skipped files remain untouched in `_inbox`.

### Run the batch without browser uploads

```powershell
python run_upload.py --dry-run
```

---

## 7. Multiple terminals and simultaneous clients

Different clients may be run from separate terminals:

Terminal 1:

```powershell
python run_upload.py --client client_acme --lock
```

Terminal 2:

```powershell
python run_upload.py --client client_beta --lock
```

Different clients use different job state, client progress, catalog, session
state, reports, and browser sessions.

Do not start two terminals for the same job. The `job.lock` file prevents that.
Jobs for the same client are also serialized through `auth.lock` because the
client authentication/session and catalog resources are shared.

The batch command launches separate worker processes automatically. Separate
terminals remain supported for explicitly targeted jobs, but are not required
for normal multi-client batch execution.

Before launching any browser, batch mode performs a readiness preflight. A
client without `crm_account_id`, valid credentials, or an enabled profile is
marked `manual_review` and reported once; its browser is not launched and
other ready clients continue. After correcting the profile, use the job
resume command or run the batch command again; `manual_review` jobs are
rediscovered and rechecked automatically.

---

## 8. Authentication, email, and password handling

### Important comparison with the senior-engineer attachment

The current code implements the per-client environment-variable reference
portion of the proposed design. It does **not** yet implement the attachment's
stronger staged-credentials/keyring workflow.

Not currently implemented:

- `clients\_staging\credentials.env`
- `stage-credentials`
- `import-credentials`
- Windows keyring storage
- automatic password scrubbing
- `doctor`
- automatic deletion of a fully imported staging file

Therefore, the current workflow uses one shared private `.env` file. When a
new client is provisioned, the code idempotently appends one labeled client
block to `apps\.env`; it does not create a separate `.env` per client and it
does not append the block again when the client already exists. The template
provided at [apps/.env.example](../.env.example) shows the same labeled-block
format.

### Does client creation create a `.env` file?

No.

Client creation only creates the client workspace and profile metadata. It
does not create a `.env` file, email field, password field, or secret file.

This is intentional: passwords must not be automatically written into client
profiles, job manifests, logs, reports, or source directories.

### Where does the code load `.env`?

`apps\sniper\utils\auth.py` calls:

```python
load_dotenv()
```

This loads environment variables from the normal `python-dotenv` lookup
locations, typically the current working directory and its parent lookup
path.

The client manager's shared credentials file is:

```text
C:\Users\Chinedu\Documents\Automations\ww\apps\.env
```

Client provisioning creates this file if it does not exist and appends a
single block for a new client under a locked, atomic update. Do not commit it.

### Legacy/global credential names

The fallback/global variables are:

```dotenv
SNIPER_EMAIL=default@example.com
SNIPER_PASSWORD=default-password
```

These are retained for backward compatibility with the original
single-client flow.

### Credential-reference names

Client creation derives one unique prefix from the normalized client name and
the eight-character suffix of its canonical ID. For example, client creation
may write this labeled block automatically:

```dotenv
ACME_1CCE89E8_EMAIL=CHANGE_ME
ACME_1CCE89E8_PASSWORD=CHANGE_ME
```

The values initially appear as `CHANGE_ME`; replace them manually. For a
ready-to-copy layout, start with [apps/.env.example](../.env.example).

The profile stores only:

```yaml
credential_reference: env:ACME_1CCE89E8
```

The authentication code resolves `env:ACME_1CCE89E8` to
`ACME_1CCE89E8_EMAIL` and `ACME_1CCE89E8_PASSWORD`.

### Credential lookup order

For a client upload, the code checks credentials in this order:

1. The profile's `credential_reference` if it starts with `env:`.
2. Legacy global variables:
   `SNIPER_EMAIL` and `SNIPER_PASSWORD`.

If no complete pair exists, the run raises a clear missing-credentials error.
It does not silently continue and does not print the password.

### Session reuse

After a successful login, Playwright storage state is saved per client:

```text
apps\sniper\clients\client_acme\auth\session_state.json
```

Safe metadata is written to:

```text
apps\sniper\clients\client_acme\auth\metadata.json
```

The metadata records client/session bookkeeping only. It does not contain the
email, password, or authentication token.

On the next run, the code tries the client-specific cached browser session
first. If it is expired or invalid, it performs a fresh login using the
resolved environment credentials and replaces the session cache.

`auth.lock` prevents concurrent refreshes of the same client's session.

---

## 9. Important security rules

Do not put passwords in:

- `client.yaml`
- `job.json`
- `source.sha256`
- failure reports
- diagnostics
- command-line arguments
- source Excel files
- registry files

Use environment variables or an external secret manager. Keep `.env` private
and add it to `.gitignore` if this directory is placed under version control.

The browser `session_state.json` is sensitive even though it is not the
password itself. Protect it like a credential and do not commit it.

---

## 10. What the current implementation does not do

The current implementation does automatically create/update the single shared
`apps\.env` when a new client is created. It does not import passwords into
Windows Credential Manager/keyring and does not scrub a temporary credentials
file. Those are
future hardening features from the attached senior-engineer prompt, not
features currently present in this code.

Until that feature is implemented, credential lifecycle is:

1. Operator creates the client and selects a `credential_reference`.
2. Client provisioning atomically appends one labeled email/password block to
   `apps\.env` if that client/prefix is not already present.
3. Operator replaces `CHANGE_ME` values in that block.
4. `auth.py` reads the values from the shared `.env` for the selected client.
5. Login uses the values only in memory.
6. Playwright saves client browser storage state under the client's `auth`
   directory; the password itself is not saved by this code.

## 11. Recorder command

The separate recorder entry point is:

```powershell
python run_recorder.py
```

It opens a browser and supports a manual login/session-recording workflow. It
uses its own `session_state.json` and `recordings` paths and is separate from
the multi-client upload orchestration.

To enable its debug mode:

```powershell
python run_recorder.py --debug
```

The recorder is not the normal order-upload command and should not be used to
process client Excel jobs.

---

## 12. Recommended first-time setup

### Step 1: Create a client

```powershell
python client_manager.py init acme --id client_acme --crm-account-id CRM_ACME
```

The command automatically creates the client-specific credential reference
and appends its variables to `apps\.env`. Find the generated block there,
then replace its `CHANGE_ME` values. For example:

```dotenv
ACME_1CCE89E8_EMAIL=acme@example.com
ACME_1CCE89E8_PASSWORD=replace-with-the-real-password
```

The eight-character suffix in the example is only illustrative; use the
prefix actually generated for the client.

### Step 2: Check client configuration

```powershell
python client_manager.py doctor
```

### Step 3: Validate the source without uploading

```powershell
python run_upload.py --client client_acme --source C:\orders\acme.xlsx --dry-run
```

### Step 4: Start the real upload

```powershell
python run_upload.py --client client_acme --source C:\orders\acme.xlsx --lock
```

### Step 5: List clients and inspect job folders

```powershell
python client_manager.py list
```

Inspect the generated job manifest and run artifacts under:

```text
apps\sniper\clients\client_acme\jobs\
```
