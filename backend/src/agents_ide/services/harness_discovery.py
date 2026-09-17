"""Find supported native installations without executing shell wrappers."""

import os
import shutil
from pathlib import Path


def native_executable(path: Path) -> bool:
    return path.is_file() and (
        path.suffix.lower() == ".exe" if os.name == "nt" else os.access(path, os.X_OK)
    )


def resolve_installation(kind: str, path: Path) -> Path | None:
    if native_executable(path):
        return path.resolve()
    # npm exposes .cmd/.ps1 shims on Windows. Locate their package's native
    # executable; never parse or execute a wrapper as shell code.
    root = path.parent / "node_modules"
    patterns = (
        ("opencode-ai/bin/opencode.exe", "opencode-ai/node_modules/opencode-*/bin/opencode.exe")
        if kind == "opencode"
        else (
            "@openai/codex/vendor/*/codex/codex.exe",
            "@openai/codex/node_modules/@openai/codex-*/vendor/*/codex/codex.exe",
            "@openai/codex-*/vendor/*/codex/codex.exe",
        )
    )
    for pattern in patterns:
        for candidate in sorted(root.glob(pattern)):
            if native_executable(candidate):
                return candidate.resolve()
    return None


def discover_executables() -> dict[str, str]:
    user = Path.home()
    directories = [
        user / ".local/bin",
        user / ".opencode/bin",
        user / "scoop/shims",
        user / ".bun/bin",
    ]
    if os.environ.get("APPDATA"):
        directories.append(Path(os.environ["APPDATA"]) / "npm")
    result: dict[str, str] = {}
    for kind in ("codex", "opencode"):
        found = shutil.which(kind)
        candidates = [Path(found)] if found else []
        for directory in directories:
            candidates.extend(directory / (kind + suffix) for suffix in (".exe", ".cmd", ""))
        if kind == "codex" and os.environ.get("LOCALAPPDATA"):
            bundled = Path(os.environ["LOCALAPPDATA"]) / "OpenAI/Codex/bin"
            candidates.extend(
                sorted(bundled.glob("*/codex.exe"), key=lambda p: p.stat().st_mtime, reverse=True)
            )
        for candidate in candidates:
            resolved = resolve_installation(kind, candidate)
            if resolved is not None:
                result[kind] = str(resolved)
                break
    return result
