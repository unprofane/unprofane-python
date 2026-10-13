import json
import re
from pathlib import Path

import httpx
import pytest

from unprofane import AsyncUnprofane, Unprofane, UnprofaneError, __version__
from unprofane.client import backoff


def transport(handler):
    return httpx.MockTransport(handler)


def test_version_matches_pyproject():
    pyproject = (Path(__file__).parent.parent / "pyproject.toml").read_text()
    assert __version__ == re.search(r'^version = "([^"]+)"', pyproject, re.M).group(1)


def test_check_sends_the_key_and_body():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"allowed": False, "action": "block"})

    with Unprofane("up_test_abc", transport=transport(handler)) as client:
        r = client.check("xXsh1tXx", context="username", policy="teen")
    assert r["action"] == "block"
    assert seen["url"] == "https://api.unprofane.com/v1/check"
    assert seen["auth"] == "Bearer up_test_abc"
    assert seen["body"] == {"text": "xXsh1tXx", "context": "username", "policy": "teen"}


def test_refuses_a_foreign_key():
    with pytest.raises(TypeError):
        Unprofane("sk-nope")


def test_retries_then_raises_a_problem(monkeypatch):
    monkeypatch.setattr("unprofane.client.time.sleep", lambda s: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"title": "Too many requests.", "status": 429}, headers={"retry-after": "0"})
        if calls["n"] == 2:
            return httpx.Response(503, text="down")
        return httpx.Response(200, json={"id": "chk_1", "allowed": True})

    with Unprofane("up_live_k", transport=transport(handler)) as client:
        assert client.check("hi")["allowed"] is True
    assert calls["n"] == 3

    def bad(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"type": "https://unprofane.com/docs/errors/invalid-request", "title": "The request body is not valid.", "status": 400, "detail": "text: too long"}, headers={"request-id": "req_9"})

    with Unprofane("up_live_k", transport=transport(bad)) as client:
        with pytest.raises(UnprofaneError) as e:
            client.check("")
    assert e.value.status == 400
    assert "invalid-request" in e.value.problem["type"]
    assert e.value.request_id == "req_9"


def test_check_many_chunks_and_keeps_order():
    def handler(request: httpx.Request) -> httpx.Response:
        items = json.loads(request.content)["items"]
        return httpx.Response(200, json={"results": [{"id": i["id"], "result": {"allowed": i["text"] != "bad"}} for i in items], "counted": len(items)})

    texts = ["bad" if i == 137 else f"t{i}" for i in range(250)]
    with Unprofane("up_live_k", transport=transport(handler)) as client:
        results = client.check_many(texts, context="comment")
        with pytest.raises(ValueError):
            client.check_batch([{"id": str(i), "text": "x"} for i in range(101)])
    assert len(results) == 250
    assert results[137] == {"id": "137", "result": {"allowed": False}}
    assert results[249]["id"] == "249"


def test_backoff():
    assert backoff(1, "2") == 2.0
    assert all(backoff(5, None) <= 8.0 for _ in range(20))
    assert backoff(1, "soon") <= 0.5


async def test_async_client_and_lists():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            return httpx.Response(204)
        if request.url.path.endswith("/lists/allow"):
            return httpx.Response(200, json={"kind": "allow", "terms": ["Scunthorpe"], "version": 2})
        items = json.loads(request.content)["items"]
        return httpx.Response(200, json={"results": [{"id": i["id"], "result": {"allowed": True}} for i in items], "counted": len(items)})

    async with AsyncUnprofane("up_live_k", transport=httpx.MockTransport(handler)) as client:
        assert (await client.get_list("prj_1", "allow"))["terms"] == ["Scunthorpe"]
        assert await client.delete_list("prj_1", "allow") is None
        results = await client.check_many([f"t{i}" for i in range(150)], concurrency=3)
    assert [r["id"] for r in results] == [str(i) for i in range(150)]
