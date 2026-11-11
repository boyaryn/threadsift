# Spec Delta

## Purpose

Lets a reviewer label collected items and authors for the trait being searched for, and focuses review effort on authors who have not been decided yet. Labels made by hand can be told apart from labels made by any automated reviewer that may be added.

## ADDED Requirements

### Requirement: Labels belong to a trait
The system SHALL keep traits, each with a short name and a free-text description. Item reviews and author statuses SHALL belong to a trait. This version SHALL work with exactly one active trait, configured before review starts.

#### Scenario: Defining the trait
- **WHEN** the operator defines the trait "pro-renewables" with the description "positive attitude towards renewable energy sources"
- **THEN** reviews and author statuses created afterwards belong to "pro-renewables"

### Requirement: Items start unreviewed
Every collected item SHALL start unreviewed for the active trait, including replies added by a later refresh.

#### Scenario: Newly collected post
- **WHEN** a post is collected for the first time
- **THEN** its review state for the active trait is unreviewed

### Requirement: A review sets independent evidence and lead flags
Reviewing an item SHALL set two independent yes/no flags: evidence (the item shows its author has the trait) and lead (replies to the item are likely to be evidence). An item can therefore be reviewed as evidence, lead, both, or neither. A reviewer SHALL be able to set a reviewed item back to unreviewed.

#### Scenario: Reviewed as lead only
- **WHEN** the reviewer reviews "What do you think of using natural gas vs solar panels for heating houses?" as lead but not evidence
- **THEN** the item is reviewed with lead = yes and evidence = no

#### Scenario: Reviewed as neither
- **WHEN** the reviewer reviews an off-topic item and sets neither flag
- **THEN** the item is reviewed with both flags = no and no longer counts as unreviewed

### Requirement: Record who reviewed and when
Each review SHALL record when it was made and what kind of reviewer made it (human or automated), plus an optional reviewer name. Reviews made through the review views SHALL be recorded as human reviews automatically.

#### Scenario: Review through a view
- **WHEN** the reviewer sets the evidence flag on an item in a review view
- **THEN** the review is stored as a human review with the current time

### Requirement: Author status is set by hand and independent of evidence
Each author SHALL have a status for the active trait, either unknown or match, defaulting to unknown. Only the reviewer SHALL change it. Marking an author's items as evidence SHALL NOT change the author's status.

#### Scenario: Evidence does not promote the author
- **WHEN** the reviewer marks two items by an unknown author as evidence
- **THEN** the author's status remains unknown until the reviewer sets it to match

### Requirement: Author type from an editable list
Each author SHALL have an optional type, empty by default, chosen from a list of allowed values. The reviewer SHALL be able to add values to the list without changing the application. The list SHALL start empty. Setting a type outside the list SHALL be rejected. The type SHALL be a property of the author, not of a trait.

#### Scenario: Value not in the list
- **WHEN** the reviewer sets an author's type to a value that is not in the list
- **THEN** the change is rejected and the author's type is unchanged

#### Scenario: New value added
- **WHEN** the reviewer adds "organization" to the list and sets an author's type to "organization"
- **THEN** the change is accepted

### Requirement: Review queue excludes decided authors
The system SHALL provide a review queue of unreviewed items for the active trait. It SHALL leave out items whose author is a match or has a type set. Each item SHALL be shown with its thread context: its top-level post, its direct parent's text, and its author. Items of the same thread SHALL be grouped together in tree order.

#### Scenario: Matched author leaves the queue
- **WHEN** the reviewer sets an author's status to match
- **THEN** none of that author's unreviewed items appear in the review queue anymore

#### Scenario: Author with a type leaves the queue
- **WHEN** the reviewer sets an author's type to any allowed value
- **THEN** none of that author's unreviewed items appear in the review queue anymore

### Requirement: List authors awaiting a decision
The system SHALL provide a list of authors who have at least one item reviewed as evidence but whose status is still unknown and whose type is empty, with the number of their evidence items.

#### Scenario: Candidate author listed
- **WHEN** an unknown author with no type has one item reviewed as evidence
- **THEN** the author appears in the list with an evidence count of 1

### Requirement: Show collection yield per URL
The system SHALL provide a read-only list of collected URLs showing, for each: the total page actions spent on it, the number of items collected, and, for the active trait, how many of them are reviewed, evidence, or lead, and the evidence count per 100 actions. This shows which kinds of posts are worth the collection effort.

#### Scenario: Yield of a URL
- **WHEN** a URL took 200 actions over two attempts and 4 of its items are reviewed as evidence
- **THEN** the list shows 200 actions, 4 evidence items and 2 evidence items per 100 actions for that URL

### Requirement: Review by editing views in a generic database client
The reviewer SHALL be able to review by editing rows of the review views in a generic database client. Changes to the evidence flag, the lead flag, the review state, and the author's status and type SHALL be saved to the underlying data. Attempts to change collected content through a view SHALL be rejected.

#### Scenario: Editing a flag in the queue
- **WHEN** the reviewer sets the evidence flag on a row of the review queue and saves
- **THEN** the item's review is stored and the row leaves the queue

#### Scenario: Editing post text is rejected
- **WHEN** the reviewer edits the text column of a row in a review view
- **THEN** the change is rejected and the stored text is unchanged
