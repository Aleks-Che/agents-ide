"""Provider URL policy shared by connection settings, probes and adapters.

Loopback HTTP is allowed; remote providers must use HTTPS. Credentials,
query strings, fragments and whitespace are rejected before any request is
made so a key can never travel inside a URL.
"""

from __future__ import annotations

from urllib.parse import urlsplit, urlunsplit

from agents_ide.errors import AppError

_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def validate_provider_url(value: str) -> str:
    """Validate and return a normalized base URL without a trailing slash."""

    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        raise AppError("connection_url_invalid", "Неверный адрес подключения", 400) from None
    if (
        not parts.hostname
        or parts.query
        or parts.fragment
        or "\\" in value
        or any(char.isspace() for char in value)
        or (port is not None and port < 1)
    ):
        raise AppError(
            "connection_url_invalid", "URL должен содержать host без query/fragment", 400
        )
    if parts.scheme not in {"http", "https"}:
        raise AppError("connection_url_invalid", "Допустимы только http и https", 400)
    if (
        parts.username is not None
        or parts.password is not None
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise AppError("connection_url_invalid", "URL не должен содержать учётные данные", 400)
    if parts.scheme == "http" and (parts.hostname or "").lower() not in _LOOPBACK_HOSTS:
        raise AppError("connection_url_invalid", "Удалённый провайдер требует HTTPS", 400)
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def chat_completions_url(base_url: str) -> str:
    normalized = validate_provider_url(base_url)
    if normalized.endswith("/chat/completions"):
        return normalized
    return f"{normalized}/chat/completions"


def models_url(base_url: str) -> str:
    normalized = validate_provider_url(base_url)
    if normalized.endswith("/models"):
        return normalized
    return f"{normalized}/models"


def same_origin(left: str, right: str) -> bool:
    """Compare scheme/host/port without following any redirect by string prefix."""

    try:
        a, b = urlsplit(left), urlsplit(right)
    except ValueError:
        return False
    default_port = {"http": 80, "https": 443}
    return (
        a.scheme == b.scheme
        and (a.hostname or "").lower() == (b.hostname or "").lower()
        and (a.port or default_port.get(a.scheme)) == (b.port or default_port.get(b.scheme))
    )
