"""Small JSON HTTP client with retries. Every API call goes through here."""
from __future__ import annotations

import math
import time
from typing import Any, Callable

import httpx

USER_AGENT = "weatherbot/0.1 (polymarket paper-trading research)"
_RETRY_STATUS = {429, 500, 502, 503, 504}
_MAX_RETRY_WAIT = 60.0  # a run holds its lock while it sleeps


class HttpError(Exception):
    def __init__(self, status: int, url: str, detail: str = ""):
        super().__init__(f"HTTP {status} from {url} {detail}".strip())
        self.status = status
        self.url = url


class Http:
    def __init__(
        self,
        retries: int = 3,
        backoff: float = 1.0,
        timeout: float = 30.0,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self._client = httpx.Client(timeout=timeout, headers={"User-Agent": USER_AGENT})
        self._retries = retries
        self._backoff = backoff
        self._sleep = sleep

    def get(self, url: str, params: dict | None = None, headers: dict | None = None) -> Any:
        return self._request("GET", url, params=params, headers=headers)

    def post(self, url: str, body: Any, headers: dict | None = None) -> Any:
        return self._request("POST", url, json=body, headers=headers)

    def _request(self, method: str, url: str, **kwargs) -> Any:
        for attempt in range(self._retries + 1):
            wait = self._backoff * 2**attempt
            final = attempt == self._retries
            try:
                response = self._client.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                if final:
                    raise HttpError(0, url, type(exc).__name__) from exc
                self._sleep(wait)
                continue
            if response.status_code < 400:
                return _decode(response, url)
            if response.status_code not in _RETRY_STATUS or final:
                raise HttpError(response.status_code, url)
            self._sleep(_retry_wait(response.headers.get("Retry-After"), wait))
        raise AssertionError("unreachable")


def _decode(response: httpx.Response, url: str) -> Any:
    if not response.content:
        return None
    try:
        return response.json()
    except ValueError as exc:
        raise HttpError(response.status_code, url, "reply is not JSON") from exc


def _retry_wait(header: str | None, backoff: float) -> float:
    """Retry-After can also be a date, which isn't worth parsing to save a few seconds."""
    try:
        wait = float(header)
    except (TypeError, ValueError):
        return backoff
    if not math.isfinite(wait) or wait < 0:
        return backoff
    return min(wait, _MAX_RETRY_WAIT)
