import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from threadsift.collection.guard import BlockedError, check_response, is_graphql_url
from threadsift.collection.monitor import EarlyWarning, Monitor

EMBEDDED_JSON = re.compile(
    r"<script[^>]*type=[\"']application/json[\"'][^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL
)


@dataclass(frozen=True)
class Payload:
    url: str
    kind: str  # "graphql" or "embedded"
    data: Any


def extract_embedded_json(html: str) -> list[Any]:
    """JSON documents embedded in a page's HTML. The first screen of a post is
    usually rendered by the server and never fetched over GraphQL."""
    found = []
    for raw in EMBEDDED_JSON.findall(html):
        try:
            found.append(json.loads(raw))
        except ValueError:
            continue
    return found


def describe_failure(response: Any, status: int) -> dict:
    """Why a GraphQL response counted as failed: status, which operation it was, what
    kind of body it had and how big. Never the body or the request's content, which can
    hold personal data and tokens. Anything that cannot be read is simply left out."""
    note: dict[str, Any] = {"status": status, "reason": f"HTTP {status}"}
    request = getattr(response, "request", None)
    for name, read in (
        ("operation", lambda: _operation_name(request)),
        ("content_type", lambda: (response.headers or {}).get("content-type")),
        ("bytes", lambda: len(response.body())),
    ):
        try:
            value = read()
        except Exception:
            continue
        if value is not None:
            note[name] = value
    return note


def _operation_name(request: Any) -> str | None:
    """The GraphQL operation's friendly name, from the request header or the form body."""
    if request is None:
        return None
    name = (getattr(request, "headers", None) or {}).get("x-fb-friendly-name")
    if name:
        return str(name)[:80]
    found = re.search(r"fb_api_req_friendly_name=([A-Za-z0-9_]+)", getattr(request, "post_data", None) or "")
    return found.group(1)[:80] if found else None


class ResponseCapture:
    """Listens to the web app's own responses.

    Blocking signs are checked on every response, but payloads are buffered (and
    response statistics kept) only between begin_url() and end_url(), so nothing
    from the home-feed warm-up can reach the archive. A listener cannot reliably
    raise into the code that is driving the page, so a problem is remembered and
    raised later by raise_pending().

    With `save_dir`, every buffered payload is also written to
    `save_dir/<label>/NNN-<kind>.json`, to build test fixtures from real pages.
    """

    def __init__(
        self, page: Any, monitor: Monitor | None = None, save_dir: Path | str | None = None
    ) -> None:
        self._monitor = monitor
        self._save_dir = Path(save_dir) if save_dir is not None else None
        self._label = "url"
        self._enabled = False
        self._payloads: list[Payload] = []
        self._saved = 0
        self._pending: Exception | None = None
        page.on("response", self._on_response)

    def begin_url(self, label: str = "url") -> None:
        self._payloads = []
        self._label = label
        self._saved = 0
        self._enabled = True

    def drain(self) -> list[Payload]:
        """Payloads captured since the last drain; capturing continues."""
        payloads, self._payloads = self._payloads, []
        return payloads

    def end_url(self) -> list[Payload]:
        self._enabled = False
        payloads, self._payloads = self._payloads, []
        return payloads

    def raise_pending(self) -> None:
        if self._pending is not None:
            error, self._pending = self._pending, None
            raise error

    def _remember(self, error: Exception) -> None:
        if self._pending is None:
            self._pending = error

    def _store(self, payload: Payload) -> None:
        self._payloads.append(payload)
        if self._save_dir is None:
            return
        folder = self._save_dir / self._label
        folder.mkdir(parents=True, exist_ok=True)
        self._saved += 1
        path = folder / f"{self._saved:03d}-{payload.kind}.json"
        document = {"url": payload.url, "kind": payload.kind, "data": payload.data}
        path.write_text(json.dumps(document, ensure_ascii=False, indent=1), encoding="utf-8")

    def _on_response(self, response: Any) -> None:
        url = response.url
        if is_graphql_url(url):
            self._on_graphql(response, url)
        elif self._enabled and self._is_document(response):
            self._on_document(response, url)

    def _on_graphql(self, response: Any, url: str) -> None:
        status = response.status
        body = self._read_json(response)
        try:
            check_response(url, status, body)
        except BlockedError as error:
            self._remember(error)
            return
        if not self._enabled:
            return
        ok = status == 200 and body is not None
        if ok:
            self._store(Payload(url, "graphql", body))
        # A 200 whose body is not a single JSON document is a streamed or script reply (the page's
        # background subscriptions and widgets send many); it says nothing about throttling, so it
        # is not counted either way. Failures are the calls that came back with an error status.
        if self._monitor is not None and (ok or status != 200):
            try:
                self._monitor.record_response(ok, None if ok else describe_failure(response, status))
            except EarlyWarning as warning:
                self._remember(warning)

    def _on_document(self, response: Any, url: str) -> None:
        if response.status != 200:
            return
        try:
            html = response.text()
        except Exception:
            return
        for data in extract_embedded_json(html):
            self._store(Payload(url, "embedded", data))

    @staticmethod
    def _is_document(response: Any) -> bool:
        try:
            return response.request.resource_type == "document"
        except Exception:
            return False

    @staticmethod
    def _read_json(response: Any) -> Any:
        try:
            return response.json()
        except Exception:
            return None
