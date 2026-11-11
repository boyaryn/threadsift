"""Turn HAR files saved from a normal browser into anonymized test fixtures.

Developer tool, not part of the app. It keeps only the payloads that carry posts
(the JSON embedded in the page HTML and the post-page GraphQL responses), replaces
every identity with a synthetic one, drops tokens, cookies, profile text and
signed media links, and writes files in the format `run --capture` produces:

    tests/fixtures/<case>/NNN-embedded.json | NNN-graphql.json
    {"url": ..., "kind": ..., "data": ...}

Usage (from the project root):
    uv run python tools/har_to_fixtures.py threadsift-har tests/fixtures [case ...]
With no case names every case in CASES is made; the HAR files they name must be present.
HAR files contain your session: never commit or share them.
"""
import base64
import hashlib
import json
import re
import sys
from pathlib import Path

POST_OPS = {
    "BarcelonaPostPageTargetQuery", "BarcelonaPostPageUpwardQuery", "BarcelonaPostPageDownwardQuery",
    "BarcelonaPostPageDirectRepliesRefetchQuery",
}
GRAPHQL_URL = "https://www.threads.com/api/graphql"

# case name -> (HAR file, include the page HTML's embedded data, GraphQL entry indexes to include,
#               identity namespace). Cases with different namespaces get non-colliding synthetic ids,
#               so a case can be (re)made later from a newer HAR without touching the others.
CASES = {
    "post-with-replies": ("1.har", True, [], 0),
    "reply-page": ("1.har", False, [123, 124, 122], 0),
    "nested-reply-page": ("1.har", False, [268, 269, 267], 0),
    "reply-page-chain": ("1.har", False, [243, 244, 242], 0),
    "media-post": ("3.har", True, [], 0),
    "media-post-reply-page": ("3.har", False, [146, 147, 145], 0),
    "quote-post": ("4.har", True, [], 0),
    "reply-url": ("5.har", True, [], 0),
    "post-without-replies": ("2.har", True, [], 0),
    # first page, then the first three batches and the last batch that scrolling loaded
    "direct-replies-paging": ("6.har", True, [124, 130, 157, 522], 1000),
}

DROP_KEYS = {
    "hd_profile_pic_versions", "biography", "text_app_biography", "friendship_status",
    "interop_messaging_user_fbid", "text_app_cover_photo_url", "viewer", "extensions",
    "video_dash_manifest", "logging_info_token", "organic_tracking_token", "text_fragments",
    "caption_add_on", "triage_comments", "audio", "text_app_music_info", "metaPlace",
    "meta_place", "media_overlay_info", "sharing_friction_info", "giphy_media_info",
    "text_app_glimmer_post_info", "fediverse_info", "reply_approval_info",
    "private_reply_partner", "transcription_data", "usertags", "link_preview_attachment",
    "link_preview_response",
}
SENTENCES = [
    "Нарешті встановив сонячні панелі ☀️", "Газ чи сонце для опалення будинку?",
    "Це дуже дорого, не вірю в окупність", "Дякую за розбір, корисно!",
    "Вітряки біля нас шумлять, але світло є 💡", "Що думаєте про теплові насоси?",
    "Согласен, у нас так же. Спасибо!", "Окупність приблизно 6 років, якщо без кредиту",
    "Сумніваюсь, що це працює взимку ❄️", "Ось мій досвід за рік:\nвитрати впали вдвічі",
]


def hid(n):
    return base64.urlsafe_b64encode(hashlib.sha1(str(n).encode()).digest())[:11].decode()


def walk(o):
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from walk(v)


def body_of(entry):
    c = entry["response"]["content"]
    text = c.get("text") or ""
    return base64.b64decode(text).decode("utf-8", "replace") if c.get("encoding") == "base64" else text


def op_of(entry):
    m = re.search(r"fb_api_req_friendly_name=([^&]+)", entry["request"].get("postData", {}).get("text", ""))
    return m.group(1) if m else None


def is_post(d):
    return isinstance(d, dict) and "pk" in d and "code" in d and "caption" in d


def select(har_dir, cases):
    """Raw payload documents per case: (url, kind, data)."""
    raw = {}
    for case in cases:
        har_name, with_html, entries, _ = CASES[case]
        entries_all = json.load(open(Path(har_dir) / har_name))["log"]["entries"]
        docs = []
        if with_html:
            html_entry = next(e for e in entries_all if e["response"]["content"].get("mimeType", "").startswith("text/html"))
            for block in re.findall(r'<script[^>]*type="application/json"[^>]*>(.*?)</script>', body_of(html_entry), re.S):
                try:
                    data = json.loads(block)
                except ValueError:
                    continue
                if any(is_post(d) for d in walk(data)):
                    docs.append((html_entry["request"]["url"], "embedded", data))
        for i in entries:
            e = entries_all[i]
            assert op_of(e) in POST_OPS, (case, i, op_of(e))
            docs.append((GRAPHQL_URL, "graphql", json.loads(body_of(e))))
        raw[case] = docs
    return raw


class Anonymizer:
    def __init__(self, raw, offset=0):
        self.offset = offset
        self.users, self.user_pks, self.posts, self.codes = {}, {}, {}, {}
        self.texts = {}
        for docs in raw.values():
            for _, _, data in docs:
                for d in walk(data):
                    if "username" in d and "pk" in d:
                        self.user(d["username"], d["pk"])
                    if "username" in d and "id" in d and "pk" not in d:
                        self.user(d["username"], d["id"])
                    if is_post(d):
                        self.post(d["pk"], d["code"])
        self.pk_map = {**{pk: v[1] for pk, v in self.user_pks.items()}, **{pk: v[0] for pk, v in self.posts.items()}}

    def user(self, username, pk):
        if username not in self.users:
            n = len(self.users) + 1 + self.offset
            self.users[username] = (f"user_{n:03d}", f"9{n:09d}")
        self.user_pks[pk] = self.users[username]

    def post(self, pk, code):
        if pk not in self.posts:
            n = len(self.posts) + 1 + self.offset
            self.posts[pk] = (f"7{n:018d}", f"A{hid(n)[:10]}")
            self.codes[code] = self.posts[pk]
        return self.posts[pk]

    def digits(self, m):
        s = m.group(0)
        if s in self.pk_map:
            return self.pk_map[s]
        digest = str(int(hashlib.sha1(s.encode()).hexdigest(), 16))
        return (digest * 3)[: len(s)]

    def string(self, key, s):
        for username, (alias, _) in self.users.items():
            s = s.replace(f"/@{username}/", f"/@{alias}/")
        for code, (_, alias) in self.codes.items():
            s = s.replace(code, alias[1] if isinstance(alias, tuple) else alias)
        return re.sub(r"\d{9,}", self.digits, s)

    def transform(self, o, key=None, parent=None):
        if isinstance(o, dict):
            out = {}
            for k, v in o.items():
                if k in DROP_KEYS:
                    continue
                out[k] = self.transform(v, k, o)
            return out
        if isinstance(o, list):
            return [self.transform(v, key, parent) for v in o]
        if not isinstance(o, str):
            return o
        if key == "username":
            return self.users[o][0]
        if key == "full_name":
            return self.users[parent["username"]][0].replace("user_", "User ") if parent and parent.get("username") in self.users else "User"
        if key == "text" and parent is not None and "pk" in parent and "can_translate" in parent:
            self.texts.setdefault(o, SENTENCES[len(self.texts) % len(SENTENCES)] + f" №{len(self.texts) + 1}")
            return self.texts[o]
        if key in ("accessibility_caption", "text_translation"):
            return "Тестовий опис"
        if key in ("end_cursor", "start_cursor", "cursor") or (key and "token" in key):
            return "redacted"
        if o.startswith("http") and "threads.com" not in o and "example.invalid" not in o:
            return "https://example.invalid/media/" + hid(o)[:8] + (".mp4" if parent and parent.get("type") == 2 else ".jpg")
        if key == "profile_pic_url" or (key == "url" and o.startswith("http")):
            return "https://example.invalid/media/" + hid(o)[:8] + (".mp4" if parent and parent.get("type") == 2 else ".jpg")
        if o.startswith("http") and key == "canonical_url":
            return self.string(key, o).split("?")[0]
        return self.string(key, o)

    def code_alias(self, code):
        return self.codes[code][1]


def main(har_dir, out_dir, *only):
    cases = list(only) or list(CASES)
    unknown = [c for c in cases if c not in CASES]
    if unknown:
        sys.exit(f"unknown case(s): {', '.join(unknown)}")
    raw = select(har_dir, cases)
    for namespace in sorted({CASES[c][3] for c in cases}):
        group = {c: raw[c] for c in cases if CASES[c][3] == namespace}
        anon = Anonymizer(group, offset=namespace)
        for case, docs in group.items():
            folder = Path(out_dir) / case
            folder.mkdir(parents=True, exist_ok=True)
            for old in folder.glob("*.json"):
                old.unlink()
            for n, (url, kind, data) in enumerate(docs, start=1):
                new_url = anon.string(None, url).split("?")[0] if kind == "embedded" else url
                doc = {"url": new_url, "kind": kind, "data": anon.transform(data)}
                text = json.dumps(doc, ensure_ascii=False, indent=1)
                (folder / f"{n:03d}-{kind}.json").write_text(text, encoding="utf-8")
            print(f"{case}: {len(docs)} payloads")
        leaks = list(anon.users) + list(anon.codes) + list(anon.posts) + list(anon.user_pks)
        bad = []
        for case in group:
            for path in (Path(out_dir) / case).glob("*.json"):
                text = path.read_text(encoding="utf-8")
                bad += [(case, leak[:12]) for leak in leaks if leak in text]
        print("leaked identities:", bad or "none")


if __name__ == "__main__":
    main(*sys.argv[1:])
