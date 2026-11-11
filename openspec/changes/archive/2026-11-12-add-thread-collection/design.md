# Design

## Context

This is the first change in a new project; see proposal.md for motivation and specs/ for the behaviour contract. Standing constraints and the stack are in the project context (`openspec/config.yaml`) and aren't repeated here. One more fact shapes this design: Threads has no public API for this, so the app reads data the web app loads for itself. That data format is undocumented and can change without notice.

## Goals / Non-Goals

**Goals:**
- Clear module boundaries, so that the Threads-specific parts (browser driving, payload parsing) can change without touching storage or review.
- The database is the single source of truth and the review interface: review views can be edited in DBeaver or psql.
- Collection is restartable at URL granularity, and partial progress inside a URL is never lost.

**Non-Goals:**
- Concurrency or throughput. Being slow is intentional.
- Keeping raw payloads. If parsing misses a field, the affected URLs are collected again.
- A generic plugin system. If other URL sources or automated reviewers are added, they are ordinary modules that read and write the existing tables.

## Decisions

### D1. Why this stack
This change establishes the stack listed in the project context. The reasons and alternatives:
- **Python** keeps possible LLM and NLP work easy, including free local models via mlx-lm, which may be served from another machine. Nothing in v1 depends on this.
- **Patchright** is a free, open-source drop-in replacement for Playwright's Python API. It drives a real browser, so requests look like normal web usage, and it removes Playwright's known automation leaks at the source (D14).
- **Installed Google Chrome**, not Playwright's bundled Chromium, because it's the browser real users run.
- **SQLAlchemy Core** (not the ORM) keeps the SQL explicit, which matters because a lot of the logic lives in upserts, views and triggers.
- **Alembic** handles schema migrations.
- Alternatives considered: plain Playwright, which leaks more automation signals (it remains the fallback, D14); TypeScript with Playwright, which is equally capable but weaker for possible ML work; and plain HTTP calls to the GraphQL endpoint, which is easier to detect, needs reverse-engineered request signing, and carries a higher ban risk.

### D2. Module layout (ports and adapters, kept light)
```
src/threadsift/
  cli.py            # Typer commands: db-upgrade, trait, login, import, run, runs
  config.py         # TOML config + env overrides (DB URL, pacing, caps, paths)
  domain.py         # dataclasses: CollectedItem, CollectedAuthor, MediaRef, RunResult…
  intake/           # URL normalization, file source, queue operations
  collection/
    browser.py      # persistent Chrome context, home-feed warm-up, navigation
    expansion.py    # reply-expansion strategy: when to scroll, which reply pages to open, within budget and caps
    capture.py      # response interception → payload buffer
    extract.py      # payload → CollectedItem/CollectedAuthor (pure, fixture-tested)
    controls.py     # allow-list of content-revealing controls: a guard for any click
    guard.py        # blocking detection → BlockedError
    monitor.py      # early-warning signals → EarlyWarning
    budget.py       # page-action counter, per-URL and per-run budgets
    pacing.py       # randomized waits, dwell by text length (capped), breaks, pointer paths, wheel scrolling
    runner.py       # run loop: cool-down check, budget check, pick pending URLs, collect, persist, log runs and attempts
  archive/          # repository: upserts for authors/items, run log
  review/           # nothing in Python yet; views and triggers live in migrations
migrations/         # Alembic; review views and triggers as raw SQL
tests/fixtures/     # anonymized captured payload JSON
tools/              # har_to_fixtures.py: pages saved from a normal browser → anonymized fixtures
```
- `extract.py` is pure (payload in, domain objects out). It is the part most likely to break when Threads changes its format, and it can be tested without a browser or a database.
- If automatic URL discovery is added, it becomes a new module under `intake/` that adds to the same queue.
- Reply expansion is its own strategy (`expansion.py`) that spends from the budget. A possible later two-stage mode would collect posts with only the replies visible without any reveal action, let the reviewer mark leads, then queue only the leads for full expansion. That would be a second strategy plus a queue flag, with no change to the archive or review model. Nothing is built for it now.
- If an LLM reviewer is added, it becomes a new `review/` module that writes `post_reviews` rows with `reviewer_kind = 'automated'`.

### D3. Data acquisition: intercept the web app's own responses
- Listen to the browser's responses for the GraphQL endpoints and parse the JSON. Also parse the initial data embedded in the page's HTML, because the first screen of a post is usually server-rendered and isn't fetched over GraphQL.
- The extractor doesn't depend on exact response paths. It walks each JSON tree and recognizes post-shaped objects by key signature (an identifier, a shortcode, the user, and the caption/text). That way, moved fields don't break it.
- How posts relate to each other comes from the structure around them, not from a "parent" field. A page carries the post, its ancestors (root first), and its replies as chains: the first post of a chain replies to the page's post and each next one replies to the one before it. A chain continues only through a reply that has exactly one reply, so a reply with two or more replies ends its chain and its own replies need their own page. The author's own continuation of a thread ("2 of 3") is read as a chain of replies to the first post. Quoted and reposted posts are nested inside the post and are never treated as part of the thread.
- Reply expansion (confirmed against saved pages): the first page of a post carries the first batch of its direct replies. Further batches (4 chains each, loaded by cursor) arrive as the page is scrolled, before the end of the page is reached (how far ahead is not known). A reply that has replies not yet seen is opened on its own page by its address, a paced and counted navigation, never by clicking its Reply control. No "view more replies" control was found. The expansion goes breadth first and stays within the action budget and the caps (D8).
- Alternative considered: reading the rendered DOM. It's fragile, harder to map to parents, and loses identifiers.

### D4. Session: persistent browser profile
- `login` opens a visible Chrome window with a persistent profile directory (default `./.session/`, which is git-ignored). The operator logs in, closes the window, and the profile is kept.
- Login happens **on the machine that runs collection**, so the account always appears from the same device and IP.
- Runs reuse the same profile in a visible window. That's the default because it is less detectable than headless mode. Headless can be set in config but isn't recommended (D14).
- Using a full persistent profile, rather than only saving cookies, keeps the browser fingerprint stable across runs.
- Alternative considered: logging in on another machine and copying the session over. Chrome encrypts profile cookies in an OS-specific way, so the profile isn't necessarily portable. A cookie export would work, but the account would suddenly move to a new device and IP, which is a common trigger for challenges.

### D5. Blocking guard, early warnings and cool-downs
**Hard blocks.** `guard.py` checks every navigation and response for:
- redirects to login, challenge, checkpoint or suspension paths;
- logged-out markers on the page;
- HTTP 429 or 403 from GraphQL endpoints;
- GraphQL error payloads indicating rate limits or required verification.

Any match raises `BlockedError`, which the runner never catches per URL. The run ends at once with reason `blocked`, and the current URL goes back to pending with the specific reason recorded.

**Early warnings.** `monitor.py` keeps rolling statistics for the run and raises `EarlyWarning` when any signal passes its threshold. The thresholds are configurable, and the defaults below are starting points to tune with real runs:
- **Empty reveals**: 3 reveal actions in a row that add no new items while the known reply counts say more exist. A reveal that loads at least one item resets the counter, so normal gaps such as deleted replies don't trigger it. Reveals are scrolls and opened reply pages. A scroll counts only once the page is at its bottom: scrolling through replies that are already loaded loads nothing by design.
- **Failing responses**: more than 30% of the last 20 GraphQL responses came back with an error status (other than the ones that already count as blocking). A 200 reply whose body is not a single JSON document is not counted either way: the page's background subscriptions and widgets send streamed or script-wrapped replies all the time (seen on the live site in the first false alarm), and they say nothing about throttling. If a data reply the app depends on comes back unreadable, the empty-reveals signal catches it, because scrolling then loads nothing. When this signal fires, the stored details list the failed responses (status, operation name, content type and size, never contents), so the cause can be judged.
- **Slowdown**: the median load time of the last 5 pages is more than 3× the median of the first 5 pages after the warm-up.

`EarlyWarning` is handled like `BlockedError`: the run ends at once with reason `early_warning`, the signals and their values are stored, and the current URL goes back to pending.

**Cool-downs** are derived from the `runs` table. A new run is refused while the latest run ended `blocked` or `early_warning` less than the configured cool-down for that reason ago. `run --override-cooldown` starts anyway and sets `cooldown_overridden` on the new run.

**Other errors.** Errors for a single URL (not found, private) mark it failed, and the run continues. Unexpected exceptions end the run with reason `error`, so an unknown condition never produces a fast series of failures.

### D6. Database schema

| Table | Key | Notes |
|---|---|---|
| `traits` | `id` | `name` unique, `description`, `is_active`. A partial unique index allows only one active trait. |
| `authors` | `id` (Threads user pk, text) | `username`, `display_name`, `is_verified`, `profile_pic_url`, `type author_type NULL`, `first_seen_at`, `last_seen_at` |
| `author_trait_status` | (`author_id`, `trait_id`) | `status` enum (`unknown`, `match`), `updated_at`. A missing row means unknown. |
| `posts` | `id` (Threads pk, text) | `code` (shortcode), `url`, `author_id` FK, `parent_id NULL`, `root_id`, `depth`, `published_at`, `first_text`, `latest_text`, `text_changed_at`, `like_count`, `reply_count`, `media jsonb` (`[{type,url}]`), `quoted_post_url`, `first_seen_at`, `last_seen_at` |
| `post_reviews` | (`post_id`, `trait_id`) | `is_evidence`, `is_lead`, `reviewer_kind` enum (`human`, `automated`), `reviewer_name`, `reviewed_at`. A missing row means unreviewed. |
| `url_queue` | `id`; `code` unique | `canonical_url`, `state` enum (`pending`, `done`, `partial`, `failed`), `reason`, `source`, `first_queued_at`, `last_attempt_at`, `post_id NULL` |
| `runs` | `id` | `started_at`, `ended_at`, `end_reason` enum (`completed`, `url_limit`, `action_budget`, `early_warning`, `blocked`, `interrupted`, `error`), `stop_details jsonb` (block reason or warning signals), `cooldown_overridden`, `actions_spent`, per-state counts |
| `url_attempts` | `id` | `run_id` FK, `url_id` FK, `started_at`, `ended_at`, `actions_spent`, `items_new`, `items_updated`, `outcome`, `reason` |

- `parent_id` and `root_id` deliberately have **no foreign keys**: a parent can be missing (spec: post-archive, "Parent not collected").
- `root_id` is set when an item is stored. If an ancestor arrives later, its own row is filled in, but its children don't need to change.
- A top-level post has no parent and is its own root at depth 0. A reply whose parent cannot be determined from the collected data has `parent_id`, `root_id` and `depth` all NULL, and a later collection that learns them fills them in (known links are never overwritten).
- Reviews and statuses live in separate tables keyed by trait, so collection upserts never touch them, which enforces "never change review data".
- "Unreviewed" is the absence of a review row. This avoids creating review rows for every item and keeps a future second trait cheap.
- One current review per item and trait. Review history, for example for comparing human and LLM opinions, can be added later as a separate table.

### D7. Upsert semantics
- **Authors**: `INSERT … ON CONFLICT (id) DO UPDATE` on the profile fields and `last_seen_at`.
- **Posts**:
  - On insert, `first_text = latest_text = text`.
  - On conflict, update the counts, `last_seen_at` and `latest_text`, and fill in parent, root and depth if they were unknown. Set `text_changed_at = now()` only when the text actually differs.
  - `first_text` and `first_seen_at` are never updated.
- Items are written in small transactions as each response is parsed, so data collected before a block or interruption is kept.

### D8. Action budgets, caps and depth
- `budget.py` holds one counter per run and per URL. Every page action goes through it: navigations, scroll gestures (one gesture is several wheel ticks) and clicks, including the home-feed warm-up. Pacing and input (D14) wrap the same calls, so nothing can act on the page without being counted.
- Before each action, the expansion strategy checks the URL budget. Running out stops expansion, and the URL becomes `partial` with reason `cap: action budget`.
- Before each URL, the runner checks that the remaining run budget covers a full per-URL budget, and otherwise ends the run with `action_budget`. So the run budget never cuts a URL off midway.
- `depth` is 0 for a top-level post and the parent's depth + 1 for a reply. It's computed from the parent chain in the payload, or from the stored parent.
- The expansion skips children of items at the maximum depth, doesn't store replies deeper than it, and stops when the reply cap for the URL is reached. All limits come from config, with defaults in the spec.
- A reply is opened on its own page when its reply count is higher than the replies seen under it, and each page is opened at most once, so replies that were deleted or are hidden are tried once and not again. Only replies below the URL's own post are expanded: the ancestors that arrive with a reply's page are not, because that would fetch siblings and parents. A top-level post's own count covers all of its replies, not only the direct ones, so it is not used for this.
- Expansion is breadth first, so a budget cut-off loses the deepest and latest replies rather than whole top-level branches.
- If anything was skipped while known reply counts said more replies existed, the URL becomes `partial`, naming the limit that applied (e.g. `cap: depth 5`, `cap: 1000 replies`, `cap: action budget`).
- **Refresh skipping.** A reply's count covers only its direct replies (confirmed in the saved pages); only a top-level post's count covers its whole thread. So the post's count decides whether anything is new, and by how much (`new − stored`). Before storing anything for the URL, the expansion reads the stored reply counts and identifiers of the thread and the URL's previous outcome. If the previous outcome was done and the count did not grow, it stops after the post's page. Otherwise it opens, breadth first, replies that are new or whose count changed, then the others, and stops once the number of replies not stored before reaches the growth. Comparing Threads' new count with its old one, rather than with stored rows, keeps deleted replies (which are never removed from the archive) from hiding new ones. Accepted trade-offs: edits and counts in skipped parts are not refreshed, and a deletion plus an addition between two collections leaves the count unchanged and hides the new reply until the count changes again. Reply URLs and previously partial or failed URLs are collected in full. There is no option to force a full rescan, by decision.

### D9. URL normalization
- Accepted pattern: `https?://(www\.)?threads\.(net|com)/@<user>/post/<code>`, with any query string or fragment. Everything else is rejected with a reason.
- The canonical form is `https://www.threads.com/@<user>/post/<code>`.
- The **dedup key is the shortcode**, not the full URL, because the username in a URL changes when the author renames.

### D10. Review views with INSTEAD OF triggers
- `review_items`: every item joined with its author, the active trait's review and author status, the root post's text and the parent's text.
  - Editable columns: `is_evidence`, `is_lead` (both NULL when unreviewed), `author_status`, `author_type`. Everything else is read-only.
  - `post_id` is a stable key, so DBeaver can be pointed at it as a virtual unique key.
- `review_queue`: `review_items` filtered to unreviewed items whose author isn't a match and has no type, ordered by root, then depth, then publication time.
- `collection_yield` (read-only): one row per queued URL, with total `actions_spent` from `url_attempts`, items in the URL post's subtree (a recursive query over `parent_id`), and, for the active trait, the reviewed, evidence and lead counts and the evidence per 100 actions.
- `author_candidates`: authors with at least one evidence item, status unknown and no type, with their evidence count. `author_status` and `author_type` are editable.
- One shared trigger function (`INSTEAD OF UPDATE`) does the work:
  - If either flag becomes non-NULL, it upserts `post_reviews`. The other flag is treated as false when NULL, `reviewer_kind` is `human`, `reviewed_at` is `now()`, and `reviewer_name` is `human` (not the database role, which says nothing about the person).
  - If both flags are set to NULL, it deletes the review, so the item is unreviewed again.
  - It upserts `author_trait_status` and updates `authors.type`.
  - It raises an exception if any read-only column changed.
- Alternative considered: a small review web UI. It's out of scope for v1, and the views stay useful later anyway.

### D11. Author type as a Postgres enum
- The first migration runs `CREATE TYPE author_type AS ENUM ()`, which is empty.
- The operator adds values with `ALTER TYPE author_type ADD VALUE '…'` and renames them with `RENAME VALUE`. DBeaver shows the column as a dropdown.
- Removing a value means recreating the type; a short recipe will be in the README.
- Migrations after the first one must never recreate or alter this type. This is a review rule written in `migrations/README`, so the operator's values survive upgrades.
- Alternative considered: a CHECK constraint, which makes removing values easy but gives no dropdown.

### D12. Configuration
- `config.toml` (git-ignored, with `config.example.toml` committed) holds every configurable value the specs name, with the specs' defaults: pause ranges, dwell (with a cap on how much text counts), breaks, home-feed warm-up, cool-downs, per-run URL limit, action budgets and tree caps. It also holds the early-warning thresholds (D5).
- It also holds:
  - the database URL, also settable as `THREADSIFT_DATABASE_URL`. The default is `postgresql://threadsift@localhost/threadsift`: a dedicated role that doesn't depend on the operator's own user name, on a local server that listens on the loopback address only. The password isn't stored in the config; it comes from `~/.pgpass` (or `PGPASSWORD`, or the URL);
  - the session directory and the headless flag (D4);
  - the Chrome channel and the automation backend: `patchright`, or `playwright` as the fallback (D14).
- The active trait is stored in the database, set with `trait set`, not in config, so the views can filter on it.

### D13. Testing
- `pytest` tests:
  - **Pure tests**: URL normalization, and the extractor against captured fixture JSON (the first post page, reply pages, nested replies, batches of replies loaded by scrolling, an edited post, media, quotes).
  - **Database tests**: against a throwaway database, created per test session, migrated with Alembic and dropped afterwards. They cover upserts, the never-delete rule, and the view triggers (flag edits, unreviewing, rejecting a read-only edit, enum rejection, queue exclusion).
    - By default the test fixture starts a temporary Postgres server from the `pgserver` dev dependency. It is a Python wheel with bundled Postgres binaries that runs in user space with its data in a temp directory, so nothing is installed system-wide.
    - If `THREADSIFT_TEST_DATABASE_URL` is set, tests use that server instead, for example the Postgres the app uses, where tests create and drop their own separate database.
  - **Runner tests**: with a fake collector, covering the state transitions, a block or early warning stopping the run, both cool-downs and the override, breaks, the per-run URL limit, and both action budgets (including that the run budget never cuts a URL off).
  - **Monitor tests**: synthetic sequences of reveal results, response outcomes and load times. They check that each signal triggers at its threshold and that normal gaps, such as deleted replies, don't trigger it. Pacing uses an injectable clock and random source, so these tests run instantly.
  - **Expansion tests**: the real page driver, capture and extractor run against a fake Threads site that serves pages in the shape of the real payloads (`tests/fake_threads.py`), covering reply pages, batches loaded by scrolling, the caps, the budget and the stops.
- No test contacts Threads. Checks against the live site follow a manual checklist and are run **only on the machine that runs collection**, with its session (project context).
- Capturing fixtures: pages are saved as HAR files from a normal browser and converted by `tools/har_to_fixtures.py`, which keeps only post payloads and replaces identities, text, tokens and media links with synthetic ones. The `capture` debug flag on `run` saves intercepted payloads for one URL to a local directory in the same format. HAR files hold the session and are git-ignored.

### D14. Ban-risk hardening
Automation traces are removed in the browser itself, not covered up by scripts. JavaScript stealth patches (redefining `navigator.webdriver`, faking plugins) are **not** used: detection scripts look for exactly those patches, and an inconsistent browser is more suspicious than an honest one.

**Browser**
- Patchright with installed Chrome (`channel="chrome"`), a persistent profile and a visible window. This follows Patchright's recommended setup: Patchright removes the automation flag, the "controlled by automated software" banner, the debugger calls detection services probe for, and injected helper scripts.
- No fingerprint overrides: no custom user agent, no forced viewport or window size, no proxy. Time zone, language and IP are the machine's real ones.
- Pages load completely. Images, video, fonts and scripts are never blocked.
- Fallback backend: plain Playwright with installed Chrome, with `--enable-automation` removed from the default arguments and `--disable-blink-features=AutomationControlled` added.

**Input**
- Expansion clicks nothing: it scrolls and opens pages by address. Any click that is ever needed moves the pointer along a curved, slightly jittered path with variable speed, and is a real input event, never JavaScript `element.click()`.
- Only content-revealing controls may be clicked ("view more replies" and similar), checked against a small allow-list of control patterns each time. This keeps the app from ever touching action controls (spec: "Never act on Threads").
- A scroll gesture is a few mouse-wheel ticks of uneven size with short pauses, sometimes with a small step back, and moves about 75 to 100% of the window's height (like Page Down), never past the end of the page and never a mere twitch.

**Session shape**
- Each run starts by scrolling the home feed (spec: thread-collection). Feed responses are ignored by the capture layer, so nothing from the feed can reach the archive.
- Dwell time is a configurable time per character of visible text, with random jitter, added to the random action pause. At most about a screenful of text counts (`dwell_max_chars`), so a page grown long by loading replies doesn't mean minutes of waiting.
- During breaks (spec: thread-collection) the browser stays open and idle on the last page, rather than closing, so a run looks like one continuous session.
- Not adopted, by operator decision: a varied run size and allowed hours of the day.
- The cool-downs after a block or an early warning are implemented in D5.

**Operator checklist (README, not code)**
- Warm up the dedicated account by hand for a week or two before the first run: complete and verify the profile, follow some accounts, and scroll the feed now and then. Keep using it by hand occasionally.
- Use the same machine and home connection every time, with no VPN, and log in only on the machine that runs collection.
- With no daily limit, how often runs are started is the strongest remaining lever. Leave generous gaps between runs.
- Leave the browser window alone during a run (see Risks). Other programs can be used beside it, because the app's input does not move the real cursor, but do not click, type or scroll in it.
- Use the same window size every time (Chrome reopens at the last size). Threads shows posts in a fixed-width column, so the full height and part of the width is enough, as long as the layout doesn't switch to the compact one. Keep other windows from covering it.
- No measure removes the risk: Meta's terms prohibit automated collection.

## Risks / Trade-offs

- [Threads changes its internal payloads] → Extraction is isolated, recognizes objects by key signature, and is fixture-tested. A failed extraction marks the URL failed with "parse" in the reason, so it is visible. Without raw payloads, affected URLs must be collected again.
- [Deep threads need many page actions] → The per-URL action budget bounds the effort, and the `collection_yield` view shows whether big threads are worth it. Budgets can be tuned from that data.
- [Early-warning false positives end runs needlessly] → Each stop stores the signals and values that triggered it, so thresholds can be tuned. The cool-down after a warning is short, and an unnecessary stop costs only time. The first live runs showed this: seven unreadable background replies were counted as failures and ended two runs; the rule was narrowed to error statuses and the failed responses are now stored with the warning.
- [Early-warning false negatives] → The signals are heuristics, and the hard-block guard still applies. New signals can be added to `monitor.py` as real throttling patterns are observed.
- [The account is restricted or banned anyway, e.g. through bot detection] → All measures in D14, the pacing in the specs, stopping at the first blocking signal, and the cool-down. The risk can't be removed entirely.
- [Patchright lags behind Playwright or Chrome updates, or stops being maintained] → It's a drop-in for the same API, so switching to the plain-Playwright fallback is a config change. The fallback leaks more signals, so it's a temporary measure.
- [Installed Chrome auto-updates and an update breaks automation] → Pin and update Patchright together with Chrome. A run that can't start the browser fails before opening any Threads page.
- [The visible browser window shares the desktop with the operator] → Clicking or typing in it, or closing it, disturbs a run. Minimizing it or covering it with other windows can slow rendering and timers, which may stall reply loading. The README advises leaving the window alone during a run and keeping it uncovered. Headless mode stays available in config, at a somewhat higher detection risk.
- [A refresh skips parts of a thread by reply counts] → Edits there are missed, and a deletion balanced by an addition hides the new reply until the count changes. Accepted in exchange for fewer page actions.
- [Edits through views depend on the client] → DBeaver needs a virtual key on views, which will be documented. psql `UPDATE` statements always work.
- [Removing an enum value is awkward] → Documented recipe. Removing values is expected to be rare.
- [Session data is sensitive] → It's kept in a git-ignored directory on the machine that runs collection only, and the README warns not to share it.
- [Tests run against a different Postgres build than production] → SQL is limited to Postgres 14+ features (project context). Before releasing, the suite can also be run once against the app's Postgres server via `THREADSIFT_TEST_DATABASE_URL`.
- [`pgserver` lags behind or stops being maintained] → It's only a test convenience. Any reachable Postgres works through `THREADSIFT_TEST_DATABASE_URL`, for example the app's server.

## Migration Plan

This is a new installation, so there's nothing to migrate. Everything below happens on the machine that runs collection:
1. Install the prerequisites: `uv` and Google Chrome (`patchright install chrome` can install it).
2. Create the dedicated role and the database: `sudo -u postgres createuser --pwprompt threadsift` and `sudo -u postgres createdb -O threadsift threadsift`, then put the password in `~/.pgpass`.
3. Clone the repo, `uv sync`, then copy `config.example.toml` to `config.toml`.
4. `threadsift db-upgrade`
5. `threadsift trait set`
6. Warm up the account by hand first (README checklist, D14), then `threadsift login`
7. `threadsift import <file>`
8. `threadsift run`

Upgrades: `git pull`, `uv sync`, `threadsift db-upgrade`.

Rollback is `alembic downgrade base`, which drops all data.

## Open Questions

None open. The live check (task 13.3) answered the earlier question about scrolling: on one post and one window size, each scroll gesture made while the page was far from its end loaded a new batch of replies (one gesture per batch), and batches arrived while about 2,300 to 3,000 pixels (roughly three screens) were still left to scroll. The run used 10 actions in total, including the home-feed browse. Because a scroll costs about one action per batch, counting every scroll gesture stays as designed. The result comes from a single post and window size, so the scroll log that `run --capture` saves (`scroll-log.json`) remains the way to re-check it if the action budget turns out too tight on larger posts.
