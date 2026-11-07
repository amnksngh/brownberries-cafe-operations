# Kitchen KOT station — setup and operation

Receipt printing is unchanged. With no enabled kitchen station, KOTs retain their
existing browser-print behavior. Deploying code does not copy local station keys,
printer selections or test jobs to AWS. Register the live station separately.

## Local test without a printer

1. Sign in as admin/owner and open `/cafe/kot-print` (also linked from Cashier Desk).
2. Register the kitchen station and copy the secret key shown once. Keep it private.
3. Run the following from the development repository in PowerShell:

```powershell
& 'C:/Brownberries/brownberries-cafe-operations/.venv/Scripts/python.exe' scripts/kitchen_print_agent.py --url http://127.0.0.1:5052 --simulate
```

4. Paste the key at the hidden terminal prompt. Refresh the settings page. Select
   **SIMULATED KOT PRINTER**, keep **Test** mode and enable kitchen routing.
5. Approve a test order. It queues automatically without opening browser print settings. Refresh the queue to see `test_complete` after polling.
6. Stop the agent and approve another test order. It remains queued. Restart the agent to collect it.

Do not create fake orders on production. Use the copied local database for this test.

## Dedicated kitchen Windows machine

The agent supports 80mm, 203dpi ESC/POS-compatible thermal printers. Protocol 3
prints Unicode raster images, using Windows text shaping for Hindi. Copy both
`kitchen_print_agent.py` and `kot_windows_raster.py` into the kitchen folder.

1. Install the Windows printer driver and print its Windows test page.
2. Install Python 3.11 or newer with the Python launcher. Create a dedicated folder,
   for example `C:\BrownberriesKitchen`, and copy `scripts/kitchen_print_agent.py`
   from the deployed GitHub release into that folder. Do not copy a database,
   station key or the development machine's virtual environment.
3. Run these commands in PowerShell on the **kitchen PC**:

```powershell
cd C:\BrownberriesKitchen
py -3 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install pywin32 Pillow
& .\.venv\Scripts\python.exe -c "import win32print; print([p[2] for p in win32print.EnumPrinters(6)])"
```

4. As admin/owner, open https://brownberriescafe.com/cafe/kot-print and click
   **Register kitchen station**. Copy the secret key shown once. If a station already
   exists, stop its old agent before using **Replace station key**; this disables
   routing until the replacement is configured. Run:

```powershell
& .\.venv\Scripts\python.exe .\kitchen_print_agent.py --url https://brownberriescafe.com
```

5. Paste the live key at the hidden prompt (nothing appears while typing). Leave
   the terminal running. Refresh the live portal after 5–10 seconds. It should say
   **Connected** and list printers from this PC. Select the kitchen printer, keep
   **Test** mode, check **Route new KOT print requests to this station**, and save.
   A test job should reach `test_complete` without entering Windows' print queue.
   Then switch to **Live**, save, and verify a supervised physical KOT using an
   explicit reprint or the next legitimate approved order. Completed test jobs
   do not print automatically when the mode changes.
6. For unattended operation, follow **Automatic startup** below. Otherwise keep
   the foreground agent running.

Only outbound HTTPS is needed. Do not expose the Windows printer or a local HTTP
listener to the internet. Never put the station key in Git or a command line.
For unattended deployment, the agent also accepts `BROWNBERRIES_KOT_KEY`; provision
it securely for its dedicated Windows account, not system-wide shared terminals.
Run only one agent instance per registered station. Keep the PC awake, connected
to the internet, and logged in. No AWS account or AWS credentials are required on
the kitchen machine; only the dedicated station key. Do not use `127.0.0.1:5052`
on the kitchen PC for production; that address refers to that PC, not AWS.

## Daily startup and troubleshooting

- Start the agent with the command above after logging into Windows; enter the key.
- Confirm **Connected**, correct printer and **Live** mode on the live portal.
- If the dropdown is empty, check the agent's Python environment has `pywin32`,
  the agent is running, and the station key belongs to the same server URL.
- If a job is `test_complete`, no Windows job is expected. Use Live mode for paper.
- If `spooled` but no paper appears, inspect the Windows printer queue, USB/network
  connection, paper and printer errors before considering a reprint.
- Stop the agent with Ctrl+C before moving to another PC or replacing the key.

## Status and recovery

- `queued`: retained until an enabled station with the configured printer polls.
- `claimed`: delivered once to the agent. No automatic redelivery after timeouts.
- `test_complete`: rendered successfully without physical printing.
- `spooled`: Windows accepted the job. **Not a physical-print confirmation.**
- `needs_review`: failure or uncertain outcome; inspect Windows queue/paper first.
- `cancelled`: admin cancelled a queued job with a reason before the agent claimed it.

The agent journals its attempt before spooling and retries result acknowledgements
without reprinting. A network failure before claim delivery can leave a claimed
job uncertain; it is deliberately not retried automatically. After two minutes,
an admin can request a reprint with a reason and duplicate-risk confirmation.
Reprints are marked on paper. Check for delayed Windows jobs before reprinting.

Replacing the station key disables routing and clears the reported printer list;
the old key stops working. Queued jobs remain stored for the replacement station.
Claimed jobs require inspection. Changing mode/printer affects unclaimed jobs.
Offline queues do not expire automatically; inspect old jobs before bringing a
long-offline station back online. Exact-once physical printing cannot be guaranteed.

## Limitations and hardware commissioning

- Test the actual printer model, width, cutter, encoding and paper length.
- Commission the startup task under the dedicated Windows account.
- Review manual queue cancellation and offline recovery with staff before
  enabling unattended live printing. Automatic expiry is not implemented.
- Validate physical output and power/network interruption recovery on kitchen hardware.

## Automatic startup (Windows Task Scheduler)

Copy the updated `kitchen_print_agent.py` and `install_kitchen_startup.ps1`
into `C:\BrownberriesKitchen`, beside the existing `.venv` directory.
Stop any foreground agent first. Open PowerShell **as administrator under the
same kitchen Windows account** that owns the printer, then run:

```powershell
cd C:\BrownberriesKitchen
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\install_kitchen_startup.ps1
```

Enter that Windows account's **password**, not its PIN. Then paste the live
station key at the hidden prompt. Task Scheduler stores the Windows credentials;
the station key is encrypted using Windows current-user DPAPI and bound to the
server URL. Neither credential is placed in task arguments or logs.

The task starts immediately and at every Windows startup, without requiring
interactive sign-in. It retries network failures and restarts after process
failure. Keep the PC awake and printer connected. Check the portal's Connected
status and installed printer before enabling live printing. Verify again after
a reboot while the Windows account is signed out; printer-driver availability
in this background context must be checked on the actual kitchen machine.

Logs: `%LOCALAPPDATA%\BrownberriesKitchen\agent.log` (rotating, up to four 1MB files).
Task Scheduler task: **Brownberries Kitchen KOT**.

```powershell
Get-ScheduledTaskInfo -TaskName 'Brownberries Kitchen KOT'
Stop-ScheduledTask -TaskName 'Brownberries Kitchen KOT'
Start-ScheduledTask -TaskName 'Brownberries Kitchen KOT'
```

Registration is done once by an admin in the live portal. The server stores only
a hash, so it cannot return the original key. Subsequent boots automatically load
the saved encrypted key; they do not register again. If the key is lost or revoked,
an admin must replace it in the portal and rerun setup with the new key. Reselect
and enable the printer after rotation. Never silently rotate keys on authentication
failures. If the Windows account password changes, rerun setup to update the task.
Uninstall with `Unregister-ScheduledTask -TaskName 'Brownberries Kitchen KOT'`;
this does not delete the print journal or saved credential.

## Approved-order changes and kitchen acknowledgement

Use Cashier Desk > Edit Order. Enter a reason, adjust quantities or uncheck items,
then save. Cancelled rows remain in the database with their original quantities;
an audit record stores before/after snapshots and the acting staff account (or
an authorized reception kiosk context). A stale form must be refreshed before saving.

- The first kitchen ticket for an unsettled table is NEW ORDER; subsequent
  tickets are RUNNING (MODIFIED), or RUNNING (CANCELLED) when the affected items
  have no remaining quantity. Identical item/variety/parcel combinations are summed
  across the table's unpaid orders. For example, 2 cups then 3 more prints 2 → 5;
  cancelling one from the first order prints 5 → 4. Settlement starts a new cycle.
- Hindi names are optional in Menu Management. Blank names fall back to English.
  Dropdown reasons print in Hindi. Varieties remain as configured in the menu.
- Water, barista items, plain ice cream, and direct-resale items produce no KOT.
- Queued tickets stay in sequence, so the kitchen receives each quantity transition.
  Historical snapshots are never overwritten.
- Change jobs are queued automatically when the station is enabled. They also
  remain visible on Kitchen Display > Kitchen changes awaiting acknowledgement
  for browser printing if the station is unavailable or disabled.
- Kitchen staff must explicitly acknowledge the change after adjusting preparation.
  Windows spool completion does not acknowledge it. This records receipt of the
  instruction; it cannot prove the physical preparation was stopped.
- The generic kitchen status endpoint no longer silently cancels or revives orders;
  use Cashier Desk cancellation so the reason and change ticket are retained.

### Upgrade order and physical print test

The server accepts existing protocol 2 agents using their previous English layout.
To enable Hindi and table totals on paper, update the kitchen PC:

1. Stop the scheduled task (or press Ctrl+C in the manually running agent).
2. Copy **both** `kitchen_print_agent.py` and `kot_windows_raster.py` from this
   release to `C:\BrownberriesKitchen`. Keep the existing saved key and journal.
3. Run the following in PowerShell:

```powershell
cd C:\BrownberriesKitchen
& .\.venv\Scripts\python.exe -m pip install pywin32 Pillow
Start-ScheduledTask -TaskName 'Brownberries Kitchen KOT'
```

If not using the startup task, run:

```powershell
& .\.venv\Scripts\python.exe .\kitchen_print_agent.py --url https://brownberriescafe.com
```

No key rotation or registration is required. Never run both agents simultaneously.

The new layout uses a 576-dot raster with equal 24-dot (approximately 3mm) side
insets, left-aligned items/reasons and centered headings. It prints only the measured
content plus two final feed lines for the cutter. Browser or Windows page margins
are not used by RAW ESC/POS printing. Do not change to landscape or a long paper size.
The printer's physical head/cutter offset can still leave a small unavoidable gap.

Latin text uses Arial (a Helvetica-style fallback); Helvetica is not bundled.
If licensed Helvetica is installed for the agent's Windows account, set the user
environment variable `BROWNBERRIES_KOT_FONT` to `Helvetica` and restart the task.
Hindi uses Windows Nirmala UI. The preview has been visually checked; test an actual
long Hindi name, variety, quantity increase/decrease and cancellation on the kitchen
printer to confirm its hardware margins and raster support.

Cancelled item names are struck through. Served items show a check mark on displays;
paper already printed cannot update when service status changes.
