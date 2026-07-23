# Brownberries Cafe & Library Platform

Professional, single-website operations platform for:

- Cafe operations (tables, menu, orders, payments, inventory, staff)
- Book library operations (memberships, books, issue/reissue/return, payments)
- Unified homepage status + module navigation
- Excel exports for reporting

## 1) Implementation Plan

1. Foundation (done)
   - Flask app with modular blueprints (`main`, `cafe`, `library`)
   - SQLite database with structured models
   - Role-based authentication and user profiles
2. Core Operations (done)
   - Cafe table/menu/order/inventory/stats modules
   - Library member/book/loan/subscription/payment modules
3. Hardening (next)
   - Strong secrets via environment variables
   - CSRF protection and stricter validation
   - Daily DB backups + audit trail tables
4. Production Readiness (next)
   - PostgreSQL migration
   - Nginx reverse proxy + HTTPS
   - Background jobs for due-date alert automation (SMS/call integration)

## 2) Secure and Efficient Architecture

### Application Layer
- Monolith with module boundaries:
  - `app/main.py`: auth + shared homepage
  - `app/cafe.py`: cafe domain
  - `app/library.py`: library domain
- Reusable ORM entities in `app/models.py`

### Data Layer
- SQLite for local-first deployment (fast setup, low ops overhead)
- Normalized entities for orders, inventory, books, loans, plans, payments
- Ready for PostgreSQL migration later

### Security Controls (current + recommended)
- Current:
  - Password hashing (`werkzeug.security`)
  - Session authentication
  - Role-based route guards (`admin/manager/staff/barista/librarian`)
- Recommended next:
  - Move `SECRET_KEY` and DB URI to `.env`
  - CSRF middleware
  - Login lockout/rate-limit
  - Encrypted backups

### Networking for Local Wi-Fi Use
- App binds to `0.0.0.0`
- Accessible from devices on same Wi-Fi:
  - `http://<your-laptop-ip>:5050`

## 3) Features Implemented

### Cafe
- Table Management
- Menu Management (category/subcategory/item, availability toggle)
- Table Ordering (payment type + payment reference tracking)
- Inventory Management (barista/kitchen/cafe)
- Cafe Statistics + Excel export

### Library
- Membership Management
- Books Inventory (shelf no, copies, availability)
- Issue/Reissue/Return workflow
- Weekly reissue and late fee configuration
- Damage/lost fee handling
- Payments tracking
- Library statistics Excel export
- Due-tomorrow alert count on dashboard

## 4) Run Locally (localhost + Wi-Fi)

```bash
cd /Users/amnksngh/Documents/Codex/2026-04-27/i-want-to-build-a-website
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
flask --app run.py init-db
python run.py
```

Open in browser:
- Localhost: `http://127.0.0.1:5050`
- Wi-Fi devices: `http://<your-ip-address>:5050`
- Kitchen display: `http://<your-ip-address>:5050/cafe/kitchen`

Note:
- The app runs on port `5050` by default in `run.py`.
- On startup, `run.py` now prints current Local URL and Wi-Fi URL automatically.
- Keep the terminal running while using the site.

## 6) Cafe Logo

- Put your logo file at:
  - `/Users/amnksngh/Documents/Codex/2026-04-27/i-want-to-build-a-website/static/images/cafe-logo.png`
- Then refresh browser; the header will automatically show it.

## 5) Default Login

- Email: `admin@brownberries.local`
- Password: `admin123`

Change this immediately from User Management after first login.

## 7) Windows Production Auto-Start

For a reliable Windows server setup, use:

- App server: `scripts/windows/start_app.bat`
- Cloudflare Tunnel: `scripts/windows/start_tunnel.bat`
- Watchdog helper: `scripts/windows/watchdog_services.ps1`
- WSGI entrypoint: `wsgi.py`

Recommended production stack:

1. Run the Flask app with `waitress-serve` against `wsgi:app`
2. Run `cloudflared` as a Windows service
3. Configure both services with automatic restart on failure
4. Add a small watchdog scheduled task every 5 minutes as a backup

Notes:

- `start_app.bat` uses `waitress-serve --listen=127.0.0.1:5050 --threads=12 wsgi:app`
- `start_tunnel.bat` uses `cloudflared` and automatically prefers `%USERPROFILE%\.cloudflared\config.yml` when present
- `watchdog_services.ps1` checks the `BrownberriesApp` and `cloudflared` services, verifies local `/healthz`, and restarts the tunnel if the public domain health check fails while internet is available
- All scripts write logs into the repo `logs/` folder

### Windows Always-On Checklist

Use this exact setup on the Windows machine that serves production:

1. Run the app as the `BrownberriesApp` Windows service
2. Run the tunnel as the `cloudflared` Windows service
3. Set both services to `Automatic (Delayed Start)`
4. Set service recovery for both to restart on failure
5. Run `scripts/windows/watchdog_services.ps1` every 5 minutes from Task Scheduler
6. Do not keep the live site dependent on a foreground `cloudflared tunnel run ...` terminal

Quick verification commands in Administrator PowerShell:

```powershell
Get-Service BrownberriesApp,cloudflared
curl http://127.0.0.1:5050/healthz
curl https://brownberriescafe.com/healthz
Get-Content "C:\Brownberries\brownberries-cafe-operations\logs\windows-watchdog.log" -Tail 50
```

## 8) Attendance Rule Book

- Employees open **Profile → Attendance Rule Book** to read the currently published policy.
- Admins open **Staff → Attendance Rule Book** to edit the text or upload a PDF/TXT/MD/DOC/DOCX version.
- Every publish creates a new version and archives the previous one. The app seeds version 1 automatically if the table is empty.
- The rule book is stored in SQLite metadata and optional attachments are stored under `instance/uploads/rulebooks/`; both are intentionally ignored by Git.

## 9) Safe Mac-to-Windows Source Migration

The Windows machine owns production data. Push source code from the Mac only; do not push `instance/`, `instance/uploads/`, `instance/brownberries.db`, `instance/deployment_config.json`, or `static/uploads/`.

### On the Mac

```bash
cd "/Users/amnksngh/Documents/Codex/2026-04-27/Brownberries Cafe Operations"
git pull --ff-only origin main
git status --short --ignored
bash scripts/migration/publish_source.sh "Attendance rule book and leave workflow"
```

The publish script refuses to push runtime data and publishes only tracked source files.

### On the Windows server (Administrator PowerShell)

```powershell
Set-Location "C:\Brownberries\brownberries-cafe-operations"
Set-ExecutionPolicy -Scope Process Bypass
PowerShell -ExecutionPolicy Bypass -File .\scripts\windows\update_live_server.ps1
Invoke-WebRequest http://127.0.0.1:5050/healthz
Invoke-WebRequest https://brownberriescafe.com/healthz
```

`update_live_server.ps1` checks for uncommitted tracked source, stops `BrownberriesApp`, backs up `instance` and `static\uploads` into `instance_windows_backup\<timestamp>`, pulls the selected branch, installs dependencies, restarts the app, and refuses to finish unless the local health endpoint returns HTTP 200. It does not stop or replace the Cloudflare Tunnel service.

If the update fails, inspect `logs\windows-app-service.log` and restore only after checking the backup. Do not delete the Windows `instance` directory during a code deployment.
