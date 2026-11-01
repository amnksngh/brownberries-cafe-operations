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
5. Approve a test order and click Yes. The modal reports queued, without opening
   browser print settings. Refresh the queue to see `test_complete` after polling.
6. Stop the agent, approve another test order and click Yes. It remains queued.
   Restart the agent to collect it. Clicking No never creates a job.

Do not create fake orders on production. Use the copied local database for this test.

## Dedicated kitchen Windows machine

The first agent supports **ESC/POS-compatible thermal printers**, 42-column text,
CP437 character encoding. Verify the printer model/emulation before live use.
Non-CP437 text (including Hindi) is rejected for review, not silently corrupted.
Unicode raster printing is not implemented in this first version.

1. Install the Windows printer driver and print its Windows test page.
2. Install Python 3.11 or newer with the Python launcher. Create a dedicated folder,
   for example `C:\BrownberriesKitchen`, and copy `scripts/kitchen_print_agent.py`
   from the deployed GitHub release into that folder. Do not copy a database,
   station key or the development machine's virtual environment.
3. Run these commands in PowerShell on the **kitchen PC**:

```powershell
cd C:\BrownberriesKitchen
py -3 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install pywin32
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
6. Keep the agent running. Unattended Windows-service installation/autostart still
   needs packaging and testing on the new kitchen machine. This script is currently
   a foreground agent, not an installed Windows service. Logging out stops it.

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
- Package unattended startup under the dedicated Windows account.
- Review manual queue cancellation and offline recovery with staff before
  enabling unattended live printing. Automatic expiry is not implemented.
- Validate physical output and power/network interruption recovery on kitchen hardware.
