"""Kitchen Windows agent. Requires pywin32 for real printers; stdlib test mode.

Outbound HTTPS only. Server claims are never automatically reprinted. A durable
local journal lets acknowledgement retries survive network failures and restarts.
"""
import argparse
import getpass
import json
import os
import sqlite3
import textwrap
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse


def clean(value):
    return ''.join(c if c.isprintable() else ' ' for c in str(value))


def render(ticket, attempt=1):
    lines = ['BROWNBERRIES - KOT']
    if attempt > 1:
        lines.append(f'REPRINT - ATTEMPT {attempt}')
    lines += [f'Table: {clean(ticket.get("table", ""))}',
              f'Order: {clean(ticket.get("order_id", ""))} / {clean(ticket.get("reference", ""))}',
              clean(ticket.get('approved_at', '')) + ' IST', '-'*42]
    for item in ticket['items']:
        label = clean(item['name'])
        if item.get('size'):
            label += f' ({clean(item["size"])})'
        if item.get('parcel'):
            label += ' [PARCEL]'
        wrapped = textwrap.wrap(label, width=34) or ['Item']
        lines.append(wrapped[0].ljust(35) + clean(item['quantity']))
        lines.extend(wrapped[1:])
    lines.append('-'*42)
    # Reject unsupported glyphs rather than silently printing incorrect names.
    content = ('\n'.join(lines)+'\n\n\n').encode('cp437', errors='strict')
    return b'\x1b@\x1bt\x00'+content+b'\x1dV\x00'


def printers():
    import win32print
    return [p[2] for p in win32print.EnumPrinters(win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS)]


def spool(name, payload, job_id):
    import win32print
    handle = win32print.OpenPrinter(name)
    try:
        spool_id = win32print.StartDocPrinter(handle, 1, (f'Brownberries KOT job {job_id}', None, 'RAW'))
        try:
            win32print.StartPagePrinter(handle)
            written = win32print.WritePrinter(handle, payload)
            if written != len(payload):
                raise RuntimeError('Incomplete spool write; check printer before retrying.')
            win32print.EndPagePrinter(handle)
        finally:
            win32print.EndDocPrinter(handle)
        return spool_id
    finally:
        win32print.ClosePrinter(handle)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', required=True)
    parser.add_argument('--journal', default=str(Path.home()/'BrownberriesKitchen'/'jobs.sqlite'))
    parser.add_argument('--simulate', action='store_true', help='Advertise a virtual printer; NEVER send paper')
    args = parser.parse_args()
    base = args.url.rstrip('/')
    parsed = urlparse(base)
    if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1','localhost'}):
        parser.error('HTTPS required except for loopback testing.')
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        parser.error('Use a base URL without credentials, query or fragment.')
    token = os.environ.get('BROWNBERRIES_KOT_KEY') or getpass.getpass('Kitchen station key: ')
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise RuntimeError('Redirect rejected; check the configured server URL.')
    opener = urllib.request.build_opener(NoRedirect)
    def api(path, body):
        req = urllib.request.Request(base+path, json.dumps(body).encode(),
            headers={'Content-Type':'application/json', 'Authorization':'Bearer '+token}, method='POST')
        with opener.open(req, timeout=20) as response:
            return json.load(response)
    journal = Path(args.journal)
    journal.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(journal)
    conn.execute('PRAGMA synchronous=FULL')
    conn.execute('CREATE TABLE IF NOT EXISTS jobs (server TEXT, id INTEGER, key TEXT, status TEXT, detail TEXT, ack INTEGER DEFAULT 0, PRIMARY KEY(server,id))')
    conn.commit()
    print('Kitchen agent running. Keep this process active. Ctrl+C to stop.')
    while True:
        try:
            for jid, key, status, detail in conn.execute('SELECT id,key,status,detail FROM jobs WHERE server=? AND ack=0', (base,)).fetchall():
                api(f'/api/kot-station/result/{jid}', dict(claim_key=key, status=status, detail=detail))
                conn.execute('UPDATE jobs SET ack=1 WHERE server=? AND id=?', (base,jid))
                conn.commit()
            installed = ['SIMULATED KOT PRINTER'] if args.simulate else printers()
            job = api('/api/kot-station/poll', dict(printers=installed)).get('job')
            if job:
                # Persist uncertainty BEFORE any physical side effect.
                conn.execute('INSERT INTO jobs(server,id,key,status,detail) VALUES(?,?,?,?,?)',
                    (base,job['id'],job['claim_key'],'needs_review','Agent interrupted before confirmed spool result; check printer.'))
                conn.commit()
                status, detail = 'needs_review', 'Not sent'
                try:
                    payload = render(job['ticket'], job['attempt'])
                    if job['test_mode']:
                        status, detail = 'test_complete', f'Test rendered {len(payload)} bytes; no paper sent.'
                    elif args.simulate:
                        detail = 'Simulation agent refused a live printing job.'
                    elif job['printer'] not in installed:
                        detail = 'Configured printer is not installed.'
                    else:
                        spool_id = spool(job['printer'], payload, job['id'])
                        status, detail = 'spooled', f'Windows spool job {spool_id}; physical output not verified.'
                except Exception as exc:
                    detail = f'{type(exc).__name__}: {exc}'[:500]
                conn.execute('UPDATE jobs SET status=?,detail=? WHERE server=? AND id=?', (status,detail,base,job['id']))
                conn.commit()
                print(f'Job {job["id"]}: {status}')
        except KeyboardInterrupt:
            break
        except Exception as exc:
            print(f'Agent waiting: {type(exc).__name__}. Check network, station key and printer. No automatic reprint.')
        time.sleep(5)
    conn.close()


if __name__ == '__main__':
    main()
