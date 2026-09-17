"""Probe the production Council filesystem profile without any model request."""

import argparse
import json
import os
import tempfile
from pathlib import Path

from agents_ide.adapters.codex import CodexAdapter
from agents_ide.engine.codex_runtime import CodexRuntime
from agents_ide.security.filesystem import private_directory


def probe(executable: str) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="agents-ide-council-policy-") as directory:
        parent = Path(directory)
        private_directory(parent)
        root = parent / "artifacts"
        private_directory(root)
        (root / "context.txt").write_text("ALLOWED_SYNTHETIC_CONTEXT", encoding="utf-8")
        (parent / ".env").write_text("DENIED_SYNTHETIC_SECRET", encoding="utf-8")
        runtime = CodexRuntime.start(executable=executable, workspace_path=root, isolated_read=True)
        try:
            adapter = CodexAdapter(stream=runtime.stream, session=runtime.session())
            shell = str(
                Path(os.environ["SYSTEMROOT"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
            )
            commands = {
                "allowed_read": "Get-Content -LiteralPath context.txt",
                "outside_read": "Get-Content -LiteralPath ../.env",
                "inside_write": "Set-Content -LiteralPath forbidden.txt -Value denied",
                "outside_write": "Set-Content -LiteralPath ../forbidden.txt -Value denied",
                "allowed_read_after_denials": "Get-Content -LiteralPath context.txt",
            }
            observations = {}
            for name, command in commands.items():
                response = adapter._request_response(
                    "command/exec",
                    {
                        "command": [
                            shell,
                            "-NoProfile",
                            "-Command",
                            "$ErrorActionPreference='Stop'; " + command,
                        ],
                        "cwd": str(root),
                        "timeoutMs": 10000,
                    },
                    timeout=20,
                )
                result = response.get("result", {})
                output = result.get("stdout", "")
                error = result.get("stderr", "")
                observations[name] = {
                    "rpc_error": bool(response.get("error")),
                    "rpc_error_details": (
                        str(response["error"])
                        .replace(str(parent), "<probe>")
                        .replace(str(Path.home()), "<user>")[:2048]
                        if response.get("error")
                        else None
                    ),
                    "exit_code": result.get("exitCode"),
                    "allowed_marker": "ALLOWED_SYNTHETIC_CONTEXT" in output,
                    "secret_marker": "DENIED_SYNTHETIC_SECRET" in output,
                    "access_denied": "PermissionDenied" in error or "UnauthorizedAccess" in error,
                }
            passed = (
                observations["allowed_read"]["allowed_marker"]
                and all(
                    observations[name]["access_denied"]
                    for name in ("outside_read", "inside_write", "outside_write")
                )
                and not any(item["secret_marker"] for item in observations.values())
                and not (root / "forbidden.txt").exists()
                and not (parent / "forbidden.txt").exists()
            )
            return {
                "passed": passed,
                "version": runtime.server_version,
                "model_calls": 0,
                "profile": "agents_ide_council",
                "private_acl": True,
                "sandbox_warmup_retried": runtime.sandbox_warmup_retried,
                "observations": observations,
            }
        finally:
            runtime.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("Output already exists")
    result = probe(args.executable)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
