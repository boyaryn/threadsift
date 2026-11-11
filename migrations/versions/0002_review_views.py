"""Review views with INSTEAD OF UPDATE triggers, and the collection yield view.

These depend on the type of authors.type, so they live apart from 0001: the
operator can drop them (alembic downgrade 0001), recreate the author_type
enum to remove a value, and bring them back (see the main README).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-31
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

REVIEW_FUNCTION = """
CREATE FUNCTION review_view_update() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v_trait bigint;
    v_read_only text;
    v_has_post boolean := to_jsonb(NEW) ? 'post_id';
BEGIN
    -- Only these columns may be edited; changing any other one is an error.
    SELECT string_agg(n.key, ', ' ORDER BY n.key) INTO v_read_only
    FROM jsonb_each(to_jsonb(NEW)) AS n(key, value)
    JOIN jsonb_each(to_jsonb(OLD)) AS o(key, value) USING (key)
    WHERE n.key NOT IN ('is_evidence', 'is_lead', 'author_status', 'author_type')
      AND n.value IS DISTINCT FROM o.value;
    IF v_read_only IS NOT NULL THEN
        RAISE EXCEPTION 'cannot change read-only column(s): %', v_read_only
            USING HINT = 'Only is_evidence, is_lead, author_status and author_type can be edited.';
    END IF;

    SELECT id INTO v_trait FROM traits WHERE is_active;

    -- Item-level views carry post_id; author_candidates does not, so its rows
    -- must never reach the NEW.is_evidence / NEW.is_lead references below.
    IF v_has_post THEN
        IF NEW.is_evidence IS DISTINCT FROM OLD.is_evidence
           OR NEW.is_lead IS DISTINCT FROM OLD.is_lead THEN
            IF v_trait IS NULL THEN
                RAISE EXCEPTION 'no active trait' USING HINT = 'Run: threadsift trait set NAME';
            END IF;
            IF NEW.is_evidence IS NULL AND NEW.is_lead IS NULL THEN
                DELETE FROM post_reviews WHERE post_id = OLD.post_id AND trait_id = v_trait;
            ELSE
                INSERT INTO post_reviews (post_id, trait_id, is_evidence, is_lead,
                                          reviewer_kind, reviewer_name, reviewed_at)
                VALUES (OLD.post_id, v_trait, COALESCE(NEW.is_evidence, false),
                        COALESCE(NEW.is_lead, false), 'human', 'human', now())
                ON CONFLICT (post_id, trait_id) DO UPDATE SET
                    is_evidence = EXCLUDED.is_evidence,
                    is_lead = EXCLUDED.is_lead,
                    reviewer_kind = EXCLUDED.reviewer_kind,
                    reviewer_name = EXCLUDED.reviewer_name,
                    reviewed_at = EXCLUDED.reviewed_at;
            END IF;
        END IF;
    END IF;

    IF NEW.author_status IS DISTINCT FROM OLD.author_status THEN
        IF v_trait IS NULL THEN
            RAISE EXCEPTION 'no active trait' USING HINT = 'Run: threadsift trait set NAME';
        END IF;
        INSERT INTO author_trait_status (author_id, trait_id, status, updated_at)
        VALUES (OLD.author_id, v_trait, COALESCE(NEW.author_status, 'unknown'), now())
        ON CONFLICT (author_id, trait_id) DO UPDATE SET
            status = EXCLUDED.status,
            updated_at = EXCLUDED.updated_at;
    END IF;

    IF NEW.author_type IS DISTINCT FROM OLD.author_type THEN
        UPDATE authors SET type = NEW.author_type WHERE id = OLD.author_id;
    END IF;

    RETURN NEW;
END;
$$
"""

UPGRADE = [
    REVIEW_FUNCTION,
    # Every item with its thread context and, for the active trait, its review and
    # its author's status. A missing review row reads as NULL flags (unreviewed).
    """CREATE VIEW review_items AS
    SELECT p.id AS post_id,
           p.root_id,
           p.parent_id,
           p.depth,
           p.published_at,
           p.url,
           p.latest_text AS text,
           p.like_count,
           p.reply_count,
           a.id AS author_id,
           a.username,
           root.latest_text AS root_text,
           parent.latest_text AS parent_text,
           t.name AS trait,
           r.reviewer_kind,
           r.reviewer_name,
           r.reviewed_at,
           r.is_evidence,
           r.is_lead,
           COALESCE(s.status, 'unknown'::trait_status) AS author_status,
           a.type AS author_type
    FROM posts p
    JOIN authors a ON a.id = p.author_id
    LEFT JOIN posts root ON root.id = p.root_id
    LEFT JOIN posts parent ON parent.id = p.parent_id
    LEFT JOIN traits t ON t.is_active
    LEFT JOIN post_reviews r ON r.post_id = p.id AND r.trait_id = t.id
    LEFT JOIN author_trait_status s ON s.author_id = a.id AND s.trait_id = t.id""",
    """CREATE TRIGGER review_items_update INSTEAD OF UPDATE ON review_items
    FOR EACH ROW EXECUTE FUNCTION review_view_update()""",
    # Unreviewed items of authors who are neither a match nor given a type.
    """CREATE VIEW review_queue AS
    SELECT * FROM review_items
    WHERE is_evidence IS NULL
      AND author_status <> 'match'
      AND author_type IS NULL
    ORDER BY root_id, depth, published_at, post_id""",
    """CREATE TRIGGER review_queue_update INSTEAD OF UPDATE ON review_queue
    FOR EACH ROW EXECUTE FUNCTION review_view_update()""",
    # Authors with evidence who still need a decision.
    """CREATE VIEW author_candidates AS
    SELECT a.id AS author_id,
           a.username,
           count(*) AS evidence_count,
           COALESCE(s.status, 'unknown'::trait_status) AS author_status,
           a.type AS author_type
    FROM authors a
    JOIN traits t ON t.is_active
    JOIN posts p ON p.author_id = a.id
    JOIN post_reviews r ON r.post_id = p.id AND r.trait_id = t.id AND r.is_evidence
    LEFT JOIN author_trait_status s ON s.author_id = a.id AND s.trait_id = t.id
    WHERE COALESCE(s.status, 'unknown'::trait_status) = 'unknown'
      AND a.type IS NULL
    GROUP BY a.id, a.username, s.status, a.type
    ORDER BY count(*) DESC, a.username, a.id""",
    """CREATE TRIGGER author_candidates_update INSTEAD OF UPDATE ON author_candidates
    FOR EACH ROW EXECUTE FUNCTION review_view_update()""",
    # One row per queued URL: effort spent and what it produced.
    """CREATE VIEW collection_yield AS
    SELECT q.id AS url_id,
           q.canonical_url,
           q.state,
           q.last_attempt_at,
           COALESCE(at.attempts, 0) AS attempts,
           COALESCE(at.actions_spent, 0) AS actions_spent,
           COALESCE(s.items, 0) AS items,
           COALESCE(s.reviewed, 0) AS reviewed,
           COALESCE(s.evidence, 0) AS evidence,
           COALESCE(s.leads, 0) AS leads,
           CASE WHEN COALESCE(at.actions_spent, 0) > 0
                THEN round(100.0 * COALESCE(s.evidence, 0) / at.actions_spent, 2)
           END AS evidence_per_100_actions
    FROM url_queue q
    LEFT JOIN (
        SELECT url_id, count(*) AS attempts, sum(actions_spent) AS actions_spent
        FROM url_attempts
        GROUP BY url_id
    ) at ON at.url_id = q.id
    LEFT JOIN (
        WITH RECURSIVE tree(url_id, post_id) AS (
            SELECT q2.id, q2.post_id FROM url_queue q2 WHERE q2.post_id IS NOT NULL
            UNION ALL
            SELECT tree.url_id, p.id FROM tree JOIN posts p ON p.parent_id = tree.post_id
        )
        SELECT tree.url_id,
               count(*) AS items,
               count(r.post_id) AS reviewed,
               count(*) FILTER (WHERE r.is_evidence) AS evidence,
               count(*) FILTER (WHERE r.is_lead) AS leads
        FROM tree
        LEFT JOIN post_reviews r
               ON r.post_id = tree.post_id
              AND r.trait_id = (SELECT id FROM traits WHERE is_active)
        GROUP BY tree.url_id
    ) s ON s.url_id = q.id""",
]

DOWNGRADE = [
    "DROP VIEW collection_yield",
    "DROP VIEW author_candidates",
    "DROP VIEW review_queue",
    "DROP VIEW review_items",
    "DROP FUNCTION review_view_update()",
]


def upgrade() -> None:
    for statement in UPGRADE:
        op.execute(statement)


def downgrade() -> None:
    for statement in DOWNGRADE:
        op.execute(statement)
