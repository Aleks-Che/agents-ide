import hashlib
import secrets
import time
from dataclasses import dataclass

import portalocker
from sqlalchemy import Engine, text

from agents_ide.config import Settings
from agents_ide.errors import AppError
from agents_ide.logging import register_secret
from agents_ide.security.filesystem import atomic_write

COOKIE_NAME = "agents_ide_session"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@dataclass(frozen=True)
class Session:
    token_hash: str
    csrf_token: str
    expires_at: float


class AuthService:
    def __init__(self, settings: Settings, engine: Engine) -> None:
        self.settings = settings
        self.engine = engine
        self.code_file = settings.data_dir / "runtime/pair-code"

    def _lock(self) -> portalocker.Lock:
        return portalocker.Lock(str(self.settings.data_dir / "runtime/auth.lock"), timeout=5)

    def issue_code(self, rotate: bool = False) -> str:
        with self._lock(), self.engine.begin() as connection:
            row = connection.execute(text("SELECT * FROM pairing WHERE id=1")).mappings().first()
            if (
                not rotate
                and row
                and row["expires_at"] > time.time()
                and row["attempts"] < 5
                and self.code_file.exists()
            ):
                code = self.code_file.read_text(encoding="utf-8")
                if secrets.compare_digest(digest(code), row["code_hash"]):
                    register_secret(code)
                    return code
            code = secrets.token_urlsafe(24)
            register_secret(code)
            connection.execute(text("DELETE FROM pairing"))
            connection.execute(
                text("INSERT INTO pairing VALUES (1, :hash, :expiry, 0, 0)"),
                {"hash": digest(code), "expiry": time.time() + self.settings.pairing_seconds},
            )
            atomic_write(self.code_file, code.encode())
            return code

    def cleanup(self) -> None:
        with self._lock(), self.engine.begin() as connection:
            row = connection.execute(text("SELECT * FROM pairing WHERE id=1")).mappings().first()
            if not row or row["expires_at"] <= time.time() or row["attempts"] >= 5:
                connection.execute(text("DELETE FROM pairing"))
                self.code_file.unlink(missing_ok=True)
            connection.execute(
                text("DELETE FROM auth_sessions WHERE expires_at <= :now"), {"now": time.time()}
            )

    def pair(self, code: str) -> tuple[str, Session]:
        now = time.time()
        error: AppError | None = None
        token = secrets.token_urlsafe(32)
        session = Session(
            digest(token), secrets.token_urlsafe(32), now + self.settings.session_seconds
        )
        with self._lock(), self.engine.begin() as connection:
            row = connection.execute(text("SELECT * FROM pairing WHERE id=1")).mappings().first()
            if not row or row["expires_at"] <= now or row["attempts"] >= 5:
                connection.execute(text("DELETE FROM pairing"))
                self.code_file.unlink(missing_ok=True)
                error = AppError("pairing_invalid", "Код недействителен или истёк", 401)
            elif row["next_attempt_at"] > now:
                error = AppError("rate_limited", "Подождите перед следующей попыткой", 429)
            elif not secrets.compare_digest(row["code_hash"], digest(code)):
                connection.execute(
                    text(
                        "UPDATE pairing SET attempts=attempts+1, next_attempt_at=:next WHERE id=1"
                    ),
                    {"next": now + 1},
                )
                if row["attempts"] >= 4:
                    self.code_file.unlink(missing_ok=True)
                error = AppError("pairing_invalid", "Код недействителен или истёк", 401)
            else:
                connection.execute(text("DELETE FROM pairing"))
                self.code_file.unlink(missing_ok=True)
                connection.execute(
                    text("INSERT INTO auth_sessions VALUES (:hash, :csrf, :expiry, 0)"),
                    {
                        "hash": session.token_hash,
                        "csrf": session.csrf_token,
                        "expiry": session.expires_at,
                    },
                )
        if error:
            raise error
        register_secret(token)
        register_secret(session.csrf_token)
        return token, session

    def authenticate(self, token: str | None) -> Session:
        if not token or len(token) > 128:
            raise AppError("auth_required", "Войдите с помощью локального кода", 401)
        with self.engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT * FROM auth_sessions WHERE token_hash=:hash "
                        "AND revoked=0 AND expires_at>:now"
                    ),
                    {"hash": digest(token), "now": time.time()},
                )
                .mappings()
                .first()
            )
        if not row:
            raise AppError("auth_required", "Сессия истекла или отозвана", 401)
        register_secret(token)
        register_secret(row["csrf_token"])
        return Session(row["token_hash"], row["csrf_token"], row["expires_at"])

    def revoke(self, session: Session) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text("UPDATE auth_sessions SET revoked=1 WHERE token_hash=:hash"),
                {"hash": session.token_hash},
            )
