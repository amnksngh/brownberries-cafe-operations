"""Kitchen Windows agent. Requires pywin32 for real printers; stdlib test mode.

Outbound HTTPS only. Server claims are never automatically reprinted. A durable
local journal lets acknowledgement retries survive network failures and restarts.
"""
import argparse
import getpass
import json
import logging
from logging.handlers import RotatingFileHandler
import os
import sqlite3
import textwrap
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlparse


def saved_key(base, value=None):
    """Windows current-user DPAPI; ciphertext is bound to this server URL."""
    import win32crypt
    folder = Path(os.environ['LOCALAPPDATA']) / 'BrownberriesKitchen'
    folder.mkdir(parents=True, exist_ok=True)
    import hashlib
    path = folder / (hashlib.sha256(base.encode()).hexdigest() + '.key')
    entropy = base.encode()
    if value is not None:
        encrypted = win32crypt.CryptProtectData(value.encode(), 'Kitchen station', entropy, None, None, 0)
        temporary = path.with_suffix('.tmp')
        temporary.write_bytes(encrypted)
        temporary.replace(path)
        return value
    return win32crypt.CryptUnprotectData(path.read_bytes(), entropy, None, None, 0)[1].decode()


def clean(value):
    return ''.join(c if c.isprintable() else ' ' for c in str(value))


def render(ticket, attempt=1):
    # 80mm / 203dpi head: 576 dots, 24-dot inset each side, Font A 12 dots.
    # Use explicit printer settings rather than whatever the preceding receipt left.
    out = bytearray(b'\x1b@\x1bt\x00\x1bM\x00\x1b3\x20\x1dL\x18\x00\x1dW\x10\x02')
    width = 44
    def line(value='', bold=False, tall=False):
        out.extend(b'\x1bE' + bytes([int(bold)]) + b'\x1d!' + bytes([1 if tall else 0]))
        for text in textwrap.wrap(clean(value), width=width) or ['']:
            out.extend((text+'\n').encode('cp437', errors='strict'))
        out.extend(b'\x1bE\x00\x1d!\x00')
    line('BROWNBERRIES - KITCHEN', bold=True)
    line(ticket.get('kind', 'NEW KITCHEN ORDER'), bold=True, tall=True)
    if attempt > 1:
        line(f'REPRINT - ATTEMPT {attempt}', bold=True, tall=True)
    line(f'TABLE {ticket.get("table", "")} | ORDER {ticket.get("order_id", "")}', bold=True, tall=True)
    line(f'Ref: {ticket.get("reference", "")} | Ticket: {ticket.get("sequence", "-")}')
    line(clean(ticket.get('approved_at', '')) + ' IST')
    if ticket.get('reason'):
        line('Reason: '+ticket['reason'])
    line('='*width)
    for item in ticket['items']:
        if item.get('action'):
            line(f'{item["action"]}: {item["quantity"]}', bold=True, tall=True)
        if item.get('action') == 'CANCEL ITEM':
            # Raster text allows a true strike across the name; ESC/POS has no
            # portable strikethrough text command. Pillow is required for this.
            from PIL import Image, ImageDraw, ImageFont
            try:
                font = ImageFont.truetype('arial.ttf', 24)
            except OSError:
                font = ImageFont.load_default(size=24)
            for text in textwrap.wrap(clean(f'{item["quantity"]} x {item["name"]}'), width=32):
                canvas = Image.new('1', (528, 36), 1)
                draw = ImageDraw.Draw(canvas)
                draw.text((0, 0), text, font=font, fill=0)
                box = draw.textbbox((0, 0), text, font=font)
                middle = (box[1]+box[3])//2
                draw.line((0, middle, min(527, box[2]), middle), fill=0, width=2)
                raster = bytearray()
                for y in range(36):
                    for x in range(0,528,8):
                        raster.append(sum((1 << (7-bit)) for bit in range(8) if not canvas.getpixel((x+bit,y))))
                out.extend(b'\x1dv0\x00\x42\x00\x24\x00'+raster)
        else:
            line(f'{item["quantity"]} x {item["name"]}', bold=True, tall=True)
        if item.get('size'):
            line('Variety: '+item['size'])
        if item.get('parcel'):
            line('PARCEL', bold=True)
        if 'previous' in item:
            line(f'Previous: {item["previous"]} -> NEW TOTAL: {item["current"]}', bold=True)
        line('-'*width)
    if ticket.get('protocol') == 2:
        line('Acknowledge change on Kitchen Display.', bold=True)
    out.extend(b'\n\n\x1dV\x00')
    return bytes(out)


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
    parser.add_argument('--save-key', action='store_true', help='Save key encrypted for this Windows user, then exit')
    parser.add_argument('--unattended', action='store_true', help='Use saved key without prompting; write rotating logs')
    args = parser.parse_args()
    base = args.url.rstrip('/')
    parsed = urlparse(base)
    if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1','localhost'}):
        parser.error('HTTPS required except for loopback testing.')
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        parser.error('Use a base URL without credentials, query or fragment.')
    if args.save_key:
        token = getpass.getpass('Kitchen station key: ').strip()
        if not token:
            parser.error('Station key cannot be empty.')
        saved_key(base, token)
        print('Station key saved encrypted for this Windows account and server.')
        return
    if args.unattended:
        folder = Path(os.environ['LOCALAPPDATA']) / 'BrownberriesKitchen'
        folder.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(folder / 'agent.log', maxBytes=1000000, backupCount=3)
        logging.basicConfig(level=logging.INFO, handlers=[handler], format='%(asctime)s %(message)s')
        token = saved_key(base)
    else:
        token = os.environ.get('BROWNBERRIES_KOT_KEY') or getpass.getpass('Kitchen station key: ').strip()
    def report(message):
        logging.info(message)
        if not args.unattended:
            print(message)
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            raise RuntimeError('Redirect rejected; check the configured server URL.')
    opener = urllib.request.build_opener(NoRedirect)
    def api(path, body):
        req = urllib.request.Request(base+path, json.dumps(body).encode(),
            headers={'Content-Type':'application/json', 'Authorization':'Bearer '+token, 'User-Agent':'BrownberriesKitchenAgent/1.0'}, method='POST')
        with opener.open(req, timeout=20) as response:
            return json.load(response)
    journal = Path(args.journal)
    journal.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(journal)
    conn.execute('PRAGMA synchronous=FULL')
    conn.execute('CREATE TABLE IF NOT EXISTS jobs (server TEXT, id INTEGER, key TEXT, status TEXT, detail TEXT, ack INTEGER DEFAULT 0, PRIMARY KEY(server,id))')
    conn.commit()
    report('Kitchen agent running.')
    while True:
        try:
            for jid, key, status, detail in conn.execute('SELECT id,key,status,detail FROM jobs WHERE server=? AND ack=0', (base,)).fetchall():
                api(f'/api/kot-station/result/{jid}', dict(claim_key=key, status=status, detail=detail))
                conn.execute('UPDATE jobs SET ack=1 WHERE server=? AND id=?', (base,jid))
                conn.commit()
            installed = ['SIMULATED KOT PRINTER'] if args.simulate else printers()
            poll_result = api('/api/kot-station/poll', dict(printers=installed, protocol=2))
            if poll_result.get('upgrade_required'):
                report('Agent upgrade required; no job claimed.')
            job = poll_result.get('job')
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
                report(f'Job {job["id"]}: {status}')
        except KeyboardInterrupt:
            break
        except Exception as exc:
            report(f"Agent waiting: {type(exc).__name__}; HTTP {getattr(exc, 'code', 'n/a')}. Check network, station key and printer. No automatic reprint.")
        try:
            time.sleep(5)
        except KeyboardInterrupt:
            break
    conn.close()


if __name__ == '__main__':
    main()
