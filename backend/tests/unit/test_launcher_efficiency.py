"""Launcher polling reuses transport and writes only observable changes."""

import json
from contextlib import nullcontext
from types import SimpleNamespace

import httpx

from agents_ide import launcher


def test_health_polling_reuses_client_and_persists_only_state_changes(settings, monkeypatch):
    requests, clients, saved, closed = [], [], [], []
    responses = iter([200, 200, 503, 503, 200])
    real_client = httpx.Client

    def respond(request):
        requests.append(request)
        return httpx.Response(next(responses))

    def client(**kwargs):
        instance = real_client(transport=httpx.MockTransport(respond), **kwargs)
        clients.append(instance)
        return instance

    connection = SimpleNamespace(execute=lambda *_: SimpleNamespace(scalar=lambda: 2.0))
    engine = SimpleNamespace(connect=lambda: nullcontext(connection), dispose=lambda: None)

    class Group:
        def start(self, *args):
            return SimpleNamespace(pid=123, create_time=lambda: 1.0, exe=lambda: "python")

        def close(self):
            closed.append(True)

    monkeypatch.setattr(launcher.portalocker, "Lock", lambda *a, **k: nullcontext())
    monkeypatch.setattr(launcher, "migrate", lambda *_: None)
    monkeypatch.setattr(launcher, "create_database", lambda *_: engine)
    monkeypatch.setattr(launcher, "ProcessGroup", Group)
    monkeypatch.setattr(launcher, "is_running", lambda *_: True)
    monkeypatch.setattr(launcher, "wait_stopped", lambda *_: True)
    monkeypatch.setattr(launcher, "stop_requested", lambda *_: len(requests) >= 5)
    monkeypatch.setattr(launcher.time, "sleep", lambda *_: None)
    monkeypatch.setattr(launcher.httpx, "Client", client)
    monkeypatch.setattr(
        launcher, "atomic_write", lambda path, data: saved.append((path.name, data))
    )

    launcher.run_launcher(settings)

    assert len(requests) == 5 and len(clients) == 1 and clients[0].is_closed
    assert [json.loads(data)["status"] for name, data in saved if name == "launcher.json"] == [
        "starting",
        "running",
        "starting",
        "running",
        "stopped",
    ]
    assert len(closed) == 2
