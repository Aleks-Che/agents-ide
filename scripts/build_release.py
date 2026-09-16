"""Build a versioned offline UI/source bundle; Node is needed only on the build host."""

import argparse
import hashlib
import json
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True, help="New archive path")
    args = parser.parse_args()
    repository = Path(__file__).resolve().parents[1]
    destination = args.output.absolute()
    if destination.exists():
        parser.error("Output already exists; choose a new path")
    npm = "npm.cmd" if sys.platform == "win32" else "npm"
    subprocess.run([npm, "ci"], cwd=repository / "frontend", check=True)
    subprocess.run([npm, "run", "build"], cwd=repository / "frontend", check=True)
    version = tomllib.loads((repository / "backend/pyproject.toml").read_text())["project"][
        "version"
    ]
    files = [
        repository / name
        for name in (
            "README.md",
            "backend/pyproject.toml",
            "backend/uv.lock",
            "backend/.python-version",
            "scripts/install.ps1",
            "scripts/agents-ide.ps1",
        )
    ]
    for root in ("backend/src", "frontend/dist", "docs"):
        files.extend(
            path
            for path in (repository / root).rglob("*")
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
        )
    manifest = {
        "version": version,
        "ui": "bundled",
        "node_required_at_runtime": False,
        "files": [
            {
                "path": path.relative_to(repository).as_posix(),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            }
            for path in sorted(files)
        ],
    }
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(files):
            archive.write(path, path.relative_to(repository).as_posix())
        archive.writestr("release-manifest.json", json.dumps(manifest, indent=2))
    print(
        json.dumps(
            {
                "archive": str(destination),
                "version": version,
                "files": len(files),
                "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
            }
        )
    )


if __name__ == "__main__":
    main()
