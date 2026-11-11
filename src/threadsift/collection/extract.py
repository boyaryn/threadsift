"""Payloads captured from a post page -> collected items. Pure: no browser, no database.

Threads has no public API, so this reads what the web app loads for itself. Posts
are recognized by their shape (an identifier, a shortcode, a user and a caption),
not by fixed response paths, so moved fields do not break extraction. Only the
three structures that say how posts relate to each other are understood:

* ``text_post_app_info.direct_replies``: chains of replies. The first post of a
  chain replies to the page's post; each next post replies to the one before it.
* ``text_post_app_info.self_thread``: the author's own continuation posts ("2 of 3"),
  chained the same way.
* ``text_post_app_info.containing_thread``: the page post's ancestors, root first.
* ``text_post_app_info.is_reply``: false for a top-level post.
"""
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from threadsift.domain import CollectedAuthor, CollectedItem, MediaRef

# Posts nested under these keys are quoted or reposted posts, not part of the thread.
QUOTE_KEYS = ("quoted_post", "reposted_post", "quoted_attachment_post")
# A post whose text_post_app_info has one of these says how other posts relate to it.
OWNER_KEYS = ("direct_replies", "containing_thread", "self_thread")


class ExtractionError(Exception):
    """The captured data could not be understood. The message starts with "parse"."""

    def __init__(self, detail: str) -> None:
        super().__init__(f"parse: {detail}")


@dataclass(frozen=True)
class Extraction:
    target: CollectedItem
    items: list[CollectedItem]
    # post id -> whether more of its direct replies can be loaded by scrolling
    more_replies: dict[str, bool] = field(default_factory=dict)


def is_post(node: Any) -> bool:
    return (
        isinstance(node, dict)
        and "pk" in node
        and "code" in node
        and isinstance(node.get("user"), dict)
        and "username" in node["user"]
        and ("caption" in node or "text_post_app_info" in node)
    )


def _edge_nodes(connection: Any) -> list[dict]:
    edges = connection.get("edges") if isinstance(connection, dict) else None
    return [e["node"] for e in edges or [] if isinstance(e, dict) and isinstance(e.get("node"), dict)]


class _Collector:
    def __init__(self) -> None:
        self.posts: dict[str, dict] = {}
        self.parent: dict[str, str] = {}
        self.top_level: set[str] = set()
        self.more_replies: dict[str, bool] = {}

    def visit(self, node: Any) -> None:
        if isinstance(node, list):
            for child in node:
                self.visit(child)
            return
        if not isinstance(node, dict):
            return
        if is_post(node):
            self._register(node)
        info = node.get("text_post_app_info")
        if isinstance(info, dict) and any(k in info for k in OWNER_KEYS):
            self._link(node, info)
        for key, value in node.items():
            if key not in QUOTE_KEYS:
                self.visit(value)

    def _register(self, post: dict) -> None:
        pk = str(post["pk"])
        self.posts[pk] = post
        info = post.get("text_post_app_info")
        if isinstance(info, dict) and info.get("is_reply") is False:
            self.top_level.add(pk)

    def _owner_pk(self, node: dict) -> str | None:
        if "pk" in node:
            return str(node["pk"])
        identifier = node.get("id")
        return str(identifier).split("_")[0] if identifier else None

    def _link(self, owner: dict, info: dict) -> None:
        owner_pk = self._owner_pk(owner)
        if owner_pk is None:
            return

        thread = info.get("containing_thread")
        if isinstance(thread, dict):
            ancestors = [p for p in _edge_nodes(thread.get("posts")) if is_post(p) and str(p["pk"]) != owner_pk]
            for index, ancestor in enumerate(ancestors):
                if index:
                    self.parent[str(ancestor["pk"])] = str(ancestors[index - 1]["pk"])
            if ancestors:
                self.parent[owner_pk] = str(ancestors[-1]["pk"])

        continuation = info.get("self_thread")
        if isinstance(continuation, dict):
            self._chain(owner_pk, continuation.get("posts"))

        replies = info.get("direct_replies")
        if isinstance(replies, dict):
            for chain in _edge_nodes(replies):
                self._chain(owner_pk, chain.get("posts"))
            page_info = replies.get("page_info")
            if isinstance(page_info, dict):
                self.more_replies[owner_pk] = bool(page_info.get("has_next_page"))

    def _chain(self, owner_pk: str, posts: Any) -> None:
        """The first post replies to the owner, each next one to the post before it."""
        previous = owner_pk
        for post in _edge_nodes(posts):
            if not is_post(post) or str(post["pk"]) == owner_pk:
                continue
            self.parent[str(post["pk"])] = previous
            previous = str(post["pk"])

    def resolve(self, pk: str) -> tuple[str | None, int | None]:
        """The top-level post of a post's thread and its depth, or (None, None)
        when the chain of parents is not fully known."""
        seen = [pk]
        current = pk
        while current not in self.top_level:
            parent = self.parent.get(current)
            if parent is None or parent in seen:
                return None, None
            seen.append(parent)
            current = parent
        return current, len(seen) - 1


def _text(post: dict) -> str:
    caption = post.get("caption")
    return caption.get("text") or "" if isinstance(caption, dict) else ""


def _media(post: dict) -> tuple[MediaRef, ...]:
    refs: list[MediaRef] = []
    for part in post.get("carousel_media") or [post]:
        videos = part.get("video_versions") or []
        candidates = (part.get("image_versions2") or {}).get("candidates") or []
        if videos and videos[0].get("url"):
            ref = MediaRef("video", videos[0]["url"])
        elif candidates and candidates[0].get("url"):
            ref = MediaRef("image", candidates[0]["url"])
        else:
            continue
        if ref not in refs:
            refs.append(ref)
    return tuple(refs)


def _post_url(post: dict) -> str:
    return f"https://www.threads.com/@{post['user']['username']}/post/{post['code']}"


def _quoted_url(post: dict) -> str | None:
    share = (post.get("text_post_app_info") or {}).get("share_info") or {}
    for key in QUOTE_KEYS:
        quoted = share.get(key)
        if is_post(quoted) or (isinstance(quoted, dict) and quoted.get("code") and isinstance(quoted.get("user"), dict)):
            return _post_url(quoted)
    return None


def _published(post: dict) -> datetime | None:
    taken_at = post.get("taken_at")
    return datetime.fromtimestamp(taken_at, UTC) if isinstance(taken_at, (int, float)) else None


def _item(post: dict, collector: _Collector) -> CollectedItem:
    pk = str(post["pk"])
    user = post["user"]
    info = post.get("text_post_app_info") or {}
    root, depth = collector.resolve(pk)
    parent = None if pk in collector.top_level else collector.parent.get(pk)
    return CollectedItem(
        id=pk,
        code=post["code"],
        url=_post_url(post),
        author=CollectedAuthor(
            id=str(user.get("pk") or user.get("id")),
            username=user["username"],
            display_name=user.get("full_name") or None,
            is_verified=bool(user.get("is_verified")),
            profile_pic_url=user.get("profile_pic_url") or None,
        ),
        text=_text(post),
        parent_id=parent,
        root_id=root,
        depth=depth,
        published_at=_published(post),
        like_count=post.get("like_count"),
        reply_count=info.get("direct_reply_count"),
        media=_media(post),
        quoted_post_url=_quoted_url(post),
    )


def _thread_of(target_pk: str, collector: _Collector) -> set[str]:
    """The target, its ancestors and all of its descendants."""
    keep = {target_pk}
    pk = target_pk
    while pk in collector.parent and collector.parent[pk] not in keep:
        pk = collector.parent[pk]
        keep.add(pk)
    children: dict[str, list[str]] = {}
    for child, parent in collector.parent.items():
        children.setdefault(parent, []).append(child)
    stack = [target_pk]
    while stack:
        for child in children.get(stack.pop(), []):
            if child not in keep:
                keep.add(child)
                stack.append(child)
    return keep


def extract(payloads: Iterable[Any], target_code: str) -> Extraction:
    """Items of the target post's thread found in the captured payloads.

    `payloads` are the captured documents (or objects with a `.data`). Posts that
    are not connected to the target (suggestions, other threads) are left out, and
    so are quoted posts. Raises ExtractionError if nothing can be made of the data.
    """
    collector = _Collector()
    for payload in payloads:
        collector.visit(getattr(payload, "data", payload))
    if not collector.posts:
        raise ExtractionError("no posts found in the captured data")
    target_pk = next((pk for pk, post in collector.posts.items() if post["code"] == target_code), None)
    if target_pk is None:
        raise ExtractionError(f"post {target_code} is not in the captured data")

    keep = _thread_of(target_pk, collector)
    items = [_item(post, collector) for pk, post in collector.posts.items() if pk in keep]
    target = next(item for item in items if item.id == target_pk)
    return Extraction(
        target=target,
        items=items,
        more_replies={pk: more for pk, more in collector.more_replies.items() if pk in keep},
    )
