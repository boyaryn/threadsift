from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


@dataclass(frozen=True)
class MediaRef:
    type: str
    url: str


@dataclass(frozen=True)
class CollectedAuthor:
    id: str
    username: str
    display_name: str | None = None
    is_verified: bool = False
    profile_pic_url: str | None = None


@dataclass(frozen=True)
class CollectedItem:
    """A top-level post or a reply, as read from Threads.

    A top-level post has no parent, is its own root and has depth 0. A reply
    whose parent could not be determined from the collected data has none of
    these links (parent, root and depth all None); the links are filled in when a
    later collection learns them.
    """

    id: str
    code: str
    url: str
    author: CollectedAuthor
    text: str
    parent_id: str | None = None
    root_id: str | None = None
    depth: int | None = None
    published_at: datetime | None = None
    like_count: int | None = None
    reply_count: int | None = None
    media: tuple[MediaRef, ...] = ()
    quoted_post_url: str | None = None

    def __post_init__(self) -> None:
        if self.parent_id is None and self.root_id is not None:
            if self.root_id != self.id or self.depth != 0:
                raise ValueError(
                    f"item {self.id} has no parent, so it must be its own root with depth 0"
                )


@dataclass(frozen=True)
class ThreadBaseline:
    """What an earlier, finished collection of a top-level post left in the archive,
    read before the new collection stores anything: the post's reply count then (it
    covers the whole thread) and the reply count stored for every item of the thread."""

    post_id: str
    reply_count: int
    known: dict[str, int | None]


class StoreOutcome(StrEnum):
    NEW = "new"
    UPDATED = "updated"
    UNCHANGED = "unchanged"


@dataclass
class StoreSummary:
    new: int = 0
    updated: int = 0
    unchanged: int = 0

    def add(self, outcome: StoreOutcome) -> None:
        setattr(self, outcome.value, getattr(self, outcome.value) + 1)


@dataclass
class RunResult:
    """How a run ended, as shown to the operator and stored in the run log."""

    run_id: int
    started_at: datetime
    ended_at: datetime
    end_reason: str
    actions_spent: int
    done: int = 0
    partial: int = 0
    failed: int = 0
    pending: int = 0
    stop_details: dict | None = None
    cooldown_overridden: bool = False
    error: str | None = None
    next_run_allowed: datetime | None = None
    attempts: list = field(default_factory=list)
