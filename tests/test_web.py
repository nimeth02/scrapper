"""The web UI serves its page, reads session state from the file alone, validates input, and proxies
only Instagram CDN images. Nothing here launches a browser."""

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app.web import server


@pytest.fixture
def client():
    with TestClient(server.app) as c:
        yield c


def test_index_serves_the_page(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert "Hashtag Scout" in response.text
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/styles.css").status_code == 200
    assert client.get("/static/graph.js").status_code == 200
    assert 'id="viewMap"' in response.text


def test_status_without_session_file(client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(server.settings, "session_file", tmp_path / "missing.json")
    body = client.get("/api/status").json()
    assert body["logged_in"] is False
    assert body["session_file_exists"] is False
    assert "login" in body["message"]


def test_status_reads_cookies_from_the_session_file(client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    path.write_text(
        json.dumps(
            {
                "cookies": [
                    {"name": "sessionid", "value": "abc", "domain": ".instagram.com"},
                    {"name": "ds_user_id", "value": "42", "domain": ".instagram.com"},
                    {"name": "ds_user_id", "value": "other-site", "domain": ".example.com"},
                ]
            }
        )
    )
    monkeypatch.setattr(server.settings, "session_file", path)
    body = client.get("/api/status").json()
    assert body["logged_in"] is True
    assert body["user_id"] == "42"
    assert body["message"] is None


def test_scrape_rejects_empty_tags(client: TestClient) -> None:
    response = client.get("/api/scrape?tags=%20,%20")
    assert response.status_code == 400
    assert "hashtag" in response.json()["error"]


def test_scrape_needs_a_session_file(client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(server.settings, "session_file", tmp_path / "missing.json")
    response = client.get("/api/scrape?tags=tea")
    assert response.status_code == 409


def test_image_proxy_only_accepts_instagram_cdn(client: TestClient) -> None:
    assert client.get("/api/image?url=https://example.com/x.jpg").status_code == 403
    assert client.get("/api/image?url=http://scontent.cdninstagram.com/x.jpg").status_code == 403
    assert client.get("/api/image").status_code == 403


def test_clamp_and_cookie_helpers(tmp_path: Path) -> None:
    assert server._clamp(None, 60, 1, 500) == 60
    assert server._clamp("abc", 60, 1, 500) == 60
    assert server._clamp("9999", 60, 1, 500) == 500
    assert server._clamp("0", 5, 0, 30) == 0
    assert server.session_cookies(tmp_path / "nope.json") == {}
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    assert server.session_cookies(broken) == {}


def test_free_port_skips_a_busy_one() -> None:
    import socket

    from app.web.__main__ import free_port

    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        taken = busy.getsockname()[1]
        chosen = free_port("127.0.0.1", taken, attempts=5)
    assert chosen != taken
    assert taken < chosen < taken + 5


def test_engagement_rejects_bad_input_and_needs_a_session(client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert client.get("/api/engagement").status_code == 400
    assert client.get("/api/engagement?posts=%20,%20").status_code == 400
    monkeypatch.setattr(server.settings, "session_file", tmp_path / "missing.json")
    assert client.get("/api/engagement?posts=CxYz1Ab").status_code == 409


def test_parse_posts_accepts_urls_and_dedupes() -> None:
    codes = server.parse_posts("CxYz1Ab, https://www.instagram.com/p/DEF456/ CxYz1Ab https://www.instagram.com/reel/GHI789/?x=1")
    assert codes == ["CxYz1Ab", "DEF456", "GHI789"]
    assert server.parse_posts("") == []
    assert len(server.parse_posts(" ".join(f"C{i:07d}" for i in range(150)))) == server.MAX_ENGAGEMENT_POSTS


def test_status_includes_the_agent(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    info = {"configured": False, "models": [], "message": "no key"}
    monkeypatch.setattr(server.agent, "describe", lambda: info)
    assert client.get("/api/status").json()["agent"] == info


def test_agent_validates_input_session_and_llm(client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert client.post("/api/agent", content="nope", headers={"content-type": "application/json"}).status_code == 400
    assert client.post("/api/agent", json=["a list"]).status_code == 400
    assert client.post("/api/agent", json={"question": "  "}).status_code == 400
    assert client.post("/api/agent", json={"question": "x", "history": "bad"}).status_code == 400
    monkeypatch.setattr(server.settings, "session_file", tmp_path / "missing.json")
    assert client.post("/api/agent", json={"question": "who posts under #tea?"}).status_code == 409
    session = tmp_path / "session.json"
    session.write_text("{}")
    monkeypatch.setattr(server.settings, "session_file", session)
    monkeypatch.setattr(server.agent, "describe", lambda: {"configured": False, "models": [], "message": "No LLM provider configured"})
    response = client.post("/api/agent", json={"question": "who posts under #tea?"})
    assert response.status_code == 503 and "LLM" in response.json()["error"]


def test_agent_streams_its_events(client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    session = tmp_path / "session.json"
    session.write_text("{}")
    monkeypatch.setattr(server.settings, "session_file", session)
    monkeypatch.setattr(server.agent, "describe", lambda: {"configured": True, "models": ["gemini:x"], "message": None})

    async def fake_run(question, *, acquire, history, on_event):  # noqa: ANN001
        assert acquire == server.shared.acquire  # bound methods compare equal, not identical
        on_event({"type": "run_start", "run_id": "t1", "parent_id": "r0", "run_type": "tool", "name": "search_hashtags", "inputs": {"tags": ["tea"]}})
        on_event({"type": "run_end", "run_id": "t1", "parent_id": "r0", "run_type": "tool", "name": "search_hashtags", "summary": "#tea: 2 posts, 2 accounts", "error": None, "result": {"tags": []}})
        on_event({"type": "final", "text": f"{question} -> @a ({len(history)} earlier turns)", "model": "gemini:x", "steps": 1, "tool_calls": 1, "tokens": 10, "stopped": None})

    monkeypatch.setattr(server.agent, "run_agent", fake_run)
    response = client.post("/api/agent", json={"question": "who posts under #tea?", "history": [{"role": "user", "content": "hi"}]})
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/event-stream")
    events = [block.split("\n")[0] for block in response.text.strip().split("\n\n")]
    assert events == ["event: run_start", "event: run_end", "event: final"]
    assert "(1 earlier turns)" in response.text


class FakeShared:
    """Stands in for the shared browser so no Chromium starts."""

    is_started = False

    def acquire(self):  # noqa: ANN201
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def borrowed():
            yield object()

        return borrowed()

    async def close(self) -> None:
        pass


def test_profile_endpoint(client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from app.instagram import profiles
    from app.instagram.api import InstagramAPIError, LoginRequired, RateLimited

    assert client.get("/api/profile").status_code == 400
    assert client.get("/api/profile?username=not%20a%20name!").status_code == 400
    monkeypatch.setattr(server.settings, "session_file", tmp_path / "missing.json")
    assert client.get("/api/profile?username=tea").status_code == 409

    session = tmp_path / "session.json"
    session.write_text("{}")
    monkeypatch.setattr(server.settings, "session_file", session)
    monkeypatch.setattr(server, "shared", FakeShared())
    outcome: dict = {}

    async def fake_profile(session, username):  # noqa: ANN001
        if "exc" in outcome:
            raise outcome["exc"]
        return {"username": username, "full_name": "Dilmah Tea", "followers": 35684}

    monkeypatch.setattr(profiles, "get_profile", fake_profile)
    response = client.get("/api/profile?username=https://www.instagram.com/dilmahceylontea/")
    assert response.status_code == 200 and response.json() == {"username": "dilmahceylontea", "full_name": "Dilmah Tea", "followers": 35684}
    for exc, status in [
        (profiles.ProfileNotFound("@x does not exist"), 404),
        (LoginRequired("session expired"), 401),
        (RateLimited("HTTP 429"), 429),
        (InstagramAPIError("page changed"), 502),
    ]:
        outcome["exc"] = exc
        response = client.get("/api/profile?username=x_y")
        assert response.status_code == status and str(exc) in response.json()["error"]


def test_map_validates_input_session_and_llm(client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert client.post("/api/map", content="nope").status_code == 400
    assert client.post("/api/map", json={"seeds": ""}).status_code == 400
    assert client.post("/api/map", json={"seeds": "a b c d"}).json()["error"] == "at most 3 seed hashtags"
    monkeypatch.setattr(server.settings, "session_file", tmp_path / "missing.json")
    assert client.post("/api/map", json={"seeds": "concertslk"}).status_code == 409
    session = tmp_path / "session.json"
    session.write_text("{}")
    monkeypatch.setattr(server.settings, "session_file", session)
    monkeypatch.setattr(server.agent, "describe", lambda: {"configured": False, "models": [], "message": "No LLM provider configured"})
    assert client.post("/api/map", json={"seeds": "concertslk"}).status_code == 503


def test_map_streams_the_agent_and_the_graph(client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    session = tmp_path / "session.json"
    session.write_text("{}")
    monkeypatch.setattr(server.settings, "session_file", session)
    monkeypatch.setattr(server.agent, "describe", lambda: {"configured": True, "models": ["gemini:x"], "message": None})
    seen: dict = {}

    async def fake_run(seeds, *, acquire, store, on_event, **limits):  # noqa: ANN001
        seen.update(seeds=seeds, store=store, **limits)
        on_event({"type": "graph", "id": "concertslk", "nodes": [], "edges": []})
        on_event({"type": "final", "text": "mapped", "model": "gemini:x", "steps": 1, "tool_calls": 1, "tokens": 5, "stopped": None})

    monkeypatch.setattr(server.mapper, "run_mapper", fake_run)
    response = client.post("/api/map", json={"seeds": "#ConcertSLK", "max_nodes": 500, "max_searches": "4", "posts_per_search": 1})
    assert response.status_code == 200 and response.headers["content-type"].startswith("text/event-stream")
    assert [block.split("\n")[0] for block in response.text.strip().split("\n\n")] == ["event: graph", "event: final"]
    assert seen == {"seeds": ["concertslk"], "store": server.graph_store, "max_nodes": 100, "max_searches": 4, "posts_per_search": 12}


def test_stored_maps_can_be_listed_read_and_deleted(client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import asyncio

    from app.graph import GraphStore, HashtagGraph

    store = GraphStore(tmp_path / "graph")
    monkeypatch.setattr(server, "graph_store", store)
    assert client.get("/api/maps").json()["maps"] == []
    asyncio.run(store.save(HashtagGraph(["concertslk"], max_nodes=50).snapshot()))
    listed = client.get("/api/maps").json()
    assert listed["backend"].startswith("LadybugDB") and [m["id"] for m in listed["maps"]] == ["concertslk"]
    body = client.get("/api/maps/concertslk").json()
    assert body["seeds"] == ["concertslk"] and body["nodes"][0]["tag"] == "concertslk" and body["edges"] == []
    assert client.get("/api/maps/nothing").status_code == 404
    assert client.delete("/api/maps/concertslk").json() == {"deleted": "concertslk"}
    assert client.delete("/api/maps/concertslk").status_code == 404
    assert client.get("/api/status").json()["graph"]["path"] == str(tmp_path / "graph")
