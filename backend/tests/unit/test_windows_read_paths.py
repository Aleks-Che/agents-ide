"""Actual Windows aliases and held directory handles, with disposable paths."""

import os
import subprocess
from pathlib import Path

import pytest

from agents_ide.security.filesystem import directory_identity
from agents_ide.security.workspace_read import hold_command_directory, read_workspace_file

pytestmark = [pytest.mark.windows, pytest.mark.skipif(os.name != "nt", reason="Windows path APIs")]


def test_long_path_read_and_identity(tmp_path):
    root = tmp_path
    for index in range(14):
        root /= f"directory-{index:02}-with-space"
    root.mkdir(parents=True)
    assert len(str(root)) > 260
    (root / "context.txt").write_bytes(b"long path context")
    with hold_command_directory(root, root):
        assert read_workspace_file(root, "context.txt", 100) == b"long path context"
    assert directory_identity(root) == directory_identity(root / ".." / root.name)


def test_short_name_alias_has_same_identity(tmp_path):
    import win32api

    directory = tmp_path / "A directory with a long filename"
    directory.mkdir()
    short = Path(win32api.GetShortPathName(str(directory)))
    if str(short).lower() == str(directory).lower():
        pytest.skip("8.3 name creation is disabled on this volume")
    assert directory_identity(directory) == directory_identity(short)
    (directory / "context.txt").write_bytes(b"same object")
    assert read_workspace_file(short, "context.txt", 100) == b"same object"


def test_subst_alias_has_same_identity(tmp_path):
    import win32api

    existing = {
        item[:2].upper() for item in win32api.GetLogicalDriveStrings().split("\x00") if item
    }
    drive = next(
        (letter + ":" for letter in "ZYXWVUTSRQPONMLKJIHGFED" if letter + ":" not in existing), None
    )
    if drive is None:
        pytest.skip("No free drive letter for isolated SUBST test")
    subprocess.run(["subst.exe", drive, str(tmp_path)], check=True, capture_output=True)
    try:
        assert directory_identity(Path(drive + "\\")) == directory_identity(tmp_path)
        (tmp_path / "context.txt").write_bytes(b"subst context")
        assert read_workspace_file(Path(drive + "\\"), "context.txt", 100) == b"subst context"
    finally:
        subprocess.run(["subst.exe", drive, "/D"], check=True, capture_output=True)


def test_held_directory_cannot_be_replaced_during_read(tmp_path):
    directory = tmp_path / "held"
    directory.mkdir()
    (directory / "context.txt").write_bytes(b"stable")
    with hold_command_directory(tmp_path, directory):
        with pytest.raises(PermissionError):
            directory.rename(tmp_path / "replaced")
        assert read_workspace_file(tmp_path, "held/context.txt", 100) == b"stable"
