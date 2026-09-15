"""Message history pagination used by the stage 9 UI."""

from agents_ide.persistence.models import Message


def test_latest_history_pages_keep_new_messages_and_stable_order(authenticated, tmp_path):
    client, headers = authenticated
    workspace = tmp_path / "messages"
    workspace.mkdir()
    project = client.post(
        "/api/projects", json={"name": "History", "workspace_path": str(workspace)}, headers=headers
    ).json()
    chat = client.post(
        f"/api/projects/{project['id']}/chats", json={"title": "History"}, headers=headers
    ).json()
    # Equal timestamps exercise the ID tie-breaker across page boundaries.
    with client.app.state.session_factory() as session:
        session.add_all(
            Message(
                id=f"message-{index:04d}",
                chat_id=chat["id"],
                role="user",
                content=str(index),
                version=1,
                created_at=1000,
                archived_at=None,
            )
            for index in range(205)
        )
        session.commit()
    url = f"/api/chats/{chat['id']}/messages"
    legacy = client.get(url, params={"limit": 2}).json()
    assert [message["content"] for message in legacy] == ["0", "1"]
    latest = client.get(url, params={"latest": True, "limit": 200}).json()
    assert [message["content"] for message in latest] == [str(i) for i in range(5, 205)]
    # Archived cursors remain valid, and concurrent inserts do not shift older pages.
    client.post(
        f"/api/messages/{latest[0]['id']}/archive",
        json={"expected_version": 1},
        headers=headers,
    ).raise_for_status()
    added = client.post(url, json={"content": "new", "role": "user"}, headers=headers).json()
    older = client.get(url, params={"latest": True, "before_id": latest[0]["id"]}).json()
    assert [message["content"] for message in older] == [str(i) for i in range(5)]
    assert client.get(url, params={"latest": True}).json()[-1]["id"] == added["id"]
    other = client.post(
        f"/api/projects/{project['id']}/chats", json={"title": "Other"}, headers=headers
    ).json()
    invalid = client.get(f"/api/chats/{other['id']}/messages", params={"before_id": added["id"]})
    assert invalid.status_code == 400
    assert invalid.json()["code"] == "message_cursor_invalid"
