# Spec Delta

## Purpose

Collects Threads posts and their reply trees for queued URLs while keeping the risk of the collecting account being restricted or banned as low as possible.

## ADDED Requirements

### Requirement: Collect under a saved logged-in session
The system SHALL collect using a dedicated Threads account. The operator SHALL be able to log in once by hand in a visible browser window, and the system SHALL save that session and reuse it in later runs. Session data SHALL be stored only on the machine that runs collection, outside version control.

#### Scenario: First-time login
- **WHEN** the operator runs the login command and logs in by hand in the opened browser window
- **THEN** the session is saved and later runs start already logged in, without asking for credentials

#### Scenario: No saved session
- **WHEN** a collection run starts and no saved session exists
- **THEN** the run does not open any Threads page and tells the operator to run the login command first

### Requirement: Collect the post and its reply tree
For each URL it processes, the system SHALL collect the post the URL points to and its replies at every level, up to the tree caps. For each item it SHALL collect its text, author, publication time, like and reply counts, media references, a reference to any quoted post, and its direct parent.

#### Scenario: Nested replies keep their parents
- **WHEN** a post has reply A, A has reply B, and B has reply C
- **THEN** all four items are collected, with A's parent being the post, B's parent being A, and C's parent being B

#### Scenario: URL points to a reply
- **WHEN** the queued URL points to a reply rather than a top-level post
- **THEN** that reply, its own replies, and any of its parents that arrive with the reply's page are collected, and no extra pages are opened just to fetch parents

#### Scenario: Replies load in batches
- **WHEN** a post has more direct replies than its first page shows
- **THEN** scrolling the page loads the further replies in batches, and they are collected as they load until all are collected or a limit applies

### Requirement: Cap the size and depth of reply trees
The system SHALL stop expanding a reply tree once about 1000 replies have been collected for the URL, and SHALL NOT expand replies deeper than 5 levels below the top-level post. Both limits SHALL be configurable. If a limit cuts off any replies, the URL SHALL be marked partial, with the limit named in the reason.

#### Scenario: Depth cap reached
- **WHEN** a reply at depth 5 has its own replies
- **THEN** those deeper replies are not collected and the URL is marked partial with a reason naming the depth limit

#### Scenario: Tree within limits
- **WHEN** a post has 40 replies, none deeper than level 3
- **THEN** all 40 replies are collected and the URL is marked done

### Requirement: Skip unchanged parts of a thread on refresh
When a top-level post whose previous collection ended done is collected again, the system SHALL compare its reply count, which covers the whole thread, with the count stored at the last collection. If the count has not grown, it SHALL open no further page and not scroll. If it has grown, it SHALL expand only until it has found as many new replies as the count grew, opening new replies and those whose own count changed first, and the URL SHALL NOT become partial because of this early stop.

#### Scenario: Nothing new since the last collection
- **WHEN** a done URL is refreshed and its post still reports 42 replies, as stored
- **THEN** only the post's page is opened, 1 page action is spent, the counts on that page are updated and the URL is marked done

#### Scenario: A new reply deep in one branch
- **WHEN** a done URL is refreshed, the post's count went from 42 to 43, and the new reply is under a reply whose own count went from 2 to 3
- **THEN** that reply's page is opened, the new reply is collected, no other reply page is opened and the URL is marked done

### Requirement: Collect in full when skipping is not safe
A URL whose previous collection was partial or failed, a URL that has never been collected, and a URL whose post is a reply SHALL be collected in full, as on a first collection.

#### Scenario: Previously partial URL
- **WHEN** a URL whose last collection was partial with `cap: action budget` is refreshed
- **THEN** its reply tree is expanded in full, as on its first collection

#### Scenario: URL points to a reply
- **WHEN** a URL whose post is a reply is refreshed
- **THEN** the reply's tree is expanded in full, because a reply's count covers only its direct replies

### Requirement: Limit page actions per URL
The system SHALL count every page action: opening a page, each scroll step, and each click. It SHALL stop expanding a URL's reply tree once 150 actions (configurable) have been spent on it. This action budget SHALL be the main limit on collection effort, with the reply and depth caps as secondary limits. A URL cut off by its budget SHALL be marked partial, with the budget named in the reason.

#### Scenario: Deep thread hits its action budget
- **WHEN** expanding a large thread reaches 150 actions before the reply and depth caps are reached
- **THEN** expansion stops, the collected replies are kept, and the URL is marked partial with a reason naming the action budget

### Requirement: Limit page actions per run
The system SHALL spend at most 1200 page actions per run (configurable), counting the home-feed warm-up. Before starting a URL, the run SHALL check that the remaining run budget covers a full per-URL budget. If it doesn't, the run SHALL end normally with reason action budget reached, so no URL is ever cut off by the run budget.

#### Scenario: Not enough budget for another URL
- **WHEN** 1100 actions have been spent in the run and the per-URL budget is 150
- **THEN** the run ends with reason action budget reached and the next URL stays pending

### Requirement: Pace collection like a human
The system SHALL process one URL at a time with one browser, never in parallel. It SHALL wait a random 4–12 seconds between page actions (scrolling, expanding replies, opening a page) and a random 30–90 seconds between URLs. It SHALL stay longer on pages with more text, up to about a screenful of text, and SHALL move the pointer and scroll in a human-like way, about a screen at a time. All pause ranges SHALL be configurable.

#### Scenario: Pause between URLs
- **WHEN** the run finishes one URL and moves to the next
- **THEN** it waits a random time between 30 and 90 seconds before opening the next post

#### Scenario: Longer text, longer stay
- **WHEN** two pages are processed with the same random draws, one showing 2000 characters of text and the other 100
- **THEN** the run stays longer on the page with 2000 characters

#### Scenario: A very long page does not mean a very long stay
- **WHEN** a page has grown to 80,000 characters of text because many replies have loaded
- **THEN** the run stays no longer than it would on a page with about a screenful (2000 characters) of text

### Requirement: Take longer breaks during a run
After every random 10–15 processed URLs, the system SHALL pause for a random 3–8 minutes before continuing. Both ranges SHALL be configurable.

#### Scenario: Break after a batch of URLs
- **WHEN** the run has processed the number of URLs drawn for the current batch, for example 12
- **THEN** it pauses for a random time between 3 and 8 minutes, then draws a new batch size and continues

### Requirement: Start each run by browsing the home feed
Before opening the first queued URL, each run SHALL open the Threads home feed and scroll it for a short random time, 1–3 minutes by default (configurable). Nothing from the feed SHALL be stored or queued.

#### Scenario: Run warm-up
- **WHEN** a run starts with pending URLs
- **THEN** it first scrolls the home feed for 1–3 minutes, then opens the first pending URL

### Requirement: Never act on Threads
The system SHALL only read. It SHALL NOT like, follow, reply, repost, quote, bookmark, vote, or otherwise change anything on Threads or in the account.

#### Scenario: Expanding replies only reads
- **WHEN** the run expands a post's replies
- **THEN** it only scrolls and opens pages by their address, and never uses a control that acts on posts, authors or the account

### Requirement: Limit URLs per run
The system SHALL process at most a configurable number of URLs per run, about 60 by default, in addition to the per-run action budget. There SHALL be no daily limit, and runs SHALL be started only by the operator.

#### Scenario: Run limit reached
- **WHEN** a run has processed 60 URLs and more are pending
- **THEN** the run ends normally and the remaining URLs stay pending

### Requirement: Stop the run when blocking is detected
If the system sees a login wall, a challenge or verification request, a rate-limit response, or the session being logged out, it SHALL stop the whole run immediately without retrying. Data already collected SHALL be kept. The URL being processed SHALL stay pending with the reason recorded. The operator SHALL be told clearly what was detected.

#### Scenario: Challenge page appears mid-run
- **WHEN** a verification challenge appears while the run is processing a URL
- **THEN** the run stops at once, replies saved so far are kept, the URL stays pending with reason "blocked: challenge", and no further URLs are opened

### Requirement: Stop the run on early warning signs
The system SHALL watch for signs of quiet throttling during a run: reveals that load nothing, requests failing or returning empty, and pages getting much slower than earlier in the run. When these signs pass configurable thresholds, the run SHALL end with reason early warning, naming the signs seen. Data SHALL be kept and the current URL SHALL stay pending.

#### Scenario: Reveals stop working
- **WHEN** with a threshold of 3, three reveals in a row (scrolling at the end of the page, or opening a reply's page) load no new replies even though the reply counts say more exist
- **THEN** the run ends with reason early warning naming that sign, collected data is kept, and the current URL stays pending

#### Scenario: Scrolling through loaded replies is not a warning
- **WHEN** scrolls that have not yet reached the end of the page load nothing, because the replies are already loaded
- **THEN** the run continues

#### Scenario: Background replies that are not JSON are not failures
- **WHEN** the page's background calls answer with status 200 but with bodies that are not a single JSON document
- **THEN** they are not counted as failing responses and the run continues

#### Scenario: Error statuses are failures
- **WHEN** more than 30% of the last 20 GraphQL responses come back with an error status
- **THEN** the run ends with reason early warning, naming the sign and listing the failed responses

#### Scenario: Responses slow down
- **WHEN** recent pages take more than the configured multiple of the run's early load times
- **THEN** the run ends with reason early warning naming slow responses

#### Scenario: Normal gaps are not warnings
- **WHEN** a post has fewer visible replies than its reply count because some replies were deleted, and later reveals keep loading content
- **THEN** the run continues

### Requirement: Cool down after a blocked or warned run
After a run ends because blocking was detected, the system SHALL refuse to start another run for 24 hours. After a run ends on early warning, it SHALL refuse for 6 hours. Both periods SHALL be configurable. The operator SHALL be told when runs become possible again, SHALL be able to override explicitly, and an overridden start SHALL be marked in the run log.

#### Scenario: Run refused during cool-down
- **WHEN** the operator starts a run 3 hours after a run was stopped by a challenge
- **THEN** no Threads page is opened and the operator is told runs are possible again in about 21 hours

#### Scenario: Shorter cool-down after an early warning
- **WHEN** the operator starts a run 7 hours after a run ended on early warning
- **THEN** the run starts

#### Scenario: Explicit override
- **WHEN** the operator starts a run during the cool-down with the explicit override
- **THEN** the run starts and its run log entry is marked as started during a cool-down

### Requirement: Isolate failures of individual posts
If a single post cannot be collected for a reason other than blocking (for example, it was deleted, is private, or does not exist), the system SHALL mark that URL failed with the reason and continue with the next URL.

#### Scenario: Deleted post
- **WHEN** a queued post has been deleted
- **THEN** its URL is marked failed with reason "not found" and the run continues with the next pending URL

### Requirement: Stop gracefully when the operator interrupts
When the operator interrupts a run, the system SHALL stop without opening further pages, keep the data already collected, and leave the URL being processed pending.

#### Scenario: Operator interrupts
- **WHEN** the operator presses Ctrl+C during a run
- **THEN** the run ends within a few seconds, collected data is kept, and the current URL stays pending

### Requirement: Keep a log of runs
The system SHALL record each run's start and end time, whether it was started by overriding a cool-down, and how it ended. The possible endings are: completed, run limit reached, action budget reached, early warning, blocked, interrupted, or error. It SHALL also record the page actions spent and how many URLs ended done, partial, failed or still pending. It SHALL show this summary to the operator at the end of the run.

#### Scenario: Run summary
- **WHEN** a run ends after processing 10 URLs, 8 done and 2 partial
- **THEN** the summary shows 8 done and 2 partial with the end reason and the actions spent, and the same summary is stored in the run log

### Requirement: Record the effort spent on each URL
For every attempt at a URL, the system SHALL record the run, the page actions spent, the number of items newly added and updated, and the outcome.

#### Scenario: Attempt recorded
- **WHEN** a URL is collected in a run, spending 42 actions and adding 30 new replies
- **THEN** an attempt record for that URL shows the run, 42 actions, 30 new items and the outcome done
