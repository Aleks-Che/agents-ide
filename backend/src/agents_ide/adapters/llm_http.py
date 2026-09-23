"""OpenAI-compatible HTTP adapter for ordinary LLM connections.

The adapter is deliberately synchronous: the engine dispatches it from a
worker thread. It never follows redirects, never reads environment proxies
and never lets a credential travel to a different origin. Normal requests have
no application time/size cap; every failure is translated into the shared
:class:`ExternalOutcome` taxonomy so the runner applies one policy.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from agents_ide.adapters.base import (
    AdapterError,
    ExternalOutcome,
    LLMAdapter,
    LLMAdapterRequest,
    LLMResult,
)
from agents_ide.adapters.provider_limits import limit_error_code
from agents_ide.engine.artifacts import encode
from agents_ide.errors import AppError
from agents_ide.security.provider_url import chat_completions_url, models_url

MAX_ERROR_BYTES = 64 * 1024
CONNECT_TIMEOUT_SECONDS = 10.0
MODELS_TIMEOUT_SECONDS = 30.0
PROBE_MAX_TOKENS = 1024

_GENERATION_KEYS = (
    "reasoning_effort",
    "thinking",
    "reasoning_split",
    "temperature",
    "top_p",
    "max_tokens",
    "seed",
    "stop",
    "frequency_penalty",
    "presence_penalty",
)


@dataclass(frozen=True)
class ProviderProbe:
    ok: bool
    status: int | None
    models: tuple[str, ...]
    detail: str
    elapsed_seconds: float
    tested_model: str | None = None
    catalog_available: bool = False


def _connection_dict(connection: dict[str, Any] | None) -> dict[str, Any]:
    return connection if isinstance(connection, dict) else {}


def _error(
    code: str,
    message: str,
    *,
    outcome: ExternalOutcome,
    retry_safety: str = "unknown",
    no_effect: bool = False,
    details: dict[str, Any] | None = None,
) -> tuple[ExternalOutcome, AdapterError, bool]:
    return outcome, AdapterError(code, message, retry_safety, details or {}), no_effect


def _headers(connection: dict[str, Any]) -> dict[str, str]:
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    secret = connection.get("secret_value")
    if isinstance(secret, str) and secret:
        from agents_ide.logging import register_secret

        register_secret(secret)
        headers["Authorization"] = f"Bearer {secret}"
    return headers


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("retry-after")
    if not raw:
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        try:
            seconds = parsedate_to_datetime(raw).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(seconds, 0.0) if math.isfinite(seconds) else None


def _provider_message(payload: Any, fallback: str) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str) and message:
                return message[:1024]
        if isinstance(error, str) and error:
            return error[:1024]
    return fallback[:1024]


def _classify_http_error(
    response: httpx.Response, payload: Any
) -> tuple[ExternalOutcome, AdapterError, bool]:
    message = _provider_message(payload, f"HTTP {response.status_code}")
    lowered = message.lower()
    limit_code = limit_error_code(
        payload.get("error", {}) if isinstance(payload, dict) else {}, response.status_code
    )
    if limit_code:
        details: dict[str, Any] = {"status": response.status_code}
        retry_after = _retry_after(response)
        if limit_code == "provider_rate_limited" and retry_after is not None:
            details["retry_after_seconds"] = retry_after
        return _error(
            limit_code,
            message,
            outcome=ExternalOutcome.UNAVAILABLE
            if limit_code == "provider_quota_exhausted"
            else ExternalOutcome.RETRYABLE_FAILURE,
            retry_safety="safe",
            no_effect=True,
            details=details,
        )
    if response.status_code in {401, 403}:
        return _error(
            "provider_unauthorized",
            message,
            outcome=ExternalOutcome.PERMISSION_DENIED,
            retry_safety="unsafe",
        )
    if response.status_code >= 500 or response.status_code in {408, 409, 425}:
        return _error(
            "provider_unavailable",
            message,
            outcome=ExternalOutcome.UNKNOWN,
            details={"status": response.status_code},
        )
    if response.is_redirect:
        return _error(
            "redirect_refused",
            "Редирект провайдера не поддерживается",
            outcome=ExternalOutcome.CONFIRMED_FAILURE,
            retry_safety="safe",
            no_effect=True,
            details={"status": response.status_code},
        )
    if response.status_code == 404:
        return _error(
            "model_not_found",
            message,
            outcome=ExternalOutcome.UNAVAILABLE,
            retry_safety="safe",
            no_effect=True,
            details={"status": 404},
        )
    if "context length" in lowered or "maximum context" in lowered:
        return _error(
            "context_length_exceeded",
            message,
            outcome=ExternalOutcome.UNAVAILABLE,
            retry_safety="safe",
            no_effect=True,
            details={"status": response.status_code},
        )
    if "does not exist" in lowered or "not found" in lowered:
        return _error(
            "model_not_found",
            message,
            outcome=ExternalOutcome.UNAVAILABLE,
            retry_safety="safe",
            no_effect=True,
            details={"status": response.status_code},
        )
    return _error(
        "configuration_invalid",
        message,
        outcome=ExternalOutcome.CONFIRMED_FAILURE,
        retry_safety="safe",
        no_effect=True,
        details={"status": response.status_code},
    )


def _request_body(request: LLMAdapterRequest) -> dict[str, Any]:
    params = request.params if isinstance(request.params, dict) else {}
    body: dict[str, Any] = {"model": request.model_id}
    for key in _GENERATION_KEYS:
        if key in params:
            body[key if key != "max_output_tokens" else "max_tokens"] = params[key]
    if "max_output_tokens" in params and "max_tokens" not in body:
        body["max_tokens"] = params["max_output_tokens"]
    if request.response_format == "json" and params.get("structured_output") is True:
        body["response_format"] = {"type": "json_object"}
    if params.get("stream") is True:
        body["stream"] = True
    prompt = request.prompt
    if request.response_format == "json" and "response_format" not in body:
        prompt += "\n\nОтвет предоставь строго одним JSON-объектом без пояснений."
    evidence = (request.context_package or {}).get("evidence")
    if evidence is not None:
        try:
            rendered = encode(evidence)
        except (ValueError, TypeError):
            rendered = "{}"
        prompt += f"\n\nКонтекст и доказательства (JSON):\n{rendered}"
    body["messages"] = [{"role": "user", "content": prompt}]
    return body


def _extract_content(payload: dict[str, Any]) -> str:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("empty choices")
    first = choices[0]
    if not isinstance(first, dict):
        raise ValueError("invalid choice")
    message = first.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                item.get("text", "")
                for item in content
                if isinstance(item, dict) and isinstance(item.get("text"), str)
            ]
            return "".join(parts)
    text = first.get("text")
    if isinstance(text, str):
        return text
    raise ValueError("missing content")


def _usage(payload: dict[str, Any]) -> int | None:
    usage = payload.get("usage")
    if not isinstance(usage, dict):
        return None
    total = usage.get("total_tokens")
    if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
        return total
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    if (
        isinstance(prompt_tokens, int)
        and isinstance(completion_tokens, int)
        and prompt_tokens >= 0
        and completion_tokens >= 0
    ):
        return prompt_tokens + completion_tokens
    return None


class _ProviderLimitError(ValueError):
    def __init__(self, error: dict[str, Any]):
        self.error = error
        super().__init__("provider_limit")


def _parse_stream(text: str) -> tuple[str, int | None]:
    chunks: list[str] = []
    tokens: int | None = None
    complete = False
    for line in text.splitlines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            complete = True
            break
        if not data:
            continue
        try:
            payload = json.loads(data)
        except ValueError:
            raise ValueError("invalid_stream_json") from None
        if not isinstance(payload, dict):
            raise ValueError("invalid_stream_payload")
        if payload.get("error"):
            if limit_error_code(payload["error"]):
                raise _ProviderLimitError(payload["error"])
            raise ValueError("stream_error")
        delta = None
        choices = payload.get("choices")
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            if choices[0].get("finish_reason") in {"length", "content_filter"}:
                raise ValueError("incomplete_response")
            delta = choices[0].get("delta")
        if isinstance(delta, dict) and isinstance(delta.get("content"), str):
            chunks.append(delta["content"])
        usage = _usage(payload)
        if usage is not None:
            tokens = usage
    if not complete or not chunks:
        raise ValueError("incomplete_stream")
    return "".join(chunks), tokens


class HttpLLMAdapter(LLMAdapter):
    name = "openai_compatible"

    def __init__(
        self,
        *,
        timeout_seconds: float | None = None,
        max_response_bytes: int | None = None,
    ) -> None:
        self.timeout_seconds = max(1.0, timeout_seconds) if timeout_seconds is not None else None
        self.max_response_bytes = max_response_bytes
        self._diagnostic = False

    def run(self, request: LLMAdapterRequest) -> LLMResult:
        started = time.monotonic()
        connection = _connection_dict(request.connection)
        diagnostic_text = ""
        dispatched = False

        def failure(outcome: ExternalOutcome, error: AdapterError, no_effect: bool) -> LLMResult:
            return LLMResult(
                outcome,
                diagnostic_text,
                None,
                None,
                error=error,
                elapsed_seconds=time.monotonic() - started,
                finished_at=datetime.now(tz=UTC),
                no_effect=no_effect,
                can_fallback=dispatched
                and error.code != "interrupted"
                and not (request.stop_event and request.stop_event.is_set()),
            )

        try:
            url = chat_completions_url(str(connection.get("base_url", "")))
        except (AppError, ValueError):
            return failure(
                *_error(
                    "configuration_invalid",
                    "Подключение провайдера не настроено",
                    outcome=ExternalOutcome.CONFIRMED_FAILURE,
                    retry_safety="safe",
                    no_effect=True,
                )
            )

        timeout = self.timeout_seconds
        if request.deadline_at is not None:
            timeout = min(timeout or float("inf"), max(0.0, request.deadline_at - time.time()))
        if (request.stop_event and request.stop_event.is_set()) or (
            timeout is not None and timeout <= 0
        ):
            return failure(
                *_error(
                    "interrupted",
                    "Request stopped before dispatch",
                    outcome=ExternalOutcome.RETRYABLE_FAILURE,
                    retry_safety="safe",
                    no_effect=True,
                )
            )
        try:
            if request.check_owned:
                request.check_owned()
            body = _request_body(request)
        except ValueError:
            return failure(
                *_error(
                    "configuration_invalid",
                    "Request exceeds the configured size",
                    outcome=ExternalOutcome.CONFIRMED_FAILURE,
                    retry_safety="safe",
                    no_effect=True,
                )
            )
        try:
            dispatched = True
            response, body_bytes = asyncio.run(self._receive(request, url, body, timeout))
            if response.status_code >= 300:
                return failure(*_classify_http_error(response, self._json_or_none(body_bytes)))
            text = body_bytes.decode("utf-8", errors="strict")
            diagnostic_text = text[:MAX_ERROR_BYTES]
        except _ResponseLimit:
            return failure(
                *_error(
                    "response_too_large",
                    "Provider response exceeds its byte limit",
                    outcome=ExternalOutcome.INVALID_FORMAT,
                    retry_safety="unsafe",
                )
            )
        except _Interrupted:
            return failure(
                *_error(
                    "interrupted",
                    "Request stopped after dispatch",
                    outcome=ExternalOutcome.UNKNOWN,
                )
            )
        except TimeoutError:
            return failure(
                *_error(
                    "provider_response_unknown",
                    "Request interrupted after dispatch",
                    outcome=ExternalOutcome.UNKNOWN,
                )
            )
        except httpx.ConnectError as exc:
            return failure(
                *_error(
                    "provider_connect_failed",
                    f"Не удалось подключиться к провайдеру ({type(exc).__name__})",
                    outcome=ExternalOutcome.RETRYABLE_FAILURE,
                    retry_safety="safe",
                    no_effect=True,
                )
            )
        except (httpx.ConnectTimeout, httpx.PoolTimeout):
            return failure(
                *_error(
                    "provider_connect_timeout",
                    "Провайдер не ответил на соединение",
                    outcome=ExternalOutcome.RETRYABLE_FAILURE,
                    retry_safety="safe",
                    no_effect=True,
                )
            )
        except (httpx.ReadTimeout, httpx.WriteTimeout):
            return failure(
                *_error(
                    "provider_read_timeout",
                    "Ответ провайдера не получен до истечения времени",
                    outcome=ExternalOutcome.UNKNOWN,
                    retry_safety="unknown",
                )
            )
        except httpx.TransportError as exc:
            return failure(
                *_error(
                    "provider_transport_error",
                    f"Транспортный сбой провайдера ({type(exc).__name__})",
                    outcome=ExternalOutcome.UNKNOWN,
                    retry_safety="unknown",
                )
            )
        except (OSError, ValueError):
            return failure(
                *_error(
                    "provider_transport_error",
                    "Транспортный сбой провайдера",
                    outcome=ExternalOutcome.UNKNOWN,
                    retry_safety="unknown",
                )
            )

        tokens: int | None = None
        if body.get("stream") is True:
            try:
                content, tokens = _parse_stream(text)
            except _ProviderLimitError as exc:
                return failure(*_classify_http_error(response, {"error": exc.error}))
            except ValueError as exc:
                return failure(
                    *_error(
                        "invalid_provider_stream",
                        str(exc),
                        outcome=ExternalOutcome.INVALID_FORMAT,
                        retry_safety="unsafe",
                    )
                )
        else:
            payload = self._json_or_none(text.encode("utf-8"))
            if not isinstance(payload, dict):
                return failure(
                    *_error(
                        "invalid_provider_json",
                        "Провайдер вернул не JSON",
                        outcome=ExternalOutcome.INVALID_FORMAT,
                        retry_safety="unsafe",
                    )
                )
            if isinstance(payload.get("error"), (dict, str)):
                if limit_error_code(payload["error"]):
                    # A structured rejection can be wrapped in HTTP 200.
                    return failure(*_classify_http_error(response, payload))
                return failure(
                    *_error(
                        "provider_result_unknown",
                        "Provider returned an error after accepting the request",
                        outcome=ExternalOutcome.UNKNOWN,
                    )
                )
            try:
                choices = payload.get("choices")
                first = choices[0] if isinstance(choices, list) and choices else None
                # Adapted terminal signals from sources/claudexor raw-api fixtures:
                # an in-choice error takes precedence over partial assistant text.
                if isinstance(first, dict) and (
                    first.get("error") or first.get("finish_reason") == "error"
                ):
                    if limit_error_code(first.get("error")):
                        return failure(*_classify_http_error(response, first))
                    return failure(
                        *_error(
                            "provider_result_unknown",
                            "Provider completion failed",
                            outcome=ExternalOutcome.UNKNOWN,
                        )
                    )
                finish_reason = first.get("finish_reason") if isinstance(first, dict) else None
                content = _extract_content(payload)
                if not content.strip():
                    raise ValueError("empty_response")
                if finish_reason == "content_filter" or (
                    finish_reason == "length" and not self._diagnostic
                ):
                    raise ValueError("incomplete_response")
            except ValueError:
                return failure(
                    *_error(
                        "invalid_provider_response",
                        (
                            "Ответ модели обрезан лимитом токенов до завершения генерации"
                            if finish_reason == "length"
                            else "Ответ модели заблокирован фильтром провайдера"
                            if finish_reason == "content_filter"
                            else "В ответе провайдера нет текстового содержимого"
                        ),
                        outcome=ExternalOutcome.INVALID_FORMAT,
                        retry_safety="unsafe",
                    )
                )
            tokens = _usage(payload)
        return LLMResult(
            ExternalOutcome.SUCCEEDED,
            content,
            None,
            None,
            tokens_used=tokens,
            budget_quality="observed" if tokens is not None else "unknown",
            elapsed_seconds=time.monotonic() - started,
            finished_at=datetime.now(tz=UTC),
        )

    async def _receive(
        self,
        request: LLMAdapterRequest,
        url: str,
        body: dict[str, Any] | None,
        timeout: float | None,
        *,
        method: str = "POST",
    ) -> tuple[httpx.Response, bytes]:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout),
            trust_env=False,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=0),
        ) as client:

            async def receive() -> tuple[httpx.Response, bytes]:
                async with client.stream(
                    method, url, json=body, headers=_headers(_connection_dict(request.connection))
                ) as response:
                    cap = (
                        min(MAX_ERROR_BYTES, self.max_response_bytes or MAX_ERROR_BYTES)
                        if response.status_code >= 300
                        else self.max_response_bytes
                    )
                    raw = bytearray()
                    pending = b""
                    async for chunk in response.aiter_bytes():
                        if cap is not None and len(raw) + len(chunk) > cap:
                            raise _ResponseLimit
                        raw.extend(chunk)
                        if (
                            body
                            and body.get("stream")
                            and response.status_code < 300
                            and request.emit_event
                        ):
                            pending += chunk
                            while b"\n" in pending:
                                line, pending = pending.split(b"\n", 1)
                                if line.startswith(b"data:"):
                                    data = self._json_or_none(line[5:].strip())
                                    choices = (
                                        data.get("choices") if isinstance(data, dict) else None
                                    )
                                    if (
                                        isinstance(choices, list)
                                        and choices
                                        and isinstance(choices[0], dict)
                                    ):
                                        delta_body = choices[0].get("delta")
                                        delta = (
                                            delta_body.get("content")
                                            if isinstance(delta_body, dict)
                                            else None
                                        )
                                        if isinstance(delta, str):
                                            for index in range(0, len(delta), 2000):
                                                request.emit_event(
                                                    "attempt.text_delta",
                                                    {"text": delta[index : index + 2000]},
                                                )
                    return response, bytes(raw)

            task = asyncio.create_task(receive())
            deadline = time.monotonic() + timeout if timeout is not None else float("inf")
            try:
                while True:
                    if request.stop_event and request.stop_event.is_set():
                        raise _Interrupted
                    if request.check_owned:
                        request.check_owned()
                    if time.monotonic() >= deadline:
                        raise TimeoutError
                    done, _ = await asyncio.wait({task}, timeout=0.05)
                    if done:
                        return task.result()
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    @staticmethod
    def _json_or_none(raw: bytes) -> Any:
        try:
            return json.loads(raw.decode("utf-8", errors="replace"))
        except (ValueError, UnicodeError, RecursionError):
            return None


class _ResponseLimit(Exception):
    pass


class _Interrupted(Exception):
    pass


def probe_connection(
    connection: dict[str, Any],
    *,
    model_id: str | None = None,
    timeout_seconds: float = MODELS_TIMEOUT_SECONDS,
    probe_chat: bool = True,
) -> ProviderProbe:
    """Bound both explicit diagnostic requests; HTTP 200 alone is not access."""
    started = time.monotonic()
    models: tuple[str, ...] = ()
    catalog_available = False
    adapter = HttpLLMAdapter(timeout_seconds=timeout_seconds)
    diagnostic = LLMAdapterRequest(
        role="diagnostic",
        model_id="",
        prompt="ping",
        context_package={},
        params={},
        connection=connection,
    )
    try:
        response, raw = asyncio.run(
            adapter._receive(
                diagnostic,
                models_url(str(connection.get("base_url", ""))),
                None,
                timeout_seconds,
                method="GET",
            )
        )
        payload = adapter._json_or_none(raw)
        if response.status_code == 200 and isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, list):
                catalog_available = True
                models = tuple(
                    item["id"]
                    for item in data
                    if isinstance(item, dict) and isinstance(item.get("id"), str)
                )
    except (
        httpx.HTTPError,
        OSError,
        ValueError,
        AppError,
        _ResponseLimit,
        TimeoutError,
        _Interrupted,
    ):
        pass
    if not probe_chat:
        return ProviderProbe(
            bool(models),
            None,
            models,
            "Catalog available" if models else "Catalog unavailable",
            time.monotonic() - started,
            catalog_available=catalog_available,
        )
    adapter._diagnostic = True
    tested_model = model_id or (models[0] if models else "default")
    result = adapter.run(
        LLMAdapterRequest(
            role="diagnostic",
            model_id=tested_model,
            prompt="Reply with exactly OK and nothing else.",
            context_package={},
            # Reasoning models may spend the initial tokens before producing text.
            params={"max_tokens": PROBE_MAX_TOKENS},
            connection=connection,
        )
    )
    detail = "Проверочный запрос выполнен"
    if result.outcome != ExternalOutcome.SUCCEEDED:
        detail = "Проверочный запрос не выполнен: " + (
            result.error.code if result.error else result.outcome.value
        )
        if result.error and result.error.code == "invalid_provider_response":
            detail += f". {result.error.message}"
    return ProviderProbe(
        result.outcome == ExternalOutcome.SUCCEEDED,
        None,
        models,
        detail,
        time.monotonic() - started,
        tested_model=tested_model,
        catalog_available=catalog_available,
    )
