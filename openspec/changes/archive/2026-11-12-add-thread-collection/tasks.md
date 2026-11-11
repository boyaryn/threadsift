# Tasks

## 1. Project scaffolding

- [x] 1.1 Create the uv project (Python 3.12) with the package layout from design D2 (`src/threadsift/` with `intake/`, `collection/`, `archive/`, `review/`, plus `migrations/` and `tests/fixtures/`), and verify `uv sync` succeeds and `uv run python -c "import threadsift"` works
- [x] 1.2 Add runtime dependencies (typer, patchright, playwright as fallback, sqlalchemy, psycopg 3, alembic) and dev dependencies (pytest, pgserver), and verify `uv run pytest` runs and collects zero failing tests
- [x] 1.3 Add `.gitignore` entries for `.session/`, `config.toml` and the capture output directory, and a Typer app in `cli.py` with the command names `db-upgrade`, `trait`, `login`, `import`, `run` and `runs` registered as stubs; verify `uv run threadsift --help` lists all six
- [x] 1.4 Start `README.md` with the prerequisites and setup steps from the design's Migration Plan, and verify each documented command up to `uv sync` runs as written

## 2. Configuration

- [x] 2.1 Implement `config.py`: load `config.toml`, apply `THREADSIFT_DATABASE_URL` and other env overrides, and fall back to the defaults named in the specs (pause ranges, dwell per character, breaks, warm-up duration, cool-downs of 24h/6h, 60 URLs per run, 150/1200 action budgets, 1000-reply and depth-5 caps, early-warning thresholds, session dir, headless flag, Chrome channel, backend); verify unit tests cover defaults, file values, env override and rejection of invalid values
- [x] 2.2 Commit `config.example.toml` with every key and its default commented, and verify a test loads it and gets exactly the built-in defaults
- [x] 2.3 Document configuration in the README (keys, env overrides, the dedicated database role and where its password goes) and verify every key in the README exists in `config.example.toml`

## 3. Database schema and migrations

- [x] 3.1 Add the test fixture in `tests/conftest.py`: start a temporary Postgres from `pgserver` by default, use `THREADSIFT_TEST_DATABASE_URL` when set, create a throwaway database per session, migrate it with Alembic and drop it afterwards; verify a smoke test connects and sees the migrated schema
- [x] 3.2 Write the first Alembic migration: the `author_type` empty enum and the `traits`, `authors`, `author_trait_status`, `posts`, `post_reviews`, `url_queue`, `runs` and `url_attempts` tables as in design D6, including the one-active-trait partial unique index, the unique shortcode on `url_queue`, and no foreign keys on `posts.parent_id` and `posts.root_id`; verify a database test checks each table, enum, index and the absence of those two foreign keys
- [x] 3.3 Verify migration round-trip with a database test: `upgrade head` then `downgrade base` leaves an empty schema, and the SQL contains no features newer than Postgres 14
- [x] 3.4 Implement the `db-upgrade` command and verify it migrates a fresh database and is a no-op on a second run
- [x] 3.5 Write `migrations/README` with the rule that later migrations never recreate or alter `author_type`, and add the add/rename/remove-value recipe for the enum to the main README; verify the recipe's SQL runs against a test database

## 4. Archive repository

- [x] 4.1 Define the dataclasses in `domain.py` (`CollectedItem`, `CollectedAuthor`, `MediaRef`, `RunResult` and related types) and verify they import and type-check in a unit test
- [x] 4.2 Implement author upsert (profile fields and `last_seen_at` updated, `first_seen_at` kept), and verify database tests for insert, rename keeping one record, and first-seen unchanged
- [x] 4.3 Implement post upsert per design D7: `first_text = latest_text` on insert; on conflict update counts, `last_seen_at` and `latest_text`, set `text_changed_at` only when the text differs, never touch `first_text` or `first_seen_at`; verify database tests for the edited-post scenario, an unchanged refresh, and Cyrillic text with emoji stored character for character
- [x] 4.4 Store parent and top-level links, depth, media as `[{type,url}]` and the quoted-post URL without queueing it; verify database tests for the A/B/C chain, a parent that was never collected, a reply stored before its ancestor, and that the quoted post does not enter `url_queue`
- [x] 4.5 Write each item in its own small transaction so data survives an interruption, and verify with a test that a failure on item N leaves items 1..N-1 stored
- [x] 4.6 Verify the never-delete rule with database tests: a refresh that no longer includes a stored reply leaves it and its review row untouched, collection never writes `post_reviews` or `author_trait_status`, and new replies on refresh start unreviewed

## 5. URL intake

- [x] 5.1 Implement URL normalization in `intake/` per design D9 (accept `threads.net`/`threads.com`, with or without `www.`, drop query, fragment and trailing slash, canonical `https://www.threads.com/@<user>/post/<code>`, dedup key is the shortcode); verify unit tests cover host variants, the `?igshid=x` and trailing-slash example, a renamed username with the same code, and rejection of profile URLs, other sites and non-URL text with reasons
- [x] 5.2 Implement the file source and the import operation: skip blank lines and `#` lines, add new URLs as pending with the source file name and first-queued time, leave pending ones as one entry, set done/partial/failed ones back to pending, and report added / already pending / refresh requested / rejected with reasons; verify database tests for each count and for comments not being reported as rejected
- [x] 5.3 Implement the pending-URL picker (pending only, oldest first, up to the limit) and state updates with last-attempt time and reason; verify database tests for the Monday/Tuesday ordering example, done URLs not being picked, and the failed-with-reason "not found" case
- [x] 5.4 Wire the `import` command and document it in the README; verify by running `threadsift import` on a sample file against a test database and comparing the printed report with the expected counts

## 6. Traits and review views

- [x] 6.1 Implement the `trait` command (define a trait with name and description, make it the single active trait, show the active trait) and verify tests for creating "pro-renewables", rejecting a second active trait, and switching the active trait
- [x] 6.2 Create the `review_items` view with its `INSTEAD OF UPDATE` trigger function in a migration (design D10): both flags non-NULL or either non-NULL upserts `post_reviews` as `human` with the reviewer name `human` and `now()`, both NULL deletes the review, author status and type upserts, and any read-only column change raises; verify database tests for lead-only, evidence-only, neither, unreviewing, the text edit being rejected with stored text unchanged, and a type outside the enum being rejected
- [x] 6.3 Create `review_queue` (unreviewed, author not a match and no type, ordered by root, depth, publication time, with the root text, parent text and author shown) and verify database tests for editing a flag making the row leave the queue, a matched author leaving the queue, an author with a type leaving the queue, and thread grouping in tree order
- [x] 6.4 Create `author_candidates` (at least one evidence item, status unknown, no type, with evidence count, editable status and type) and verify database tests for the count of 1 example and for evidence marking not changing the author's status
- [x] 6.5 Create the read-only `collection_yield` view (actions from `url_attempts`, subtree item count via recursive query over `parent_id`, reviewed/evidence/lead counts for the active trait, evidence per 100 actions) and verify a database test for the 200 actions over two attempts and 4 evidence items giving 2 per 100
- [x] 6.6 Document review in the README (which view to use, the DBeaver virtual-key setup for `post_id`, psql `UPDATE` examples, how to add enum values) and verify the psql examples run against a test database

## 7. Pacing and action budget

- [x] 7.1 Implement `budget.py` with one counter per run and per URL that every page action must pass through, including the warm-up, plus the check that the remaining run budget covers a full URL budget; verify unit tests for counting, per-URL exhaustion and the 1100-of-1200 spent with 150 per URL refusing the next URL
- [x] 7.2 Implement `pacing.py` with an injectable clock and random source: random action pauses (4-12s default), URL pauses (30-90s), dwell per character of visible text with jitter, break scheduling (a break after a random 10-15 URLs lasting 3-8 minutes, redrawn each batch); verify unit tests for ranges, the 2000-vs-100 character dwell comparison with identical draws, and break timing, all running instantly
- [x] 7.3 Implement curved, jittered pointer paths with variable speed and uneven wheel-scroll steps with pauses and occasional scroll-back as pure path/step generators; verify unit tests that paths end at the target, are not straight lines, and that scroll steps are uneven

## 8. Blocking guard and early-warning monitor

- [x] 8.1 Implement `guard.py` and `BlockedError`: detect redirects to login/challenge/checkpoint/suspension paths, logged-out markers, HTTP 429/403 from GraphQL, and rate-limit or verification GraphQL error payloads, each with a specific reason such as "blocked: challenge"; verify unit tests with synthetic navigations and responses for each case plus a normal page passing
- [x] 8.2 Implement `monitor.py` and `EarlyWarning` with the three signals and configurable thresholds (3 empty reveals in a row while counts say more exist, over 30% of the last 20 GraphQL responses failing or empty, median of the last 5 loads over 3x the median of the first 5 after warm-up); verify unit tests with synthetic sequences that each signal fires at its threshold, a reveal loading one item resets the counter, deleted-reply gaps with working reveals do not fire, and fired warnings carry the signal names and values

## 9. Browser session, login and safe input

- [x] 9.1 Implement `browser.py` with Patchright and installed Chrome (`channel="chrome"`), a persistent profile in the session directory, a visible window by default, no fingerprint overrides and no resource blocking, with the plain-Playwright fallback (`--enable-automation` removed, `--disable-blink-features=AutomationControlled` added) selected by config; verify unit tests that the launch options for each backend contain exactly the expected arguments, and that no JavaScript stealth patches are applied
- [x] 9.2 Implement the `login` command opening a visible window on the persistent profile, and the no-session check that makes `run` refuse before opening any Threads page and tell the operator to run `login`; verify a test with a fake browser that `run` without a session opens no page, and perform the manual login on the collection machine to confirm a later launch starts logged in
- [x] 9.3 Implement the control allow-list so only content-revealing controls ("view more replies" and similar) can be clicked, real input events only, never `element.click()`, with every click and scroll routed through the pacing and budget layers; verify unit tests that like, follow, reply, repost, quote, bookmark and vote controls are refused and that an action that bypasses the budget cannot be issued
- [x] 9.4 Implement the home-feed warm-up (scroll for a random 1-3 minutes, counted against the run budget) and make the capture layer ignore feed responses; verify a test with a fake browser that the warm-up runs before the first URL, spends budget, and that nothing from the feed reaches the archive or queue
- [x] 9.5 Add the account warm-up and operator checklist to the README (warm the account by hand for one to two weeks, same machine and connection, no VPN, generous gaps between runs, leave the browser window alone, the session directory is sensitive and never shared, Meta's terms prohibit automated collection); verify the README contains each item from design D14

## 10. Capture and extraction

- [x] 10.1 Implement `capture.py` to intercept the web app's GraphQL responses and the embedded page data into a payload buffer, and the `--capture <dir>` flag on `run` that saves intercepted payloads for one URL; verify a test with fake responses that payloads are buffered, feed responses are dropped, and the flag writes files to the given directory
- [x] 10.2 Capture payloads (pages saved as HAR files from a normal browser and converted by `tools/har_to_fixtures.py`, or saved with `run --capture` on the collection machine) for a first post page, batches of replies loaded by scrolling, reply pages, nested replies, an edited post, media and a quote, anonymize them by hand where needed, and commit them to `tests/fixtures/`; verify the fixture files exist for each case and contain no real session tokens or cookies (search the files for them)
- [x] 10.3 Implement `extract.py` as a pure function that walks JSON by key signature (identifier, shortcode, user, text, reply-parent info) rather than fixed paths, producing items and authors with parent, top-level post and depth; verify fixture tests for the first page, reply pages, nested A/B/C parents, a URL pointing to a reply with its parents arriving, media references, the quoted-post URL, and Cyrillic text with emoji unchanged
- [x] 10.4 Make extraction failures produce a failed URL with "parse" in the reason; verify a test with a payload that matches no signature leading to a failed URL and not to an empty "done"

## 11. Reply expansion

- [x] 11.1 Implement `expansion.py`: scroll the post page to load further batches of replies, open a reply's page by its address when it has replies not yet seen (never by clicking its Reply control), breadth first, spending from the URL budget before every action and stopping when it runs out; verify tests with a fake page that the budget is never exceeded and that a budget cut-off loses the deepest replies before whole top-level branches
- [x] 11.2 Enforce the reply cap (about 1000) and depth cap (5 below the top-level post), both configurable, and mark the URL partial with the limit named (`cap: depth 5`, `cap: 1000 replies`, `cap: action budget`) only when replies were actually skipped while known counts said more existed; verify tests for the depth-5 example, the 40-replies-within-limits example marked done, and the 150-action example marked partial with the budget named
- [x] 11.3 Feed reveal results and response outcomes to the monitor and guard during expansion; verify a test that three empty reveals end expansion by raising `EarlyWarning`, and a hard-block response raises `BlockedError` and is not caught inside expansion

## 12. Run loop

- [x] 12.1 Implement the runner's start: cool-down check from the latest `runs` row (24h after `blocked`, 6h after `early_warning`, configurable), refusal with the time remaining, `run --override-cooldown` marking `cooldown_overridden`, then the home-feed warm-up; verify runner tests with a fake collector for the 3-hours-after-challenge refusal (about 21 hours left), the 7-hours-after-warning start, and the override being logged
- [x] 12.2 Implement the per-URL loop: pick pending URLs oldest first, check the run budget before each URL, apply the per-run URL limit and URL pauses and breaks, collect, persist, and set state done or partial; wire `run --capture <dir>` so that it collects only the first pending URL and saves the intercepted payloads to the directory; verify runner tests for the 60-URL limit with the rest staying pending, the 1100-spent action-budget ending, breaks after the drawn batch size, and the run budget never cutting off a URL midway, and `--capture` limiting the run to one URL and writing its payload files
- [x] 12.3 Handle stops and errors: a `BlockedError` or `EarlyWarning` ends the run at once with the reason and details stored and the URL back to pending with its reason (for example "blocked: challenge"); a not-found or private post marks only that URL failed and the run continues; an unexpected exception ends the run with `error`; Ctrl+C ends within seconds with data kept and the URL pending; verify a runner test for each, including that data saved before the stop is kept
- [x] 12.4 Record `url_attempts` (run, actions spent, items new and updated, outcome, reason) and the `runs` row (start, end, end reason, details, actions spent, per-state counts), print the end-of-run summary, and implement the `runs` command to list the run log; verify a runner test for the 8 done and 2 partial summary matching the stored row, and the 42-actions-30-new-items attempt record
- [x] 12.5 Document the commands `run`, `runs`, the cool-down behavior and the override in the README; verify the documented commands appear in `threadsift --help` and the examples run against a fake collector

## 13. Integration checks

- [x] 13.1 Run an end-to-end test with a fake collector against the migrated test database: import a file, run, review through the views, refresh with a new reply and an edit, and verify the archive, review data and queue state match the specs' scenarios
- [x] 13.2 Run the full test suite once against the app's own Postgres via `THREADSIFT_TEST_DATABASE_URL` and confirm it passes, to cover the difference from the `pgserver` build
- [x] 13.3 On the collection machine, run a small manual live check with one real URL after the account is warmed up, following a short checklist in the README: confirm login persistence, the warm-up, that the run reads only and the archive fills with correct parent links; record the observed GraphQL operations in the README notes, and log how many scroll gestures each batch of replies takes and how far ahead of the end of the page batches load, to answer the design's Open Question (and decide whether the counting and pacing of scrolls needs to change)
- [x] 13.4 Run `openspec validate add-thread-collection` and confirm it passes with no errors

## 14. Refresh skipping

- [x] 14.1 At the start of a URL, read the stored reply counts and identifiers of its thread and its previous outcome before anything is stored; verify database tests for a done, a partial and a never-collected URL
- [x] 14.2 Implement the skip rule in `expansion.py` (stop after the post's page when the count did not grow; otherwise changed and new replies first, then the others, stopping at the growth; done, not partial, when stopped this way; full expansion for reply URLs and previously partial or failed ones); verify tests with the fake site for the 1-action "nothing new" refresh, the deep new reply opening only its branch, the previously partial URL expanding in full, and a reply URL expanding in full
- [x] 14.3 Extend the end-to-end test: an edit in a skipped part keeps its stored text, an edit on the post's page is still picked up, and `url_attempts` shows the smaller action count
- [x] 14.4 Describe refresh behaviour in the README (Importing URLs: what a re-imported URL does and what it skips) and verify the README tests

## Workflow follow-up

- Archive the change after the project's review requirements are satisfied.
- Verify the archived result.
