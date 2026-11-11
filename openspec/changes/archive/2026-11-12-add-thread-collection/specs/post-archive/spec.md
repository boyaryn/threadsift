# Spec Delta

## Purpose

Keeps a durable, growing record of collected posts, replies and their authors. Every item's place in its thread can be reconstructed, and repeated collection only ever adds information.

## ADDED Requirements

### Requirement: One record per post or reply
The system SHALL treat top-level posts and replies at any depth as the same kind of item, stored once and identified by its Threads identifier. Collecting the same item again SHALL update the existing record, not create a new one.

#### Scenario: Same reply seen from two URLs
- **WHEN** a reply is collected while processing a top-level post and again while processing a URL that points to that reply
- **THEN** the archive holds a single record for the reply

### Requirement: Link each item to its parent and its top-level post
Each item SHALL record its direct parent (none for a top-level post) and the top-level post of its thread (itself for a top-level post), so that both the full tree and the thread an item belongs to can be found. If an item's parent was never collected, the item SHALL still record the parent's identifier. If its top-level post cannot be determined from the collected data, that link SHALL be left empty.

#### Scenario: Restoring the chain
- **WHEN** reply C replies to B, which replies to A, which replies to post P
- **THEN** following parents from C gives B, A, P in order, and all four items record P as their top-level post

#### Scenario: Parent not collected
- **WHEN** a reply is collected but its parent was cut off by a tree cap or is unavailable
- **THEN** the reply is stored with the parent's identifier and its top-level post, and still appears in that thread

### Requirement: Leave unknown links empty and fill them in later
If the parent of an item cannot be determined from the collected data, the system SHALL leave both the parent link and the top-level post link empty. When a later collection learns them, the system SHALL fill them in, and SHALL NOT change links that were already known.

#### Scenario: Parent unknown
- **WHEN** a reply is collected and nothing in the collected data says what it replies to
- **THEN** the reply is stored with its parent and top-level post left empty, and a later collection that learns them fills them in without changing links that were already known

### Requirement: Store item content
For each item the system SHALL store its URL, text exactly as published (any language or script, including Cyrillic and emoji, without translation or normalization), author, publication time, like and reply counts, media as type plus URL, and any quoted or reposted post as its URL. Media files SHALL NOT be downloaded, and quoted posts SHALL NOT be queued.

#### Scenario: Ukrainian text with emoji
- **WHEN** a post's text is "Нарешті встановив сонячні панелі ☀️"
- **THEN** the stored text is identical, character for character

#### Scenario: Post quoting another post
- **WHEN** a collected post quotes another post
- **THEN** the quoted post's URL is stored on the item and the quoted post is not added to the URL queue

### Requirement: Store authors from collected items
The system SHALL store each author once, identified by their Threads user identifier, with username, display name, verified badge and profile picture URL as seen in collected items. When these values change, they SHALL be updated. Author profile pages SHALL NOT be visited.

#### Scenario: Author renames
- **WHEN** an author whose username was `sun_fan` is collected again with username `solar_fan`
- **THEN** the same author record now shows `solar_fan`, and all of their items still point to it

### Requirement: Keep the first and the latest version of edited text
When an item's text differs from what was stored before, the system SHALL keep the text first collected unchanged, store the new text as the latest version, and record when the change was detected. Intermediate versions need not be kept. Edits are detected only in items that a collection sees, so items in parts of a thread that a refresh skips keep their stored text.

#### Scenario: Post edited between collections
- **WHEN** a post first collected as "Installed solar panels" is collected again as "Installed solar panels, very happy!"
- **THEN** the archive shows the first version "Installed solar panels", the latest version "Installed solar panels, very happy!", and when the change was detected

#### Scenario: Edit in a skipped part of the thread
- **WHEN** a reply on a page that a refresh does not open was edited on Threads
- **THEN** its stored first and latest text stay unchanged

### Requirement: Record when items were seen
Each item and author SHALL record when it was first seen and when it was last seen by collection.

#### Scenario: Refresh updates last seen
- **WHEN** an existing post is collected again
- **THEN** its first-seen time is unchanged and its last-seen time is the time of the new collection

### Requirement: Never delete during collection
Collection SHALL only add items and update values that can change: latest text, counts, author details, last-seen times. It SHALL NOT delete items or authors, including items that no longer appear on Threads. It SHALL NOT change any review data.

#### Scenario: Reply removed on Threads
- **WHEN** a URL is refreshed and a previously stored reply is no longer shown on Threads
- **THEN** the stored reply and its review data remain unchanged

#### Scenario: New replies on refresh
- **WHEN** a URL is refreshed and 5 new replies have appeared since the last collection
- **THEN** the 5 new replies are added, each linked to its parent and top-level post, and start unreviewed
