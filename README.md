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

## 7) Windows Auto-Start

If you host the app on a Windows machine, use these launcher scripts:

- App server: `scripts/windows/start_app.bat`
- Cloudflare Tunnel: `scripts/windows/start_tunnel.bat`

Recommended setup:

1. Create a Task Scheduler task for the app server:
   - Trigger: `At startup`
   - Action: Start `scripts/windows/start_app.bat`
   - User: your Windows server account
2. Create a second Task Scheduler task for the tunnel:
   - Trigger: `At startup`
   - Delay: `30 seconds`
   - Action: Start `scripts/windows/start_tunnel.bat`
   - User: the same Windows server account

Both scripts write logs into the repo `logs/` folder.
