import logging
import os
import subprocess

import pytest
from pydantic import ValidationError

from agents_ide.config import Settings
from agents_ide.errors import AppError
from agents_ide.logging import JsonFormatter, redact, register_secret
from agents_ide.security.filesystem import directory_identity, prepare_data_dir
from agents_ide.security.secrets import SecretStore


def test_configuration_refuses_network_listeners():
    for host in ("0.0.0.0", "192.168.1.1", "localhost"):
        with pytest.raises(ValidationError):
            Settings(host=host)
    for origin in ("http://evil.test:5173", "http://localhost:5173/", "http://u:p@localhost:5173"):
        with pytest.raises(ValidationError):
            Settings(dev_origin=origin)


def test_redaction_at_log_boundary():
    register_secret("example-secret-value")
    assert redact({"Authorization": "anything", "value": "example-secret-value"}) == {
        "Authorization": "[REDACTED]",
        "value": "[REDACTED]",
    }
    record = logging.LogRecord(
        "test", 20, "", 0, "token=abc Bearer def example-secret-value", (), None
    )
    output = JsonFormatter().format(record)
    assert "abc" not in output and "def" not in output and "example-secret-value" not in output


@pytest.mark.windows
@pytest.mark.skipif(os.name != "nt", reason="DPAPI requires the Windows user profile")
def test_real_dpapi_roundtrip_and_unavailable_keeps_ciphertext(settings):
    prepare_data_dir(settings.data_dir)
    store = SecretStore(settings.data_dir / "secrets")
    reference = store.put("private-test-value")
    path = store.directory / f"{reference}.dpapi"
    assert b"private-test-value" not in path.read_bytes()
    assert store.get(reference) == "private-test-value"
    path.write_bytes(b"unavailable-profile-ciphertext")
    with pytest.raises(AppError, match="cannot be decrypted") as error:
        store.get(reference)
    assert error.value.code == "secret_unavailable"
    assert path.read_bytes() == b"unavailable-profile-ciphertext"


def test_directory_identity_spaces_unicode_and_alias(tmp_path):
    directory = tmp_path / "проект с пробелами"
    directory.mkdir()
    assert directory_identity(directory) == directory_identity(directory / ".." / directory.name)


@pytest.mark.windows
@pytest.mark.skipif(os.name != "nt", reason="Windows ACL")
def test_data_acl_has_only_current_user(settings):
    import win32security

    prepare_data_dir(settings.data_dir)
    descriptor = win32security.GetNamedSecurityInfo(
        str(settings.data_dir / "secrets"),
        win32security.SE_FILE_OBJECT,
        win32security.DACL_SECURITY_INFORMATION,
    )
    acl = descriptor.GetSecurityDescriptorDacl()
    assert acl.GetAceCount() == 1


def test_existing_user_directory_is_not_repurposed(tmp_path):
    (tmp_path / "user-file.txt").write_text("keep this content")
    with pytest.raises(AppError, match="dedicated"):
        prepare_data_dir(tmp_path)
    assert (tmp_path / "user-file.txt").read_text() == "keep this content"


@pytest.mark.windows
@pytest.mark.skipif(os.name != "nt", reason="Windows junction identity")
def test_junction_alias_identity_and_data_directory_rejection(tmp_path):
    target = tmp_path / "target with spaces"
    target.mkdir()
    alias = tmp_path / "junction"
    subprocess.run(
        ["cmd", "/d", "/c", "mklink", "/J", str(alias), str(target)],
        capture_output=True,
        check=True,
    )
    assert directory_identity(target) == directory_identity(alias)
    with pytest.raises(AppError, match="links"):
        prepare_data_dir(alias)
