"""Change detection for native auth/config files, without storing their content."""

import hashlib
import os
from pathlib import Path


def fingerprint(kind: str, executable: str | None) -> str:
    user = Path.home()
    if kind == "codex":
        root = Path(os.environ.get("CODEX_HOME", user / ".codex"))
        paths = [root / name for name in ("auth.json", "config.toml")]
    else:
        data = Path(os.environ.get("XDG_DATA_HOME", user / ".local/share")) / "opencode"
        config = Path(os.environ.get("XDG_CONFIG_HOME", user / ".config")) / "opencode"
        paths = [
            data / "auth.json",
            *(config / name for name in ("opencode.json", "opencode.jsonc", "config.json")),
        ]
    digest = hashlib.sha256()
    digest.update((executable or "").encode())
    if executable:
        try:
            stat = Path(executable).stat()
            digest.update(f"{stat.st_size}:{stat.st_mtime_ns}".encode())
        except OSError:
            digest.update(b"executable-unavailable")
    for path in paths:
        digest.update(str(path).encode())
        try:
            with path.open("rb") as stream:
                digest.update(hashlib.file_digest(stream, "sha256").digest())
        except OSError:
            digest.update(b"unavailable")
    return digest.hexdigest()
