"""The sync and async clients: one method per endpoint, retries with jitter
on 429 and 5xx honouring Retry-After, a batch helper that chunks by 100.
No dependency beyond httpx. Responses are plain dicts in the API's own
field names (https://unprofane.com/docs/api/), so nothing here can drift
from the contract without the contract changing."""

from __future__ import annotations

import random
import time
from typing import Any, Dict, Iterable, List, Optional, Union

import httpx

__version__ = "0.1.1"
_DEFAULT_BASE = "https://api.unprofane.com"
_RETRYABLE = {429, 502, 503, 504}
_MAX_BATCH = 100

Policy = Union[str, Dict[str, str]]


class UnprofaneError(Exception):
    """A failure the API answered, with its problem details."""

    def __init__(self, message: str, status: int, problem: Optional[dict], request_id: Optional[str]):
        super().__init__(message)
        self.status = status
        self.problem = problem
        self.request_id = request_id


def backoff(attempt: int, retry_after: Optional[str]) -> float:
    """Exponential with full jitter, capped at eight seconds; Retry-After wins."""
    if retry_after is not None:
        try:
            value = float(retry_after)
            if value >= 0:
                return min(value, 30.0)
        except ValueError:
            pass
    return random.random() * min(8.0, 0.25 * (2**attempt))


def _check_key(api_key: str) -> None:
    if not api_key or not api_key.startswith(("up_live_", "up_test_", "up_pk_")):
        raise TypeError("pass an API key (up_live_…, up_test_… or up_pk_…)")


def _problem(res: httpx.Response) -> Optional[dict]:
    try:
        body = res.json()
    except ValueError:
        return None
    if isinstance(body, dict) and isinstance(body.get("title"), str):
        body.setdefault("status", res.status_code)
        return body
    return None


def _error(res: httpx.Response, method: str, path: str) -> UnprofaneError:
    problem = _problem(res)
    message = (problem or {}).get("detail") or (problem or {}).get("title") or f"Unprofane {method} {path} failed ({res.status_code})"
    return UnprofaneError(message, res.status_code, problem, res.headers.get("request-id"))


def _chunks(texts: List[str]) -> List[List[dict]]:
    return [
        [{"id": str(i + j), "text": text} for j, text in enumerate(texts[i : i + _MAX_BATCH])]
        for i in range(0, len(texts), _MAX_BATCH)
    ]


def _drop_none(body: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in body.items() if v is not None}


class _Base:
    def __init__(self, api_key: str, base_url: str = _DEFAULT_BASE, retries: int = 3, timeout: float = 10.0):
        _check_key(api_key)
        self._headers = {"authorization": f"Bearer {api_key}", "user-agent": f"unprofane-python/{__version__}"}
        self._base = base_url.rstrip("/")
        self._retries = retries
        self._timeout = timeout


class Unprofane(_Base):
    """The synchronous client. Use it as a context manager or call close()."""

    def __init__(self, api_key: str, base_url: str = _DEFAULT_BASE, retries: int = 3, timeout: float = 10.0, transport: Optional[httpx.BaseTransport] = None):
        super().__init__(api_key, base_url, retries, timeout)
        self._http = httpx.Client(base_url=self._base, headers=self._headers, timeout=timeout, transport=transport)

    def __enter__(self) -> "Unprofane":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _call(self, method: str, path: str, body: Optional[dict] = None) -> Any:
        attempt = 0
        while True:
            try:
                res = self._http.request(method, path, json=body)
            except httpx.TransportError:
                if attempt >= self._retries:
                    raise
                attempt += 1
                time.sleep(backoff(attempt, None))
                continue
            if res.is_success:
                return None if res.status_code == 204 else res.json()
            if res.status_code in _RETRYABLE and attempt < self._retries:
                attempt += 1
                time.sleep(backoff(attempt, res.headers.get("retry-after")))
                continue
            raise _error(res, method, path)

    def check(self, text: str, *, context: Optional[str] = None, policy: Optional[Policy] = None, mode: Optional[str] = None, language: Optional[str] = None, mask: Optional[str] = None, engine: Optional[str] = None, lists: Optional[dict] = None) -> dict:
        """One text: should it be allowed, with the evidence."""
        return self._call("POST", "/v1/check", _drop_none({"text": text, "context": context, "policy": policy, "mode": mode, "language": language, "mask": mask, "engine": engine, "lists": lists}))

    def check_batch(self, items: List[dict], *, context: Optional[str] = None, policy: Optional[Policy] = None, mode: Optional[str] = None, mask: Optional[str] = None, engine: Optional[str] = None, lists: Optional[dict] = None) -> dict:
        """Up to 100 items of {id, text}, answered in order."""
        if len(items) > _MAX_BATCH:
            raise ValueError(f"check_batch takes at most {_MAX_BATCH} items; use check_many")
        return self._call("POST", "/v1/check/batch", _drop_none({"items": items, "context": context, "policy": policy, "mode": mode, "mask": mask, "engine": engine, "lists": lists}))

    def check_many(self, texts: Iterable[str], **options: Any) -> List[dict]:
        """Any number of texts, chunked by 100, results in input order."""
        out: List[dict] = []
        for chunk in _chunks(list(texts)):
            out.extend(self.check_batch(chunk, **options)["results"])
        return out

    def feedback(self, check_id: str, verdict: str, *, expected_action: Optional[str] = None, note: Optional[str] = None, text: Optional[str] = None) -> dict:
        """Report a wrong result. `text` is stored only if you include it."""
        return self._call("POST", "/v1/feedback", _drop_none({"check_id": check_id, "verdict": verdict, "expected_action": expected_action, "note": note, "text": text}))

    def usage(self) -> dict:
        return self._call("GET", "/v1/usage")

    def get_list(self, project_id: str, kind: str) -> dict:
        return self._call("GET", f"/v1/projects/{project_id}/lists/{kind}")

    def put_list(self, project_id: str, kind: str, terms: List[str]) -> dict:
        return self._call("PUT", f"/v1/projects/{project_id}/lists/{kind}", {"terms": terms})

    def delete_list(self, project_id: str, kind: str) -> None:
        self._call("DELETE", f"/v1/projects/{project_id}/lists/{kind}")


class AsyncUnprofane(_Base):
    """The asynchronous client, for asyncio. Use it as an async context manager."""

    def __init__(self, api_key: str, base_url: str = _DEFAULT_BASE, retries: int = 3, timeout: float = 10.0, transport: Optional[httpx.AsyncBaseTransport] = None):
        super().__init__(api_key, base_url, retries, timeout)
        self._http = httpx.AsyncClient(base_url=self._base, headers=self._headers, timeout=timeout, transport=transport)

    async def __aenter__(self) -> "AsyncUnprofane":
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, path: str, body: Optional[dict] = None) -> Any:
        import asyncio

        attempt = 0
        while True:
            try:
                res = await self._http.request(method, path, json=body)
            except httpx.TransportError:
                if attempt >= self._retries:
                    raise
                attempt += 1
                await asyncio.sleep(backoff(attempt, None))
                continue
            if res.is_success:
                return None if res.status_code == 204 else res.json()
            if res.status_code in _RETRYABLE and attempt < self._retries:
                attempt += 1
                await asyncio.sleep(backoff(attempt, res.headers.get("retry-after")))
                continue
            raise _error(res, method, path)

    async def check(self, text: str, *, context: Optional[str] = None, policy: Optional[Policy] = None, mode: Optional[str] = None, language: Optional[str] = None, mask: Optional[str] = None, engine: Optional[str] = None, lists: Optional[dict] = None) -> dict:
        return await self._call("POST", "/v1/check", _drop_none({"text": text, "context": context, "policy": policy, "mode": mode, "language": language, "mask": mask, "engine": engine, "lists": lists}))

    async def check_batch(self, items: List[dict], *, context: Optional[str] = None, policy: Optional[Policy] = None, mode: Optional[str] = None, mask: Optional[str] = None, engine: Optional[str] = None, lists: Optional[dict] = None) -> dict:
        if len(items) > _MAX_BATCH:
            raise ValueError(f"check_batch takes at most {_MAX_BATCH} items; use check_many")
        return await self._call("POST", "/v1/check/batch", _drop_none({"items": items, "context": context, "policy": policy, "mode": mode, "mask": mask, "engine": engine, "lists": lists}))

    async def check_many(self, texts: Iterable[str], *, concurrency: int = 2, **options: Any) -> List[dict]:
        import asyncio

        chunks = _chunks(list(texts))
        out: List[Optional[dict]] = [None] * len(chunks)
        sem = asyncio.Semaphore(max(1, concurrency))

        async def one(i: int) -> None:
            async with sem:
                out[i] = await self.check_batch(chunks[i], **options)

        await asyncio.gather(*(one(i) for i in range(len(chunks))))
        return [r for batch in out for r in (batch or {}).get("results", [])]

    async def feedback(self, check_id: str, verdict: str, *, expected_action: Optional[str] = None, note: Optional[str] = None, text: Optional[str] = None) -> dict:
        return await self._call("POST", "/v1/feedback", _drop_none({"check_id": check_id, "verdict": verdict, "expected_action": expected_action, "note": note, "text": text}))

    async def usage(self) -> dict:
        return await self._call("GET", "/v1/usage")

    async def get_list(self, project_id: str, kind: str) -> dict:
        return await self._call("GET", f"/v1/projects/{project_id}/lists/{kind}")

    async def put_list(self, project_id: str, kind: str, terms: List[str]) -> dict:
        return await self._call("PUT", f"/v1/projects/{project_id}/lists/{kind}", {"terms": terms})

    async def delete_list(self, project_id: str, kind: str) -> None:
        await self._call("DELETE", f"/v1/projects/{project_id}/lists/{kind}")
