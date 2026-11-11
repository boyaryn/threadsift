# url-intake Specification

## Purpose

Gets post URLs into the system and decides which of them a collection run processes, so that collection is driven by an explicit, auditable queue. Today the queue is fed from a file; other sources can be added later.

## Requirements

### Requirement: Import URLs from a text file
The system SHALL let the operator import post URLs from a plain text file with one URL per line. Blank lines and lines starting with `#` SHALL be ignored. Each import SHALL report how many URLs were newly added, how many were already pending, how many were set back to pending for a refresh, and how many were rejected.

#### Scenario: Importing a file of new URLs
- **WHEN** the operator imports a file containing three valid Threads post URLs not seen before
- **THEN** all three URLs are added to the queue as pending and the report shows 3 added and nothing in the other counts

#### Scenario: Comments and blank lines are skipped
- **WHEN** the file contains blank lines and lines starting with `#` between URLs
- **THEN** those lines are ignored and not reported as rejected

### Requirement: Reject lines that are not post URLs
The system SHALL reject any line that is not a Threads post URL, for example a profile URL, a URL on another site, or text that is not a URL. Rejected lines SHALL be listed in the import report with a reason and SHALL NOT enter the queue.

#### Scenario: Profile URL is rejected
- **WHEN** the file contains `https://www.threads.com/@someuser`
- **THEN** the line is reported as rejected because it is not a post URL, and nothing is queued for it

### Requirement: Normalize URLs to one canonical form
The system SHALL convert every accepted URL to a canonical form, so that different spellings of the same post count as one queue entry. Host variants (`threads.net`, `threads.com`, with or without `www.`), query strings, fragments and trailing slashes SHALL NOT produce separate entries.

#### Scenario: Variants of the same post are deduplicated
- **WHEN** the operator imports `https://threads.net/@user/post/ABC123?igshid=x` and `https://www.threads.com/@user/post/ABC123/`
- **THEN** the queue contains a single entry for post `ABC123`

### Requirement: Track processing state per URL
The system SHALL record a state for each queued URL: pending, done, partial (collected but incomplete, for example because a cap was hit), or failed. It SHALL also record the time of the last attempt and, for partial or failed URLs, a human-readable reason.

#### Scenario: Failure is recorded with a reason
- **WHEN** collection of a queued URL fails because the post no longer exists
- **THEN** the URL's state becomes failed and its reason says the post was not found

### Requirement: Runs process only pending URLs
A collection run SHALL process only URLs in state pending, oldest first, up to the per-run limit. URLs left over when the limit is reached SHALL stay pending for the next run. Done, partial and failed URLs SHALL NOT be processed.

#### Scenario: Processed URL is not collected again on its own
- **WHEN** a URL is in state done and has not been imported again
- **THEN** no run collects it again

#### Scenario: Oldest pending URLs go first
- **WHEN** URLs were queued on Monday and on Tuesday and the run can process only some of them
- **THEN** the Monday URLs are processed first and the unprocessed ones stay pending

### Requirement: Re-listing a processed URL requests a refresh
Importing a URL that is already done, partial or failed SHALL set it back to pending, so the next run collects it again and picks up new replies. The import report SHALL count these URLs as refresh requested. A pending URL imported again SHALL remain a single pending entry.

#### Scenario: Re-listed URL is refreshed
- **WHEN** a URL in state done appears in a newly imported file
- **THEN** its state becomes pending, the report counts it as refresh requested, and the next run collects it again

### Requirement: Remember where each URL came from
The system SHALL record, for each queued URL, its source (for this version, the name of the imported file) and when it was first queued, so that URLs from future sources can be told apart.

#### Scenario: Source is recorded
- **WHEN** a URL is first imported from `batch-01.txt`
- **THEN** the queue entry records `batch-01.txt` as its source and the import time as first queued
