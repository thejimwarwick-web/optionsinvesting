"""Small fail-closed HTTP transport used by the production adapters."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol
import urllib.error
import urllib.request


@dataclass(frozen=True)
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes
    url: str


class Transport(Protocol):
    def request(self, method: str, url: str, *, headers: Mapping[str, str], body: bytes | None = None) -> HttpResponse: ...


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Redirects are responses, never implicit requests to a new authority."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class UrllibTransport:
    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout
        self._opener = urllib.request.build_opener(NoRedirect)

    def request(self, method, url, *, headers, body=None):
        request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        try:
            response = self._opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as error:
            # Includes redirects because redirect following is disabled.
            response = error
        raw = response.read()
        return HttpResponse(response.status, dict(response.headers.items()), raw, response.geturl())
