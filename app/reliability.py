"""Small transport/database safeguards for the Windows production server."""
import sqlite3
from datetime import datetime, timezone
from time import perf_counter

from sqlalchemy import event
from flask import g, request


def register_request_timing(app):
    @app.before_request
    def begin_request():
        g.request_started = perf_counter()

    @app.after_request
    def record_slow_request(response):
        elapsed = perf_counter() - g.get("request_started", perf_counter())
        if elapsed >= 2:
            # No URLs, query strings, cookies or request bodies in diagnostics.
            app.logger.warning("slow_request at=%s endpoint=%s method=%s status=%s seconds=%.3f",
                               datetime.now(timezone.utc).isoformat(), request.endpoint,
                               request.method, response.status_code, elapsed)
        return response


def configure_sqlite(engine):
    if engine.dialect.name != "sqlite":
        return

    @event.listens_for(engine, "connect")
    def connection_settings(connection, _record):
        if isinstance(connection, sqlite3.Connection):
            connection.execute("PRAGMA busy_timeout=15000")

    # WAL lets readers continue during short writes. Do not relax durability.
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")


class DisconnectedPollingSession:
    """Return a reconnectable 400 for Engine.IO's expired-POST race.

    A polling session can close between Engine.IO's existence check and its
    socket lookup. Only this exact error on the transport endpoint is handled;
    application failures and unrelated KeyErrors must still be reported.
    """
    def __init__(self, app):
        self.app = app

    def __call__(self, environ, start_response):
        try:
            return self.app(environ, start_response)
        except KeyError as exc:
            if (environ.get("PATH_INFO", "").rstrip("/") != "/socket.io"
                    or exc.args != ("Session is disconnected",)):
                raise
            body = b'"Session expired; reconnect"'
            start_response("400 Bad Request", [
                ("Content-Type", "application/json"),
                ("Content-Length", str(len(body))),
                ("Cache-Control", "no-store"),
            ])
            return [body]
