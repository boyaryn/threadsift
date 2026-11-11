"""A fake Threads site that serves pages in the shape of the real (anonymized) payloads.

Posts form a tree. Visiting a post's URL serves its page the way Threads does: the
post, its ancestors root-first, and its replies as chains (the first reply of a chain
replies to the page's post, each next one to the one before). Replies are paged: a
page carries `page_size` chains and the rest loads when the page is scrolled.
"""
import copy
import json
from pathlib import Path
from urllib.parse import urlsplit

from tests.fakes import FakeNetResponse, FakePage

FIXTURES = Path(__file__).parent / "fixtures"
GRAPHQL = "https://www.threads.com/api/graphql"


def _template() -> dict:
    doc = json.loads((FIXTURES / "post-with-replies" / "001-embedded.json").read_text(encoding="utf-8"))

    def find(o):
        if isinstance(o, dict):
            if "pk" in o and "code" in o and "caption" in o and "user" in o:
                return o
            for v in o.values():
                found = find(v)
                if found:
                    return found
        elif isinstance(o, list):
            for v in o:
                found = find(v)
                if found:
                    return found

    template = copy.deepcopy(find(doc["data"]))
    template["text_post_app_info"].pop("direct_replies", None)
    template["text_post_app_info"].pop("containing_thread", None)
    template["text_post_app_info"].pop("self_thread", None)
    return template


TEMPLATE = _template()


class FakeThreads:
    def __init__(self, page_size: int = 20, gestures_per_page: int = 1) -> None:
        self.page_size = page_size
        self.gestures_per_page = gestures_per_page
        self._gestures = 0
        self.nodes: dict[str, dict] = {}
        self.order: list[str] = []
        self.deleted: set[str] = set()
        self.visits: list[str] = []
        self.scrolled: dict[str, int] = {}
        self.page: FakePage | None = None
        self._current: str | None = None
        self._last_tick: float | None = None
        self.scrolling_loads_nothing = False

    # --- building the tree -------------------------------------------------
    def add(self, code: str, parent: str | None = None, text: str | None = None,
            hidden_replies: int = 0, author: str | None = None) -> "FakeThreads":
        self.nodes[code] = {"code": code, "parent": parent, "text": text or f"Текст {code} ☀️",
                            "hidden": hidden_replies, "author": author or f"user_{len(self.nodes) % 7:03d}"}
        self.order.append(code)
        return self

    def children(self, code: str) -> list[str]:
        return [c for c in self.order if self.nodes[c]["parent"] == code and c not in self.deleted]

    def thread(self, code: str) -> list[str]:
        """The post and everything below it."""
        out = [code]
        for child in self.children(code):
            out.extend(self.thread(child))
        return out

    def url(self, code: str) -> str:
        return f"https://www.threads.com/@{self.nodes[code]['author']}/post/{code}"

    def pk(self, code: str) -> str:
        return f"7{self.order.index(code) + 1:018d}"

    def depth(self, code: str) -> int:
        return 0 if self.nodes[code]["parent"] is None else 1 + self.depth(self.nodes[code]["parent"])

    # --- post objects ------------------------------------------------------
    def post(self, code: str) -> dict:
        node = self.nodes[code]
        post = copy.deepcopy(TEMPLATE)
        user_pk = f"9{abs(hash(node['author'])) % 10**9:09d}"
        post.update(pk=self.pk(code), id=f"{self.pk(code)}_{user_pk}", code=code,
                    caption={"text": node["text"], "pk": "1"}, taken_at=1793340044 + self.order.index(code))
        post["user"].update(pk=user_pk, id=user_pk, username=node["author"], full_name=node["author"])
        info = post["text_post_app_info"]
        info["is_reply"] = node["parent"] is not None
        # On Threads a reply's count covers its direct replies, and a top-level post's count its whole thread.
        counted = self.thread(code) if node["parent"] is None else [code]
        replies = len(counted) - 1 if node["parent"] is None else len(self.children(code))
        info["direct_reply_count"] = replies + sum(self.nodes[c]["hidden"] for c in counted)
        info["reply_to_author"] = None
        return post

    def chains(self, code: str) -> list[list[str]]:
        out = []
        for child in self.children(code):
            chain = [child]
            while len(self.children(chain[-1])) == 1:
                chain.append(self.children(chain[-1])[0])
            out.append(chain)
        return out

    def owner(self, code: str, **tpa) -> dict:
        post = self.post(code)
        return {"data": {"media": {"id": post["id"], "text_post_app_info": tpa}}}

    def downward(self, code: str, start: int) -> dict:
        chains = self.chains(code)[start:start + self.page_size]
        edges = [{"node": {"fetch_token": "t", "posts": {"edges": [{"node": self.post(c)} for c in chain]}}} for chain in chains]
        has_next = start + self.page_size < len(self.chains(code))
        return self.owner(code, direct_replies={"edges": edges, "page_info": {"has_next_page": has_next, "end_cursor": "c" if has_next else None}})

    def upward(self, code: str) -> dict:
        ancestors, parent = [], self.nodes[code]["parent"]
        while parent:
            ancestors.insert(0, parent)
            parent = self.nodes[parent]["parent"]
        return self.owner(code, containing_thread={"posts": {"edges": [{"node": self.post(a)} for a in ancestors]}})

    # --- serving -----------------------------------------------------------
    def attach(self, page: FakePage) -> "FakeThreads":
        self.page = page
        page.on_goto = self.serve
        page.on_scroll = self.serve_more
        page.at_bottom = self.at_bottom
        return self

    def at_bottom(self) -> bool:
        """The end of the loaded content is in view after enough scroll gestures."""
        return self._gestures >= self.gestures_per_page

    def code_of(self, url: str) -> str:
        return urlsplit(url).path.rsplit("/", 1)[-1]

    def serve(self, url: str) -> None:
        code = self.code_of(url)
        assert self.page is not None
        if code in self.deleted or code not in self.nodes:
            self.page.statuses[url] = 404
            return
        self.visits.append(code)
        self._current, self.scrolled[code], self._gestures = code, 0, 0
        docs = [{"data": {"media": self.post(code)}}]
        if self.nodes[code]["parent"]:
            docs.append(self.upward(code))
        docs.append(self.downward(code, 0))
        html = "<html><head>" + "".join(f'<script type="application/json">{json.dumps(d)}</script>' for d in docs) + "</head></html>"
        self.page.emit("response", FakeNetResponse(url, 200, html=html))

    def serve_more(self) -> None:
        """Called for every wheel tick; a new gesture starts after a pause of a second or more."""
        code = self._current
        if code is None or self.page is None:
            return
        now = self.page.timeline.now
        new_gesture = self._last_tick is None or now - self._last_tick > 1.0
        self._last_tick = now
        if not new_gesture:
            return
        self._gestures += 1
        if self.scrolling_loads_nothing or self._gestures < self.gestures_per_page:
            return
        self._gestures = 0
        self.scrolled[code] += 1
        self.page.emit("response", FakeNetResponse(GRAPHQL, 200, self.downward(code, self.scrolled[code] * self.page_size)))
