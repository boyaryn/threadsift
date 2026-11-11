from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import Engine, text

from threadsift.intake.source import FileEntry, Rejection, read_url_file


@dataclass
class ImportReport:
    added: int = 0
    already_pending: int = 0
    refresh_requested: int = 0
    rejected: list[Rejection] = field(default_factory=list)


@dataclass(frozen=True)
class QueuedUrl:
    id: int
    code: str
    canonical_url: str


def _utcnow() -> datetime:
    return datetime.now(UTC)


class UrlQueue:
    def __init__(self, engine: Engine, clock: Callable[[], datetime] = _utcnow) -> None:
        self._engine = engine
        self._clock = clock

    def import_file(self, path: Path | str) -> ImportReport:
        """Import a URL file in one transaction; the source is the file's name."""
        return self.import_entries(read_url_file(path), source=Path(path).name)

    def import_entries(
        self, entries: Iterable[FileEntry | Rejection], source: str
    ) -> ImportReport:
        report = ImportReport()
        now = self._clock()
        with self._engine.begin() as connection:
            for entry in entries:
                if isinstance(entry, Rejection):
                    report.rejected.append(entry)
                    continue
                existing = connection.execute(
                    text("SELECT id, state FROM url_queue WHERE code = :code FOR UPDATE"),
                    {"code": entry.url.code},
                ).first()
                if existing is None:
                    connection.execute(
                        text(
                            "INSERT INTO url_queue (code, canonical_url, source, first_queued_at) "
                            "VALUES (:code, :url, :source, :now)"
                        ),
                        {
                            "code": entry.url.code,
                            "url": entry.url.canonical,
                            "source": source,
                            "now": now,
                        },
                    )
                    report.added += 1
                elif existing.state == "pending":
                    report.already_pending += 1
                else:
                    connection.execute(
                        text("UPDATE url_queue SET state = 'pending', reason = NULL WHERE id = :id"),
                        {"id": existing.id},
                    )
                    report.refresh_requested += 1
        return report

    def pending(self, limit: int) -> list[QueuedUrl]:
        """Pending URLs, oldest first, up to `limit`."""
        with self._engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT id, code, canonical_url FROM url_queue WHERE state = 'pending' "
                    "ORDER BY first_queued_at, id LIMIT :limit"
                ),
                {"limit": limit},
            )
            return [QueuedUrl(r.id, r.code, r.canonical_url) for r in rows]

    def record_result(
        self,
        url_id: int,
        state: str,
        reason: str | None = None,
        post_id: str | None = None,
    ) -> None:
        """Set a URL's state after an attempt and stamp the attempt time.

        `state` is done, partial, failed, or pending (an attempt that was stopped
        and must be retried; the reason says why).
        """
        with self._engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE url_queue SET state = CAST(:state AS url_state), reason = :reason, "
                    "last_attempt_at = :now, post_id = COALESCE(:post_id, post_id) WHERE id = :id"
                ),
                {
                    "id": url_id,
                    "state": state,
                    "reason": reason,
                    "post_id": post_id,
                    "now": self._clock(),
                },
            )
