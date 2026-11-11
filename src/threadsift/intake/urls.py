import re
from dataclasses import dataclass
from urllib.parse import urlsplit

THREADS_HOSTS = {"threads.net", "threads.com", "www.threads.net", "www.threads.com"}
POST_PATH = re.compile(r"^/@(?P<username>[A-Za-z0-9._]+)/post/(?P<code>[A-Za-z0-9_-]+)/?$")


class RejectedUrl(ValueError):
    """The line is not a Threads post URL; the message says why."""


@dataclass(frozen=True)
class NormalizedUrl:
    code: str
    username: str

    @property
    def canonical(self) -> str:
        return f"https://www.threads.com/@{self.username}/post/{self.code}"


def normalize_post_url(line: str) -> NormalizedUrl:
    """Convert a Threads post URL to its canonical form.

    The dedup key is the shortcode, not the full URL, because the username in a
    URL changes when its author renames.
    """
    value = line.strip()
    try:
        parts = urlsplit(value)
    except ValueError:
        raise RejectedUrl("not a URL") from None
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc or " " in value:
        raise RejectedUrl("not a URL")
    host = (parts.hostname or "").lower()
    if host not in THREADS_HOSTS:
        raise RejectedUrl("not a Threads URL")
    match = POST_PATH.match(parts.path)
    if not match:
        raise RejectedUrl("not a post URL (for example a profile or another Threads page)")
    return NormalizedUrl(code=match["code"], username=match["username"])
