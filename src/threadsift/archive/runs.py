import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import Engine, text


@dataclass(frozen=True)
class FinishedRun:
    id: int
    ended_at: datetime
    end_reason: str


class RunLog:
    """The run log: one row per run and one per attempt at a URL."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def latest_finished(self) -> FinishedRun | None:
        with self._engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT id, ended_at, end_reason::text AS end_reason FROM runs "
                    "WHERE ended_at IS NOT NULL AND end_reason IS NOT NULL "
                    "ORDER BY ended_at DESC, id DESC LIMIT 1"
                )
            ).first()
        return FinishedRun(row.id, row.ended_at, row.end_reason) if row else None

    def start_run(self, now: datetime, cooldown_overridden: bool) -> int:
        with self._engine.begin() as connection:
            return connection.execute(
                text("INSERT INTO runs (started_at, cooldown_overridden) VALUES (:now, :o) RETURNING id"),
                {"now": now, "o": cooldown_overridden},
            ).scalar_one()

    def finish_run(
        self, run_id: int, now: datetime, end_reason: str, details: dict | None,
        actions: int, done: int, partial: int, failed: int, pending: int,
    ) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE runs SET ended_at = :now, end_reason = CAST(:reason AS run_end_reason), "
                    "stop_details = CAST(:details AS jsonb), actions_spent = :actions, "
                    "urls_done = :done, urls_partial = :partial, urls_failed = :failed, urls_pending = :pending "
                    "WHERE id = :id"
                ),
                {
                    "id": run_id, "now": now, "reason": end_reason,
                    "details": json.dumps(details) if details is not None else None,
                    "actions": actions, "done": done, "partial": partial, "failed": failed, "pending": pending,
                },
            )

    def start_attempt(self, run_id: int, url_id: int, now: datetime) -> int:
        with self._engine.begin() as connection:
            return connection.execute(
                text("INSERT INTO url_attempts (run_id, url_id, started_at) VALUES (:r, :u, :now) RETURNING id"),
                {"r": run_id, "u": url_id, "now": now},
            ).scalar_one()

    def finish_attempt(
        self, attempt_id: int, now: datetime, actions: int, items_new: int,
        items_updated: int, outcome: str, reason: str | None,
    ) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE url_attempts SET ended_at = :now, actions_spent = :actions, items_new = :new, "
                    "items_updated = :updated, outcome = CAST(:outcome AS url_state), reason = :reason "
                    "WHERE id = :id"
                ),
                {"id": attempt_id, "now": now, "actions": actions, "new": items_new,
                 "updated": items_updated, "outcome": outcome, "reason": reason},
            )

    def recent(self, limit: int) -> list[dict]:
        with self._engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT id, started_at, ended_at, end_reason::text AS end_reason, stop_details, "
                    "cooldown_overridden, actions_spent, urls_done, urls_partial, urls_failed, urls_pending "
                    "FROM runs ORDER BY started_at DESC, id DESC LIMIT :limit"
                ),
                {"limit": limit},
            )
            return [dict(r) for r in rows.mappings()]
