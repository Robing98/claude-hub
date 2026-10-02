"""HTTP client for the hub API. Standard library only."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class HubError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}" if status else message)
        self.status = status


class Client:
    def __init__(self, server_url: str, token: str, timeout: float = 120.0):
        self.server_url = server_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def request(self, method: str, path: str, body: Any = None,
                accept: tuple[int, ...] = ()) -> tuple[int, Any]:
        """Send a request. Statuses in ``accept`` are returned, others raise."""
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.server_url + path, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            if exc.code in accept:
                return exc.code, json.loads(payload or b"null")
            raise HubError(exc.code, payload.decode("utf-8", "replace")[:300]) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise HubError(0, f"Cannot reach {self.server_url}: {exc}") from exc
