# Production Setup Guide

## Overview

This guide walks through setting up the Windwhirl Sniper NG Beta for production
execution via GitHub Actions + Google Sheets + shared Turso database.

## Prerequisites

1. Turso database provisioned with URL and auth token
2. Google Cloud project with Sheets API enabled
3. Google service account with JSON key
4. Target Google Sheet with order data
5. GitHub repository with secret management access

## Step 1: Provision Turso Database

1. Create a Turso database at https://turso.tech/
2. Note the database URL (format: `libsql://<org>.turso.io/<db-name>`)
3. Generate an authentication token from the Turso dashboard
4. Set as GitHub Secrets: `TURSO_URL` and `TURSO_AUTH`

## Step 2: Google Cloud Setup

### Enable Sheets API
1. Go to Google Cloud Console
2. Create or select a project
3. Navigate to APIs & Services → Library
4. Search for "Google Sheets API" and enable it

### Create Service Account
1. APIs & Services → Credentials
2. Create Credentials → Service Account
3. Name it (e.g., `sniper-upload-sa`)
4. Grant "Service Account User" role
5. Create a JSON key → download the key file

### Share the Google Sheet
1. Open your target Google Sheet
2. Click Share → Add people
3. Enter the service account email (from JSON key: `client_email`)
4. Grant **Editor** access
5. Copy the Sheet ID from the URL: `https://docs.google.com/spreadsheets/d/<SHEET_ID>/edit`

## Step 3: Configure GitHub Secrets

Go to your GitHub repository → Settings → Secrets and variables → Actions.

### Required Secrets (sensitive)

| Secret Name | Description | Example Value |
|---|---|---|
| `SNIPER_EMAIL` | Primary CRM login email | user@example.com |
| `SNIPER_PASSWORD` | Primary CRM login password | ******** |
| `JANE_97A9624C_EMAIL` | Jane client email | jane@example.com |
| `JANE_97A9624C_PASSWORD` | Jane client password | ******** |
| `ALEX_FD856E4C_EMAIL` | Alex client email | alex@example.com |
| `ALEX_FD856E4C_PASSWORD` | Alex client password | ******** |
| `CHISOM_7254E7B4_EMAIL` | Chisom client email | chisom@example.com |
| `CHISOM_7254E7B4_PASSWORD` | Chisom client password | ******** |
| `EPHRAIM_1CC2C1F4_EMAIL` | Ephraim client email | ephraim@example.com |
| `EPHRAIM_1CC2C1F4_PASSWORD` | Ephraim client password | ******** |
| `FRANSISCA_28A701DD_EMAIL` | Fransisca client email | fransisca@example.com |
| `FRANSISCA_28A701DD_PASSWORD` | Fransisca client password | ******** |
| `MICHAEL_0C05E53B_EMAIL` | Michael client email | michael@example.com |
| `MICHAEL_0C05E53B_PASSWORD` | Michael client password | ******** |
| `PRECIOUS_A492616C_EMAIL` | Precious client email | precious@example.com |
| `PRECIOUS_A492616C_PASSWORD` | Precious client password | ******** |
| `VALENTINE_8232C8BD_EMAIL` | Valentine client email | valentine@example.com |
| `VALENTINE_8232C8BD_PASSWORD` | Valentine client password | ******** |
| `TURSO_URL` | Turso database URL | libsql://org.turso.io/db |
| `TURSO_AUTH` | Turso auth token | t0d... |
| `GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON` | Service account key JSON | {"type": "service_account", ...} |
| `GOOGLE_SHEETS_SPREADSHEET_ID` | Google Sheet ID | 1BxiMVs... |

| Variable Name | Description | Example Value |
|---|---|---|
| `GOOGLE_SHEETS_WORKSHEET_NAME` | Worksheet/tab name (optional) | Orders |

## Step 4: Prepare the Google Sheet

The Sheet must have a worksheet with the following columns in the header row:

| Column | Description |
|---|---|
| Customer Name | Customer's full name |
| Phone Number | Customer's phone (text format, preserve leading zeros) |
| Address | Customer's address |
| Product | Product name |
| Price (₦) | Product price in Naira |

**Important**: Phone numbers must be stored as text in Google Sheets to preserve leading zeros. Do not format as numbers.

## Step 5: Migrate Historical Data (Optional but Recommended)

Before the first production run, migrate existing SQLite history to Turso:

```bash
# From the apps/sniper directory
export TURSO_URL="libsql://..."
export TURSO_AUTH="..."

# Dry-run first to see what would be migrated
python migrate_to_turso.py --client client_jane_97a9624c --dry-run

# Then run the actual migration
python migrate_to_turso.py --client client_jane_97a9624c
```

Repeat for each client that has historical data.

## Step 6: Test Dry Run via GitHub Actions

1. Go to your GitHub repository → Actions
2. Select "Sniper Upload Production" workflow
3. Click "Run workflow"
4. Select:
   - Client: `client_jane_97a9624c` (or your chosen client)
   - Dry run: `true`
   - Source type: `sheets`
5. Click "Run workflow"
6. Verify the workflow completes successfully with no errors

The dry run will:
- Validate all environment variables are present
- Connect to Turso (read-only)
- Read the Google Sheet
- Normalize and validate rows
- Check existing successful keys
- Show how many orders would be eligible
- **NOT** launch a browser or submit any uploads

## Step 7: First Real Production Upload

After validating the dry run:

1. Go to GitHub Actions → "Sniper Upload Production"
2. Click "Run workflow"
3. Select:
   - Client: `client_jane_97a9624c`
   - Dry run: `false`  ← This enables real uploads
   - Source type: `sheets`
4. Click "Run workflow"
5. Monitor the run for successful completion

**Important**: Real uploads will actually submit orders to sniper.ng. Monitor carefully.

## Step 8: Verify Tracking Persists

After the first production run:
1. Run `python maintenance.py inspect` (with TURSO_URL/TURSO_AUTH set)
2. Verify succeeded_orders count matches expectations
3. Run a second production dry-run — should show fewer eligible orders (dedup working)

## Troubleshooting

| Issue | Solution |
|---|---|
| `TURSO_URL` or `TURSO_AUTH` missing | Add as GitHub Secrets |
| `GOOGLE_SHEETS_SERVICE_ACCOUNT_JSON` invalid JSON | Re-copy the full service account key |
| Service account can't read sheet | Share the sheet with the service account email |
| Phone numbers lose leading zeros | Format the Phone Number column as Plain Text in Google Sheets |
| Cross-client dedup collision | Production uses `production_order_key()` with client_id prefix — handled automatically |
