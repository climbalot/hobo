"""Abstract REST transport: owns its httpx client, with the exchange-differing auth
surface (`get_signature`, `get_auth_headers`, optional `auth_init`) as abstract
methods. A request is signed only when `signed=True`, over the exact path+query+body
sent, so one client makes both public and private calls.
"""

from __future__ import annotations

import abc
import json as jsonlib
from urllib.parse import urlencode

import httpx

DEFAULT_TIMEOUT_SECS = 10.0


class AbstractRestClient(abc.ABC):
    def __init__(self, base_url: str, *, timeout_secs: float = DEFAULT_TIMEOUT_SECS, transport: httpx.BaseTransport | None = None) -> None:
        self._http = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout_secs, transport=transport)

    # --- exchange-specific auth surface ---

    @abc.abstractmethod
    def get_signature(self, timestamp: str, method: str, request_path: str, body: str) -> str: ...

    @abc.abstractmethod
    def get_auth_headers(self, method: str, request_path: str, body: str) -> dict[str, str]: ...

    def auth_init(self) -> None:
        """Optional handshake before signed calls (token / listen-key / session).
        No-op by default; exchanges that require it override this."""

    def default_headers(self) -> dict[str, str]:
        """Headers sent on every request, signed or not (e.g. an environment/demo
        selector). Empty by default; exchanges override."""
        return {}

    # --- transport ---

    def _request(self, method: str, path: str, *, params: dict | None = None, json_body: dict | None = None, signed: bool = False) -> dict:
        request_path = f"{path}?{urlencode(params)}" if params else path
        body = jsonlib.dumps(json_body) if json_body is not None else ""
        headers = dict(self.default_headers())
        if signed:
            headers.update(self.get_auth_headers(method, request_path, body))
        resp = self._http.request(method, request_path, content=body or None, headers=headers)
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # Surface the exchange's own error body (e.g. OKX {"code","msg"}) - the
            # bare status line hides why the call failed.
            raise httpx.HTTPStatusError(
                f"{exc}\nresponse body: {resp.text[:1000]}", request=exc.request, response=exc.response
            ) from exc
        return resp.json()

    def get(self, path: str, params: dict | None = None, signed: bool = False) -> dict:
        return self._request("GET", path, params=params, signed=signed)

    def post(self, path: str, json_body: dict | None = None, signed: bool = False) -> dict:
        return self._request("POST", path, json_body=json_body, signed=signed)

    def close(self) -> None:
        self._http.close()
