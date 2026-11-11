"""Initial schema: traits, authors, posts, reviews, URL queue, run log.

Revision ID: 0001
Revises:
Create Date: 2026-10-31
"""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

UPGRADE = [
    # Empty on purpose: the operator adds values with ALTER TYPE ... ADD VALUE.
    # Later migrations must never recreate or alter this type (migrations/README).
    "CREATE TYPE author_type AS ENUM ()",
    "CREATE TYPE trait_status AS ENUM ('unknown', 'match')",
    "CREATE TYPE reviewer_kind AS ENUM ('human', 'automated')",
    "CREATE TYPE url_state AS ENUM ('pending', 'done', 'partial', 'failed')",
    """CREATE TYPE run_end_reason AS ENUM (
        'completed', 'url_limit', 'action_budget', 'early_warning',
        'blocked', 'interrupted', 'error')""",
    """CREATE TABLE traits (
        id bigserial PRIMARY KEY,
        name text NOT NULL UNIQUE,
        description text NOT NULL DEFAULT '',
        is_active boolean NOT NULL DEFAULT false,
        created_at timestamptz NOT NULL DEFAULT now()
    )""",
    "CREATE UNIQUE INDEX traits_one_active ON traits (is_active) WHERE is_active",
    """CREATE TABLE authors (
        id text PRIMARY KEY,
        username text NOT NULL,
        display_name text,
        is_verified boolean NOT NULL DEFAULT false,
        profile_pic_url text,
        type author_type,
        first_seen_at timestamptz NOT NULL DEFAULT now(),
        last_seen_at timestamptz NOT NULL DEFAULT now()
    )""",
    """CREATE TABLE author_trait_status (
        author_id text NOT NULL REFERENCES authors (id),
        trait_id bigint NOT NULL REFERENCES traits (id),
        status trait_status NOT NULL DEFAULT 'unknown',
        updated_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (author_id, trait_id)
    )""",
    # parent_id and root_id deliberately have no foreign keys: a parent can be
    # missing (cut off by a cap or unavailable) and an ancestor can arrive later.
    """CREATE TABLE posts (
        id text PRIMARY KEY,
        code text NOT NULL,
        url text NOT NULL,
        author_id text NOT NULL REFERENCES authors (id),
        parent_id text,
        root_id text,
        depth integer,
        published_at timestamptz,
        first_text text NOT NULL,
        latest_text text NOT NULL,
        text_changed_at timestamptz,
        like_count integer,
        reply_count integer,
        media jsonb NOT NULL DEFAULT '[]'::jsonb,
        quoted_post_url text,
        first_seen_at timestamptz NOT NULL DEFAULT now(),
        last_seen_at timestamptz NOT NULL DEFAULT now()
    )""",
    "CREATE INDEX posts_code_idx ON posts (code)",
    "CREATE INDEX posts_author_id_idx ON posts (author_id)",
    "CREATE INDEX posts_parent_id_idx ON posts (parent_id)",
    "CREATE INDEX posts_root_id_idx ON posts (root_id)",
    """CREATE TABLE post_reviews (
        post_id text NOT NULL REFERENCES posts (id),
        trait_id bigint NOT NULL REFERENCES traits (id),
        is_evidence boolean NOT NULL,
        is_lead boolean NOT NULL,
        reviewer_kind reviewer_kind NOT NULL,
        reviewer_name text,
        reviewed_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (post_id, trait_id)
    )""",
    """CREATE TABLE url_queue (
        id bigserial PRIMARY KEY,
        code text NOT NULL UNIQUE,
        canonical_url text NOT NULL,
        state url_state NOT NULL DEFAULT 'pending',
        reason text,
        source text NOT NULL,
        first_queued_at timestamptz NOT NULL DEFAULT now(),
        last_attempt_at timestamptz,
        post_id text REFERENCES posts (id)
    )""",
    "CREATE INDEX url_queue_state_idx ON url_queue (state, first_queued_at, id)",
    """CREATE TABLE runs (
        id bigserial PRIMARY KEY,
        started_at timestamptz NOT NULL DEFAULT now(),
        ended_at timestamptz,
        end_reason run_end_reason,
        stop_details jsonb,
        cooldown_overridden boolean NOT NULL DEFAULT false,
        actions_spent integer NOT NULL DEFAULT 0,
        urls_done integer NOT NULL DEFAULT 0,
        urls_partial integer NOT NULL DEFAULT 0,
        urls_failed integer NOT NULL DEFAULT 0,
        urls_pending integer NOT NULL DEFAULT 0
    )""",
    """CREATE TABLE url_attempts (
        id bigserial PRIMARY KEY,
        run_id bigint NOT NULL REFERENCES runs (id),
        url_id bigint NOT NULL REFERENCES url_queue (id),
        started_at timestamptz NOT NULL DEFAULT now(),
        ended_at timestamptz,
        actions_spent integer NOT NULL DEFAULT 0,
        items_new integer NOT NULL DEFAULT 0,
        items_updated integer NOT NULL DEFAULT 0,
        outcome url_state,
        reason text
    )""",
    "CREATE INDEX url_attempts_url_id_idx ON url_attempts (url_id)",
    "CREATE INDEX url_attempts_run_id_idx ON url_attempts (run_id)",
]

DOWNGRADE = [
    "DROP TABLE url_attempts",
    "DROP TABLE runs",
    "DROP TABLE url_queue",
    "DROP TABLE post_reviews",
    "DROP TABLE posts",
    "DROP TABLE author_trait_status",
    "DROP TABLE authors",
    "DROP TABLE traits",
    "DROP TYPE run_end_reason",
    "DROP TYPE url_state",
    "DROP TYPE reviewer_kind",
    "DROP TYPE trait_status",
    "DROP TYPE author_type",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
