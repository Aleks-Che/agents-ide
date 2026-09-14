import os
import re
import uuid
from pathlib import Path

from agents_ide.errors import AppError
from agents_ide.logging import register_secret
from agents_ide.security.filesystem import atomic_write


class SecretStore:
    """Immutable DPAPI user-scope versions. No portable plaintext fallback."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def put(self, value: str) -> str:
        if os.name != "nt":
            raise AppError("secret_unavailable", "DPAPI requires Windows", 503)
        import win32crypt

        register_secret(value)
        reference = uuid.uuid4().hex
        try:
            encrypted = win32crypt.CryptProtectData(
                value.encode("utf-8"), "Agents IDE", None, None, None, 1
            )
            atomic_write(self.directory / f"{reference}.dpapi", encrypted)
        except Exception:
            raise AppError("secret_unavailable", "Cannot protect the secret", 503) from None
        return reference

    def get(self, reference: str) -> str:
        if not re.fullmatch(r"[a-f0-9]{32}", reference):
            raise AppError("secret_unavailable", "Invalid secret reference", 503)
        try:
            if os.name != "nt":
                raise OSError("DPAPI requires Windows")
            import win32crypt

            ciphertext = (self.directory / f"{reference}.dpapi").read_bytes()
            value = win32crypt.CryptUnprotectData(ciphertext, None, None, None, 1)[1].decode()
            register_secret(value)
            return value
        except Exception:
            # A missing Windows profile must never destroy existing ciphertext.
            raise AppError(
                "secret_unavailable", "Secret cannot be decrypted by this user", 503
            ) from None
