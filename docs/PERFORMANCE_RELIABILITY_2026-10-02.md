# Performance and reliability release — 2 October 2026

## Findings and corrections

- Windows Waitress had 12 workers while each Engine.IO long-polling browser
  could occupy one worker. Historical logs contained over 200,000 queue-depth
  warnings (maximum observed queue 51) and repeated expired-session tracebacks.
  Capacity is now 64 workers; polling heartbeats are 10 seconds with a 30-second
  timeout. Each browser page shares namespace connections and loads Socket.IO
  once. KDS/cashier refresh after reconnect. This increases headroom; it is not
  unlimited capacity or a replacement for a future WebSocket-capable server.
- Handle only Engine.IO's exact expired-session KeyError as HTTP 400, allowing
  reconnection, without suppressing other application errors.
- Inventory loaded all paid orders before filtering dates in Python and made
  two closing queries per inventory item. It now filters sales in SQL and
  fetches current/preceding closing rows in two batched queries.
- Workstation lookups are cached only within each GET request. Leave ledger
  credits and operational-profile links are fetched in batches. SOP readiness
  preloads recipe/SOP versions. No cross-request stock cache was introduced.
- SQLite uses WAL and a 15-second busy timeout; synchronous durability was not
  relaxed. Backups must use SQLite's online backup API, or stop the service and
  copy the complete instance (including any WAL files), never just copy a live DB.
- Tunnel logs repeatedly showed QUIC timeouts. The Windows launcher now selects
  HTTP/2 over TCP, as supported by [Cloudflare's troubleshooting guidance](https://developers.cloudflare.com/tunnel/troubleshooting/).
- Watchdog requires HTTP 200 and retries three times before a health-triggered
  restart. A one-shot `instance/restart_tunnel.request` complements the existing
  app restart marker. Slow Flask requests (2+ seconds) log UTC time, endpoint,
  method, status and elapsed time, without request payloads or query strings.

## Validation

All destructive/business-action tests use disposable databases, not production.

- 45 automated tests, including stock thresholds, closing/purchase helpers,
  menu visibility/hours/navigation, SOP ownership, reusable asset loss policy,
  closing batching, leave-credit idempotence, WAL reader/writer concurrency,
  per-request caching and the narrowly scoped expired-session handler.
- Snapshot read-route audit covers ordering, QR, browsing, KDS, cashier, cash
  counter, inventory sections, staff sections, statistics/export, SOPs, library
  and breakage reporting, plus common pages for seven staff roles.
- Stock/financial context matches commit `23f5088` for today, this month and
  last month. Includes closing openings, stock bands, revenue/expenses,
  workstation finances and reusable-asset summaries.
- Inventory queries: 644 → 84; Statistics: 372 → 18; SOP management: 556 → 22.
  Latest snapshot Inventory pages take roughly 0.08–0.31 seconds server-side.
  These are test-machine timings, not guaranteed customer/network load times.
- Synthetic 32-polling-client test: 12 workers delayed a health request about
  9 seconds; 64 workers served it in about 0.02 seconds. No production load test.

Reproduce using the installed app Python from the experimental checkout:

```powershell
python -m unittest discover -s tests -q
python scripts/audit_performance.py --instance C:/Brownberries/brownberries-cafe-operations/instance --output logs/audit.json --compare-ref 23f5088 --check-js
python scripts/check_polling_capacity.py --threads 64 --clients 32
```

## Deployment / rollback

Pre-release source is `23f5088`. Preserve a timestamped rollback Git tag and
verified online SQLite backup, deployment configuration, instance uploads and
static uploads before fast-forwarding main. Request app/tunnel restarts through
the existing SYSTEM watchdog and verify both local and public health, process
arguments, HTTP/2 tunnel connections and newly appended logs.

Rollback code by reverting the release commit on main, then request both service
restarts. Keep the current production database: restoring an older DB would lose
orders/edits made after the backup. WAL is compatible with the previous code and
does not need disabling for code rollback. Restore data only for confirmed data
damage, with the app stopped and explicit reconciliation of newer transactions.

## Live release verification

- Released commit `c2472af` on main and experimental branch; pushed both and
  rollback tag `rollback-before-performance-20261002-0732` to origin.
- Backup: `C:/Brownberries/brownberries-cafe-operations/instance_windows_backup/20261002-0732-performance`.
  Online backup integrity check passed; 8,032 orders preserved, plus configuration
  and uploaded files. Production quick-check and order count matched after restart.
- SYSTEM watchdog restarted app at 07:33 and tunnel at 07:33 on 2 October 2026 IST.
  Both services running; local/public health passed. Live Engine.IO advertises the
  new 10-second ping / 30-second timeout. SQLite reports WAL. All four tunnel
  connections registered HTTP/2.
- Snapshot audit: 71 read requests, 70 HTTP 200 and one expected customer-login
  redirect; no server errors. Rendered inline JavaScript passed syntax checks.
  Browser checks confirmed public category/subcategory filtering and KDS `Live`
  status; no console warnings/errors in these checked tabs.
- Public latency remains variable: health probes ranged about 1.3–11 seconds;
  a Cloudflare edge-only response took 3.1 seconds (2.9 seconds through TLS),
  while the local menu took 0.031 seconds. Both IPv4 and IPv6 showed delays.
  This indicates network-path overhead remains; it is not a claim that every
  customer now receives sub-second pages. One HTTP/2 stream-close error was
  observed during checking; no full tunnel disconnect was observed after restart.

## Remaining limits

The host has approximately 4 GB RAM and less than 1 GB free during this audit.
Browser/API checks and unit tests do not prove every workflow or future peak load.
Internet/Wi-Fi outages, sleep and Windows restarts can still interrupt service.
AC idle sleep is disabled, but battery idle sleep is currently 15 minutes; keep
this server plugged in. No power or network security settings were changed.
This pass does not change attendance/payroll business rules, financial policies,
customer menu visibility, or remote account permissions. Longer-term resilience
requires a dedicated always-on host/reliable network and off-machine backups.
