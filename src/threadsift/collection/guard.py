from collections.abc import Iterable
from typing import Any
from urllib.parse import urlsplit

# Starting points, to be tuned against real runs (see the manual live check).
BLOCKING_SEGMENTS = {
    "login": "login wall",
    "challenge": "challenge",
    "checkpoint": "challenge",
    "suspended": "suspended",
    "disabled": "suspended",
}

# Matched against the text of dialogs and alerts only, never against post content.
DIALOG_MARKERS = {
    "challenge": (
        "confirm it's you", "confirm that it's you", "verify your identity",
        "suspicious activity", "unusual activity", "security check",
        "підтвердьте, що це ви", "підозріла активність", "незвичайна активність",
        "подтвердите, что это вы", "подозрительная активность", "необычная активность",
    ),
    "rate limited": (
        "try again later", "too many requests", "temporarily blocked", "temporarily restricted",
        "спробуйте пізніше", "забагато запитів", "тимчасово заблоковано",
        "попробуйте позже", "слишком много запросов", "временно заблокирован",
    ),
    "login wall": (
        "log in to continue", "log in to see", "sign in to continue",
        "увійдіть, щоб продовжити", "войдите, чтобы продолжить",
    ),
}

# Matched against error codes and messages in GraphQL responses.
ERROR_MARKERS = {
    "login wall": ("login_required", "not logged in", "user_not_logged_in"),
    "challenge": ("challenge_required", "checkpoint_required", "consent_required", "verification"),
    "rate limited": ("rate limit", "rate_limit", "ratelimit", "too many requests", "throttl"),
    "action restricted": ("feedback_required", "action_blocked"),
}


class BlockedError(Exception):
    """A sign of blocking was seen. The run must stop at once, without retrying."""

    def __init__(self, kind: str, detail: str = "") -> None:
        self.kind = kind
        self.detail = detail
        super().__init__(f"blocked: {kind}" + (f" ({detail})" if detail else ""))

    @property
    def reason(self) -> str:
        return f"blocked: {self.kind}"


def is_graphql_url(url: str) -> bool:
    return "/graphql" in urlsplit(url).path.lower()


def check_url(url: str) -> None:
    """Raise if a navigation ended on a login, challenge, checkpoint or suspension page."""
    path = urlsplit(url).path.lower()
    segments = [s for s in path.split("/") if s]
    if not segments or segments[0].startswith("@"):
        return
    if segments[0] == "accounts" and len(segments) > 1:
        segments = segments[1:]
    kind = BLOCKING_SEGMENTS.get(segments[0])
    if kind:
        raise BlockedError(kind, f"redirected to {path}")


def check_page(url: str, *, login_form_present: bool = False, dialog_text: str = "") -> None:
    """Check the page that was reached: its address, whether it shows a login form
    (the session was logged out), and the text of any dialog or alert on it."""
    check_url(url)
    if login_form_present:
        raise BlockedError("login wall", "login form on the page")
    lowered = dialog_text.lower()
    for kind, phrases in DIALOG_MARKERS.items():
        for phrase in phrases:
            if phrase in lowered:
                raise BlockedError(kind, f"dialog says: {phrase}")


def check_response(url: str, status: int, body: Any = None) -> None:
    """Check a response from the web app's own GraphQL endpoint. Responses from
    other hosts and paths (images, scripts, ...) are ignored."""
    if not is_graphql_url(url):
        return
    if status == 429:
        raise BlockedError("rate limited", "HTTP 429")
    if status == 401:
        raise BlockedError("login wall", "HTTP 401")
    if status == 403:
        raise BlockedError("forbidden", "HTTP 403")
    for text in _error_texts(body):
        lowered = text.lower()
        for kind, markers in ERROR_MARKERS.items():
            if any(marker in lowered for marker in markers):
                raise BlockedError(kind, f"GraphQL error: {text[:120]}")


def _error_texts(body: Any) -> Iterable[str]:
    """Strings found under error-like keys of a GraphQL response."""
    if isinstance(body, dict):
        for key, value in body.items():
            if key in ("errors", "error", "error_type", "error_code", "message", "status_message", "extensions"):
                yield from _all_strings(value)
    elif isinstance(body, list):
        for item in body:
            yield from _error_texts(item)


def _all_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for inner in value.values():
            yield from _all_strings(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from _all_strings(inner)
