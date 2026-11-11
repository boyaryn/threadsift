from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from threadsift.intake.urls import NormalizedUrl, RejectedUrl, normalize_post_url


@dataclass(frozen=True)
class Rejection:
    line_number: int
    text: str
    reason: str


@dataclass(frozen=True)
class FileEntry:
    line_number: int
    url: NormalizedUrl


def read_url_file(path: Path | str) -> Iterator[FileEntry | Rejection]:
    """Yield an entry or a rejection for each meaningful line of a URL file.

    Blank lines and lines starting with `#` are skipped silently.
    """
    with Path(path).open(encoding="utf-8-sig") as handle:
        for number, raw in enumerate(handle, start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            try:
                yield FileEntry(number, normalize_post_url(line))
            except RejectedUrl as error:
                yield Rejection(number, line, str(error))
