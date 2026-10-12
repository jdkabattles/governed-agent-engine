"""Stdlib HTTP client (urllib) behind a tiny protocol, so tests inject fakes and Termux
needs no compiled HTTP stack."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol

USER_AGENT = "governed-agent-engine/0.1 (+https://github.com/jdkabattles/governed-agent-engine)"


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: bytes
    headers: dict[str, str] = field(default_factory=dict)

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))

    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")


class HttpClient(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json_body: object | None = None,
        timeout: float = 60.0,
    ) -> HttpResponse: ...


class HttpTransportError(OSError):
    """Network-level failure (DNS, connect, timeout)."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None  # surface 3xx as-is; a redirect from an API is a failure signal


class UrllibHttpClient:
    def __init__(self) -> None:
        self._opener = urllib.request.build_opener(_NoRedirect)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json_body: object | None = None,
        timeout: float = 60.0,
    ) -> HttpResponse:
        if not url.startswith(("https://", "http://127.0.0.1", "http://localhost")):
            raise HttpTransportError(f"refusing non-https url: {url}")
        data = None if json_body is None else json.dumps(json_body).encode("utf-8")
        hdrs = {"User-Agent": USER_AGENT, **headers}
        if data is not None:
            hdrs.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)  # noqa: S310
        try:
            with self._opener.open(req, timeout=timeout) as resp:
                return HttpResponse(
                    resp.status, resp.read(), {k.lower(): v for k, v in resp.headers.items()}
                )
        except urllib.error.HTTPError as exc:
            return HttpResponse(
                exc.code, exc.read(), {k.lower(): v for k, v in exc.headers.items()}
            )
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            raise HttpTransportError(f"{type(exc).__name__}: {exc}") from exc
