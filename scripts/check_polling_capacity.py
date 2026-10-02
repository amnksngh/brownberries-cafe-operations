"""Local-only capacity regression: real Engine.IO polls against Waitress.

Does not connect to the live cafe or modify its database. Compare --threads 12
with --threads 64 to reproduce worker starvation independently of app queries.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sys
import threading
import time
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from flask import Flask
from flask_socketio import SocketIO
from waitress import create_server
from app.reliability import DisconnectedPollingSession


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--threads", type=int, default=64)
    parser.add_argument("--clients", type=int, default=32)
    args = parser.parse_args()
    app = Flask(__name__)
    io = SocketIO(app, async_mode="threading", allow_upgrades=False, ping_interval=10, ping_timeout=30)
    app.wsgi_app = DisconnectedPollingSession(app.wsgi_app)
    app.add_url_rule("/healthz", view_func=lambda: "ok")
    server = create_server(app, host="127.0.0.1", port=0, threads=args.threads)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = "http://127.0.0.1:" + str(server.effective_port)
    def read(path):
        with urlopen(base + path, timeout=20) as response:
            return response.read().decode()
    path = "/socket.io/?EIO=4&transport=polling"
    sessions = [json.loads(read(path)[1:])["sid"] for _ in range(args.clients)]
    with ThreadPoolExecutor(max_workers=args.clients) as pool:
        pending = [pool.submit(read, path + "&sid=" + sid) for sid in sessions]
        time.sleep(0.75)
        start = time.perf_counter()
        assert read("/healthz") == "ok"
        elapsed = time.perf_counter() - start
        print(json.dumps(dict(threads=args.threads, clients=args.clients,
                              health_seconds=round(elapsed, 3))), flush=True)
        # Explicitly close only our synthetic sessions to release held polls.
        for connection in list(io.server.eio.sockets.values()):
            connection.close(wait=False)
        io.server.eio.sockets.clear()
        for result in pending:
            result.result()
    server.task_dispatcher.shutdown()
    server.close()
    if args.threads >= 64 and elapsed > 2:
        raise SystemExit("Health requests blocked by polling clients")


if __name__ == "__main__":
    main()
