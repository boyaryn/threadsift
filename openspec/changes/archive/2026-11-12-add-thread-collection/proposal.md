# Proposal

## Why

The goal of the product is to find Threads users who have one chosen trait (for example, a positive attitude towards renewable energy) by looking at what they post. Before any automatic detection is possible, we need a reliable, low-risk way to collect posts with their full reply trees and a place to review them by hand. That collection and review base is also what possible later features, such as LLM classification or automatic URL discovery, would build on.

## What Changes

- Introduce the domain vocabulary:
  - **trait**: the characteristic being searched for.
  - **evidence**: a post or reply showing its author has the trait.
  - **lead**: a post or reply whose replies are likely to contain evidence.
  - **match**: an author confirmed to have the trait.
- Add a URL queue fed from a text file. URLs are normalized and deduplicated, and each one tracks its processing state. A URL that was already processed is refreshed only when it appears in an input file again.
- Add collection of posts and their full reply trees from Threads, using a dedicated logged-in account:
  - Minimizing the risk of the account being restricted or banned is the top priority and wins over speed and convenience.
  - Pacing is slow and human-like, one post at a time, with a per-run limit, longer breaks during a run, and a short browse of the home feed at the start of each run.
  - The app only reads and never acts on Threads (no likes, follows, replies or reposts).
  - Collection effort is limited by a page-action budget per URL and per run, with reply-count and depth caps as secondary limits. Hitting any limit is recorded.
  - The effort spent on each URL is recorded, and a yield list shows how much evidence each URL produced per unit of effort.
  - The whole run stops immediately on any sign of blocking (a login wall, a challenge or rate limiting) and also on early signs of quiet throttling. A cool-down period follows before the next run, longer after a block.
- Add a durable archive of posts, replies and authors:
  - Each item links to its direct parent and to the top-level post of its thread.
  - Re-collection adds new replies and updates text that can change, while keeping the first version of each post.
    - A refresh opens only the parts of a thread that can hold new replies, judged by Threads' reply counts against those stored at the last collection. Edits and counts in the parts it skips are not updated.
  - Nothing is ever deleted.
- Add a manual review workflow:
  - Every collected item starts unreviewed. A review sets two independent flags, evidence and lead, and records who reviewed it and when.
  - Each author has a hand-set status (unknown or match) and an optional hand-set type chosen from an editable list.
  - A review queue leaves out authors who are matches or have a type set.
  - Reviewing happens through editable views in a generic database client.
- Out of scope for this change: LLM-based classification, automatic URL discovery, visiting author profile pages, downloading media, a review web UI, and tracking authors who lose the trait.

## Capabilities

### New Capabilities

- `url-intake`: importing post URLs from a file into a processing queue, normalizing and deduplicating them, tracking per-URL state, and the rule for which URLs a run picks up (pending ones only, oldest first, up to the per-run limit; re-importing an already processed URL makes it pending again so it gets refreshed).
- `thread-collection`: collecting a post and its reply tree from Threads under a logged-in session, with human-like pacing and breaks, a home-feed warm-up, read-only behaviour, page-action budgets and tree caps, stopping the run on blocking or early warning signs, cool-downs, and a record of the effort spent per URL.
- `post-archive`: storing posts, replies, authors, media references and quoted-post references, with tree links, first and latest versions, first-seen and last-seen times, and merging repeated collections without deleting anything.
- `trait-review`: traits, per-item review state (unreviewed, or reviewed with evidence and lead flags plus who reviewed it and when), author status and type, the review queue, the collection yield list, and editable review views.

### Modified Capabilities

None. This is the first change in the project.

## Impact

- New Python application, `threadsift` (command-line interface), with browser automation, plus a database schema with migrations.
- Requires a dedicated Threads account. Its saved session data lives only on the machine that runs collection and must stay out of version control.
- No paid services. Everything runs on the operator's own machines.
- Ban risk is managed by the browser setup, human-like behaviour, pacing limits, stopping on blocking signals and the cool-down, plus an operator checklist for warming up and using the account. It cannot be eliminated entirely.
