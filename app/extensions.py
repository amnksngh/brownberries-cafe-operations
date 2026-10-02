from flask_sqlalchemy import SQLAlchemy
from flask_socketio import SocketIO

db = SQLAlchemy()
socketio = SocketIO(
    async_mode="threading",
    cors_allowed_origins="*",
    allow_upgrades=False,
    ping_interval=10,
    ping_timeout=30,
)
