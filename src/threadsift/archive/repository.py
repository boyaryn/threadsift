import json
from collections.abc import Callable, Iterable
from datetime import UTC, datetime

from sqlalchemy import Connection, Engine, text

from threadsift.domain import (
    CollectedAuthor,
    CollectedItem,
    StoreOutcome,
    StoreSummary,
    ThreadBaseline,
)

UPSERT_AUTHOR = text(
    """
    INSERT INTO authors (id, username, display_name, is_verified, profile_pic_url,
                         first_seen_at, last_seen_at)
    VALUES (:id, :username, :display_name, :is_verified, :profile_pic_url, :now, :now)
    ON CONFLICT (id) DO UPDATE SET
        username = EXCLUDED.username,
        display_name = COALESCE(EXCLUDED.display_name, authors.display_name),
        is_verified = EXCLUDED.is_verified,
        profile_pic_url = COALESCE(EXCLUDED.profile_pic_url, authors.profile_pic_url),
        last_seen_at = EXCLUDED.last_seen_at
    """
)

SELECT_POST = text(
    """
    SELECT latest_text, like_count, reply_count, parent_id, root_id, depth
    FROM posts WHERE id = :id FOR UPDATE
    """
)

# first_text and first_seen_at are never updated; links that were unknown are
# filled in when they become known.
UPSERT_POST = text(
    """
    INSERT INTO posts (id, code, url, author_id, parent_id, root_id, depth, published_at,
                       first_text, latest_text, like_count, reply_count, media,
                       quoted_post_url, first_seen_at, last_seen_at)
    VALUES (:id, :code, :url, :author_id, :parent_id, :root_id, :depth, :published_at,
            :text, :text, :like_count, :reply_count, CAST(:media AS jsonb),
            :quoted_post_url, :now, :now)
    ON CONFLICT (id) DO UPDATE SET
        latest_text = EXCLUDED.latest_text,
        text_changed_at = CASE
            WHEN posts.latest_text IS DISTINCT FROM EXCLUDED.latest_text THEN :now
            ELSE posts.text_changed_at
        END,
        like_count = COALESCE(EXCLUDED.like_count, posts.like_count),
        reply_count = COALESCE(EXCLUDED.reply_count, posts.reply_count),
        parent_id = COALESCE(posts.parent_id, EXCLUDED.parent_id),
        root_id = COALESCE(posts.root_id, EXCLUDED.root_id),
        depth = COALESCE(posts.depth, EXCLUDED.depth),
        last_seen_at = EXCLUDED.last_seen_at
    """
)


LATEST_FINISHED_ATTEMPT = text(
    """
    SELECT outcome::text AS outcome, ended_at FROM url_attempts
    WHERE url_id = :url_id AND ended_at IS NOT NULL
    ORDER BY id DESC LIMIT 1
    """
)
SELECT_TOP_LEVEL_POST = text(
    "SELECT id, reply_count, depth, last_seen_at FROM posts WHERE code = :code"
)
SELECT_THREAD_COUNTS = text("SELECT id, reply_count FROM posts WHERE root_id = :root_id")


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ArchiveRepository:
    """Writes collected authors and items. Only adds and updates; never deletes,
    and never touches review data."""

    def __init__(self, engine: Engine, clock: Callable[[], datetime] = _utcnow) -> None:
        self._engine = engine
        self._clock = clock

    def store_item(self, item: CollectedItem) -> StoreOutcome:
        """Store one item and its author in a single small transaction."""
        now = self._clock()
        with self._engine.begin() as connection:
            self._upsert_author(connection, item.author, now)
            return self._upsert_post(connection, item, now)

    def baseline_for(self, queued) -> ThreadBaseline | None:
        """What a refresh may compare the page against, or None when the URL must be
        collected in full: it was never collected, its latest attempt did not end done
        (partial, failed or interrupted), its post is a reply, its post has no stored
        count, or another collection has touched the post since (the post can arrive as
        an ancestor of a reply URL, which would have replaced the stored count without
        collecting the replies). `queued` needs `id` and `code`."""
        with self._engine.connect() as connection:
            attempt = connection.execute(LATEST_FINISHED_ATTEMPT, {"url_id": queued.id}).first()
            if attempt is None or attempt.outcome != "done":
                return None
            post = connection.execute(SELECT_TOP_LEVEL_POST, {"code": queued.code}).first()
            if post is None or post.depth != 0 or post.reply_count is None:
                return None
            if post.last_seen_at > attempt.ended_at:
                return None
            known = {
                row.id: row.reply_count
                for row in connection.execute(SELECT_THREAD_COUNTS, {"root_id": post.id})
            }
        return ThreadBaseline(post.id, post.reply_count, known)

    def store_items(self, items: Iterable[CollectedItem]) -> StoreSummary:
        """Store items one transaction each, so earlier items survive a later failure."""
        summary = StoreSummary()
        for item in items:
            summary.add(self.store_item(item))
        return summary

    def _upsert_author(self, connection: Connection, author: CollectedAuthor, now: datetime) -> None:
        connection.execute(
            UPSERT_AUTHOR,
            {
                "id": author.id,
                "username": author.username,
                "display_name": author.display_name,
                "is_verified": author.is_verified,
                "profile_pic_url": author.profile_pic_url,
                "now": now,
            },
        )

    def _upsert_post(
        self, connection: Connection, item: CollectedItem, now: datetime
    ) -> StoreOutcome:
        existing = connection.execute(SELECT_POST, {"id": item.id}).first()
        connection.execute(
            UPSERT_POST,
            {
                "id": item.id,
                "code": item.code,
                "url": item.url,
                "author_id": item.author.id,
                "parent_id": item.parent_id,
                "root_id": item.root_id,
                "depth": item.depth,
                "published_at": item.published_at,
                "text": item.text,
                "like_count": item.like_count,
                "reply_count": item.reply_count,
                "media": json.dumps([{"type": m.type, "url": m.url} for m in item.media]),
                "quoted_post_url": item.quoted_post_url,
                "now": now,
            },
        )
        if existing is None:
            return StoreOutcome.NEW
        changed = (
            existing.latest_text != item.text
            or _differs(existing.like_count, item.like_count)
            or _differs(existing.reply_count, item.reply_count)
            or (existing.parent_id is None and item.parent_id is not None)
            or (existing.root_id is None and item.root_id is not None)
            or (existing.depth is None and item.depth is not None)
        )
        return StoreOutcome.UPDATED if changed else StoreOutcome.UNCHANGED


def _differs(stored: int | None, new: int | None) -> bool:
    return new is not None and stored != new
