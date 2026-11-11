# threadsift

Finds Threads users who have one chosen trait (for example, a positive attitude towards renewable energy) by collecting posts with their reply trees and reviewing them by hand.

Everything runs on your own machine and costs nothing. Collection reads Threads under a dedicated, logged-in account. It never likes, follows, replies or reposts.

## Setup

Everything below happens on the machine that runs collection.

1. Install the prerequisites: [uv](https://docs.astral.sh/uv/), Google Chrome (`uv run patchright install chrome` can install it), and a Postgres 14+ server.
2. Create a dedicated database role and database. The role is called `threadsift`, whatever your own user name is (on macOS with Homebrew, leave out `sudo -u postgres`):

   ```
   sudo -u postgres createuser --pwprompt threadsift
   sudo -u postgres createdb -O threadsift threadsift
   ```

   Keep the password out of the config by putting it in `~/.pgpass`, which the Postgres client reads by itself (replace `YOUR_PASSWORD`):

   ```
   echo 'localhost:5432:threadsift:threadsift:YOUR_PASSWORD' >> ~/.pgpass
   chmod 600 ~/.pgpass
   ```

3. Clone the repository and install the dependencies:

   ```
   uv sync
   ```

4. Copy `config.example.toml` to `config.toml`.

   If you copied the project from a Mac, leave out `.venv/`, `.session/` and the macOS metadata files named `._*` (with tar: `COPYFILE_DISABLE=1 tar czf ...`), or delete them afterwards with `find . -name '._*' -not -path './.venv/*' -delete`; the `._*.py` ones would otherwise break `db-upgrade`.
5. Create or upgrade the schema: `uv run threadsift db-upgrade`
6. Define the trait: `uv run threadsift trait set pro-renewables -d "positive attitude towards renewable energy"`
7. Warm up the account by hand first (see the operator checklist), then log in: `uv run threadsift login`
8. Queue post URLs from a text file: `uv run threadsift import <file>`
9. Collect: `uv run threadsift run`

To upgrade later: `git pull`, `uv sync`, `uv run threadsift db-upgrade`.

## Configuration

Settings live in `config.toml` (git-ignored). Copy `config.example.toml`, which lists every key with its default and a short comment. A missing `config.toml` means all defaults. Unknown keys and invalid values are rejected at startup.

Two environment variables override the file:

- `THREADSIFT_DATABASE_URL` replaces `database.url`.
- `THREADSIFT_CONFIG` names a different config file; it must exist.

The database URL defaults to `postgresql://threadsift@localhost/threadsift`: the dedicated role `threadsift` on the local server, which by default listens on the loopback address only. The password is not stored in the config: it comes from `~/.pgpass` (see Setup), or from `PGPASSWORD`, or you can put it in the URL (`postgresql://threadsift:PASSWORD@localhost/threadsift`) in `config.toml` or in `THREADSIFT_DATABASE_URL`. Relative paths such as `browser.session_dir` are resolved against the directory you run `threadsift` from.

| Section | Keys |
|---|---|
| Database | `database.url` |
| Browser | `browser.session_dir`, `browser.headless`, `browser.channel`, `browser.backend` |
| Pacing | `pacing.action_pause_seconds`, `pacing.url_pause_seconds`, `pacing.dwell_seconds_per_char`, `pacing.dwell_jitter`, `pacing.dwell_max_chars`, `pacing.urls_per_break`, `pacing.break_minutes`, `pacing.warmup_minutes` |
| Limits | `limits.urls_per_run`, `limits.url_action_budget`, `limits.run_action_budget`, `limits.max_replies`, `limits.max_depth` |
| Cool-downs | `cooldown.blocked_hours`, `cooldown.early_warning_hours` |
| Early warning | `early_warning.empty_reveals`, `early_warning.failure_window`, `early_warning.failure_ratio`, `early_warning.slowdown_pages`, `early_warning.slowdown_factor` |

Ban risk comes first: lower pauses, bigger budgets and shorter cool-downs all raise the chance that the collecting account is restricted. `browser.backend = "playwright"` is only a fallback if Patchright stops working; it leaks more automation signals.

## Author type values

An author can have an optional type (for example, "organization"), chosen from a list of allowed values. The list is a Postgres enum, `author_type`, and **starts empty**. A database client such as DBeaver shows the column as a dropdown. Run these in `psql` (or any SQL console).

Add values:

```sql
ALTER TYPE author_type ADD VALUE 'organization';
ALTER TYPE author_type ADD VALUE 'bot';
```

Rename a value:

```sql
ALTER TYPE author_type RENAME VALUE 'bot' TO 'spam';
```

Removing a value means recreating the type, because Postgres cannot drop a single enum value. Authors that use the removed value lose their type. The review views depend on the column, so take them down first (this removes only the views, no data) and bring them back afterwards.

1. Drop the views:

   ```
   uv run alembic downgrade 0001
   ```

2. Recreate the type. List the values you want to **keep**:

   ```sql
   BEGIN;
   UPDATE authors SET type = NULL WHERE type = 'spam';
   ALTER TYPE author_type RENAME TO author_type_old;
   CREATE TYPE author_type AS ENUM ('organization');
   ALTER TABLE authors ALTER COLUMN type TYPE author_type USING type::text::author_type;
   DROP TYPE author_type_old;
   COMMIT;
   ```

3. Recreate the views:

   ```
   uv run threadsift db-upgrade
   ```

Later migrations never touch this type (see `migrations/README`), so your values survive `db-upgrade`.

## Importing URLs

Collection is driven by a queue. Put post URLs in a text file, one per line. Blank lines and lines starting with `#` are ignored:

```
# batch 1
https://www.threads.com/@someuser/post/ABC123
https://threads.net/@other/post/DEF456?igshid=x
```

```
uv run threadsift import batch-01.txt
```

The report counts:

- **Added**: new URLs, queued as pending.
- **Already pending**: URLs that were already waiting (or repeated in the file).
- **Refresh requested**: URLs that were already done, partial or failed; they go back to pending so the next run collects them again and picks up new replies. A processed URL is only collected again when it appears in an imported file again.
- **Rejected**: lines that are not Threads post URLs (a profile URL, another site, plain text), listed with the line number and reason. They are not queued.

Different spellings of the same post (`threads.net` or `threads.com`, with or without `www.`, query strings, trailing slashes) count as one entry. The post is identified by its shortcode, so a renamed author does not create a duplicate. Each URL remembers the file it first came from and when it was first queued.

A run takes only pending URLs, oldest first, up to the per-run limit (`limits.urls_per_run`).

### What a refresh does and what it skips

A URL that is collected again is not walked from scratch every time, because every page opened is a page action and an extra risk for the account. The rules, for a URL that points to a top-level post and was collected in full (done) the last time:

- The post's own page is always opened, and everything on it is updated: new replies, edited text and the like and reply counts of the items it shows.
- Threads gives a top-level post the reply count of its whole thread. If that count has **not grown** since the last collection, nothing else is opened and nothing is scrolled: the refresh costs one page action and the URL is done.
- If it **has grown**, the app looks only until it has found as many new replies as the count grew, starting with the replies whose own count changed or that are new, then scrolling the post's replies, then the remaining replies in breadth-first order. It does not go on to open branches once the new replies are found, and that does not make the URL partial.

What this means for you:

- **Edits and like counts in the parts that are not opened are not refreshed.** A reply edited in a branch the refresh skipped keeps the text it had at the last time the branch was opened.
- **A deletion balanced by a new reply can hide it.** If one reply was deleted and one added since the last collection, the count looks unchanged, and the new reply is missed until the count changes again.
- **There is no option to force a full rescan.**

These URLs are always collected in full, as the first time: a URL never collected before, one whose last attempt was partial, failed or interrupted, one that points to a reply (a reply's count covers only its direct replies), and one whose post was refreshed by another URL's collection since (the post can arrive as the parent of a reply you also listed).

## Reviewing

Collected items start unreviewed. You review by editing rows of views in any database client (DBeaver, psql, ...). All views work on the **active trait** (`threadsift trait show`).

| View | Use it to | Editable columns |
|---|---|---|
| `review_queue` | Work through unreviewed items, thread by thread. Items of authors who are a match or have a type are left out. | `is_evidence`, `is_lead`, `author_status`, `author_type` |
| `review_items` | Every item, reviewed or not: correct a review, or take it back. | the same |
| `author_candidates` | Decide on authors who have evidence items but no status or type yet. | `author_status`, `author_type` |
| `collection_yield` | See which URLs were worth the collection effort (read-only). | none |

Each item row shows the item's text, its direct parent's text, the thread's top-level post text and the author. A review sets two independent flags, `is_evidence` (the item shows its author has the trait) and `is_lead` (replies to it are likely to be evidence). A row with both flags empty is unreviewed; setting a flag to `NULL` while the other is also empty takes the review back. Every review made through a view is stored as a human review (reviewer kind `human`, reviewer name `human`) with the current time, whichever database role your client is connected as. Editing any other column (for example `text`) is rejected.

`author_status` is `unknown` or `match` and is set only by you; marking items as evidence never changes it. `author_type` is chosen from the list described above, and a value outside the list is rejected.

In DBeaver, views have no primary key, so it asks for a unique key before it lets you edit: define a virtual key and pick `post_id` (for `review_queue` and `review_items`) or `author_id` (for `author_candidates`). In psql, `UPDATE` statements always work:

```sql
-- What is waiting to be reviewed:
SELECT post_id, username, text, parent_text FROM review_queue LIMIT 20;
```

```sql
-- Evidence (lead stays "no"), lead only, and neither:
UPDATE review_queue SET is_evidence = true WHERE post_id = '1001';
UPDATE review_queue SET is_lead = true WHERE post_id = '1002';
UPDATE review_queue SET is_evidence = false, is_lead = false WHERE post_id = '1003';
```

```sql
-- Authors with evidence that still need a decision; mark one as a match:
SELECT author_id, username, evidence_count FROM author_candidates;
UPDATE author_candidates SET author_status = 'match' WHERE author_id = '501';
```

```sql
-- Give an author a type (the value must be in the list); their items leave the queue:
UPDATE review_queue SET author_type = 'organization' WHERE author_id = '502';
```

```sql
-- Take a review back (the item returns to the queue):
UPDATE review_items SET is_evidence = NULL, is_lead = NULL WHERE post_id = '1001';
```

```sql
-- Effort and yield per URL:
SELECT canonical_url, actions_spent, items, evidence, evidence_per_100_actions
FROM collection_yield ORDER BY evidence_per_100_actions DESC NULLS LAST;
```

## Operator checklist: protecting the Threads account

Threads has no public API for this, so collection drives a real Chrome window under a dedicated, logged-in account. Meta's terms prohibit automated collection, and no measure removes the risk of the account being restricted or banned. These habits keep it as low as possible. When a choice trades ban risk against speed or convenience, choose lower risk.

- **Use a dedicated account** that you can afford to lose, never your personal one.
- **Warm the account up by hand for one to two weeks before the first run**: complete and verify the profile, follow some accounts, and scroll the feed now and then. Keep using it by hand occasionally afterwards.
- **Use the same machine and home connection every time**, with no VPN or proxy, and run `threadsift login` only on the machine that runs collection. Its time zone, language and IP should be the real ones. An account that suddenly appears on a new device or address is a common trigger for challenges.
- **Leave generous gaps between runs.** There is no daily limit, so how often you start a run is the strongest lever you have. Runs are only ever started by you.
- **If Chrome offers "Restore pages?"** it believes the last session ended in a crash (the profile's `Default/Preferences` says `"exit_type":"Crashed"`). Ignore the bubble (do not click it during a run). The app waits for Chrome to finish shutting down before it exits, so a normal run or `login` (end it by closing the window, not with Ctrl+C) clears the mark; if it was left by older versions, one clean run clears it. Check with `grep -o '"exit_type":"[^"]*"' .session/Default/Preferences`.
- **Use the same window size every time.** The app never forces a size, so the number of scrolls per batch of replies depends on the real window: a taller window needs fewer scrolls, and a narrow one makes posts taller. Threads shows posts in a fixed-width column, so the full screen width is not needed: the full height and part of the width (so you can work in other programs beside it) is fine. Keep it wide enough that Threads does not switch to its compact layout, keep other windows from covering it (a covered window can be slowed down by Chrome, and replies may stop loading), and keep the size the same from run to run. Set the size once by hand: Chrome reopens at the last size.
- **Leave the browser window alone during a run, but you can use other programs.** The app's mouse and scroll input goes to the page and does not move your real cursor, so working in other programs and using the mouse elsewhere is fine. Do not click, type or scroll in the browser window, close it, or minimize it: that disturbs the run (a minimized window can slow rendering and stall reply expansion). The window stays open and idle during the long breaks on purpose, so a run looks like one continuous session.
- **Respect the stops.** A run ends at the first sign of blocking (a login wall, a challenge, rate limiting) or of quiet throttling, and a cool-down follows (24 hours after blocking, 6 hours after an early warning). Do not override the cool-down unless you have a good reason.
- **Keep the session private.** The saved session lives in `.session/`, which is git-ignored, and stays on the machine that runs collection. It holds the logged-in account: never copy, share or commit it.
- **Run headless only if you must.** A visible window is less detectable; `browser.headless = true` is available but not recommended.

The app only reads. It never likes, follows, replies, reposts or otherwise acts on Threads.

## Test fixtures

The extractor is tested against anonymized copies of real Threads payloads in `tests/fixtures/<case>/`, one folder per case (a post with replies, reply pages, nested replies, media, a quote, a URL pointing to a reply, a post without replies). Each file holds `{"url", "kind", "data"}`, the same format `run --capture` writes. No test contacts Threads.

To refresh them when Threads changes its format, save pages from your normal browser (DevTools, Network tab, "Save all as HAR with content", with "Preserve log" on) into `threadsift-har/`, adjust the `CASES` table at the top of `tools/har_to_fixtures.py`, and run:

```
uv run python tools/har_to_fixtures.py threadsift-har tests/fixtures
```

The tool keeps only post payloads, replaces every user, post and code with synthetic ones, replaces captions with synthetic Ukrainian and Russian text, and drops tokens, profile text and signed media links. HAR files hold your session: they are git-ignored, so never commit or share them.

## Running collection

After `threadsift login` and `threadsift import`, start a run yourself (runs are never started automatically, and there is no daily limit):

```
uv run threadsift run
```

A run first browses the home feed for 1 to 3 minutes, as a person opening the app would. Then it takes pending URLs oldest first, one at a time, with one browser and a pause of 30 to 90 seconds between URLs, and a longer break of 3 to 8 minutes after every 10 to 15 URLs. The browser stays open through the pauses. Each URL's post and replies are stored as they load, so nothing already collected is lost if the run stops. The end-of-run summary shows how the run ended, how many URLs ended done, partial, failed or still pending, and the page actions spent.

A run ends at the first of these, and the summary says which:

| Ending | Meaning |
|---|---|
| `completed` | Nothing more is pending. |
| `url_limit` | `limits.urls_per_run` URLs were processed; the rest stay pending. |
| `action_budget` | The run budget (`limits.run_action_budget`) can no longer cover a full URL (`limits.url_action_budget`). The next URL stays pending, and no URL is ever cut off by the run budget. |
| `blocked` | A login wall, a challenge or verification, rate limiting, or a logged-out session was detected. The run stops at once without retrying; the current URL stays pending with the reason (for example `blocked: challenge`). |
| `early_warning` | Signs of quiet throttling: reveals that load nothing, failing or empty responses, or pages getting much slower. The summary names the signs; a response counts as failing when Threads answers a GraphQL call with an error status (replies that are not one JSON document, which the page's background widgets and subscriptions send all the time, are not counted); for failing responses the summary also lists what failed (status, the operation's name and the kind of reply, never their contents), and the same list is stored with the run (`stop_details` in `runs`). The current URL stays pending. |
| `interrupted` | You pressed Ctrl+C. The run stops within seconds, keeps the data and leaves the current URL pending. |
| `error` | An unexpected error. The run stops, so an unknown condition never causes a quick series of failures. |

A URL that cannot be collected for another reason (deleted, private, or data that cannot be understood) is marked failed with the reason and the run continues. A URL cut off by a limit is marked partial and names the limit (`cap: action budget`, `cap: depth 5`, `cap: 1000 replies`).

### Cool-downs

After a run ends `blocked`, no new run starts for 24 hours; after `early_warning`, for 6 hours (`cooldown.blocked_hours`, `cooldown.early_warning_hours`). The summary says when runs are possible again, and a refused start says how long is left without opening Threads at all. Only the latest finished run decides. Starting anyway is possible but raises the risk for the account, and the run log marks it:

```
uv run threadsift run --override-cooldown
```

### The run log

```
uv run threadsift runs
uv run threadsift runs --limit 5
```

Each row shows when a run started, how long it took, how it ended, the page actions spent, and the URLs done, partial, failed and still pending. A `*` marks a run started during a cool-down. How much effort each URL cost is in the `collection_yield` view (see Reviewing).

### Saving payloads for fixtures

```
uv run threadsift run --capture .capture
```

collects only the first pending URL and saves everything it captured to `.capture/<shortcode>/` (git-ignored), in the format of `tests/fixtures/`. See Test fixtures.

Exit codes: 0 when the run completed, hit its URL limit or action budget; 1 for `blocked`, `early_warning`, `error`, a refused start or a missing session; 130 for Ctrl+C.

## First live check

Do this once, on the machine that runs collection, before relying on the app. It confirms the login, the warm-up, that the run only reads, and how scrolling loads replies, and it saves what Threads sent as new material for tests.

1. **Prepare:** the database is migrated (`db-upgrade`), a trait is set, and the account has been warmed up by hand (see the operator checklist). Set the browser window to the size you will always use, with the full height and part of the width, and keep other windows from covering it.
2. **Log in:** `uv run threadsift login`, log in by hand, close the window. Run `login` again: the window should open already logged in (Chrome keeps the profile). Close it.
3. **Pick one real post** with a few dozen replies, not a viral one, and import it: `uv run threadsift import one.txt`.
4. **Run it with capture:** `uv run threadsift run --capture .capture`. You should see the home feed scroll for 1 to 3 minutes, then the post open, then slow scrolling and now and then another reply's page. Leave the window alone.
5. **Check the result:**
   - The summary says how the run ended. `uv run threadsift runs` lists it.
   - The account shows nothing new: no likes, follows, replies or reposts.
   - `post` rows have parents and depths (`SELECT code, parent_id, depth FROM posts ORDER BY depth`), and `review_queue` lists the items.
   - `.capture/<shortcode>/scroll-log.json` has one entry per scroll that loaded replies: `scroll_in_batch` counts the scrolls it took to get a batch (the entry with `new_items` above 0 closes a batch), and `px_to_end_before` is how far from the end of the page the window was.
6. **If the run stops** on `blocked` or `early_warning`, do not override the cool-down. Note the reason shown and look at `runs`.
7. **Report back:** the run summary, the scroll log, and anything that looked unlike a person using the app (long freezes, odd jumps, windows stealing focus).

### What Threads sends (observed in saved pages)

A post page loads the post, its ancestors and its first replies in the page itself, then these operations: `BarcelonaPostPageTargetQuery` (the post), `BarcelonaPostPageUpwardQuery` (ancestors), `BarcelonaPostPageDownwardQuery` (replies) and, while scrolling, `BarcelonaPostPageDirectRepliesRefetchQuery` (4 chains of replies per request, chained by cursor). Replies come as chains; a chain continues only through a reply that has exactly one reply. If the live check shows other operations or a different order, note them here.

First live check (one post that ended up with 42 stored replies, 23 of them loaded by scrolling and the rest delivered with the page or collected from reply pages; a window of ordinary size, run on the collection machine): every scroll gesture that was made while the page was still far from its end loaded a new batch of replies, so one gesture per batch, and batches arrived while the page still had about 2,300 to 3,000 pixels left to scroll, roughly three screens ahead of the end. The run used 10 page actions in all, including the home-feed browse. This is one post and one window size, so treat it as a first reading rather than a rule.

## Tests

`uv run pytest` needs no installed Postgres: it starts a temporary server with `pgserver` and never contacts Threads. To run the suite once against the Postgres the app uses (the SQL targets Postgres 14 and later), point it at the server; the tests create and drop their own database there, so the URL needs a role that may create databases (not the `threadsift` role). With `postgresql:///postgres` Postgres connects as your own Linux or macOS user name, so that role must exist. On macOS with Homebrew it already does. On Linux, create it once; a superuser role runs the whole suite (one test, which creates and switches to a temporary role, is skipped for a role that can only create databases):

```
sudo -u postgres createuser --superuser $USER
```

Then, on one line (the variable and the command together, or `export` the variable first; a variable set on its own line is not seen by pytest):

```
THREADSIFT_TEST_DATABASE_URL=postgresql:///postgres uv run pytest
```

The value is a connection URL, `postgresql://user:password@host:port/database` (or `postgresql:///postgres` for your own user over the local socket), not the `host:port:database:user:password` form of `~/.pgpass`. The last line of the test output says which server was used: `tests ran against: PostgreSQL 14.x ..., your server (THREADSIFT_TEST_DATABASE_URL)`, or `temporary bundled server` if the variable did not reach pytest.

### Tests with a real browser

Some behavior can only be checked with the real library, so one extra test file drives a real headless Chrome against a tiny local web server (never Threads): navigation and capture, scrolling, the click guard, the blocking guard, and closing the browser. It is off by default because it needs Google Chrome:

```
THREADSIFT_BROWSER_TESTS=1 uv run pytest tests/test_real_browser.py
```
