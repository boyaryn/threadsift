from dataclasses import dataclass

from sqlalchemy import Engine, text


@dataclass(frozen=True)
class Trait:
    id: int
    name: str
    description: str
    is_active: bool


def set_active_trait(engine: Engine, name: str, description: str | None = None) -> Trait:
    """Create the trait if needed and make it the single active one.

    The previously active trait (if different) is deactivated first, in the same
    transaction, because only one trait may be active at a time. A description
    given for an existing trait replaces its old one; omitting it keeps the old.
    """
    name = name.strip()
    if not name:
        raise ValueError("trait name must not be empty")
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE traits SET is_active = false WHERE is_active AND name <> :name"),
            {"name": name},
        )
        row = connection.execute(
            text(
                """
                INSERT INTO traits (name, description, is_active)
                VALUES (:name, COALESCE(:description, ''), true)
                ON CONFLICT (name) DO UPDATE SET
                    is_active = true,
                    description = COALESCE(:description, traits.description)
                RETURNING id, name, description, is_active
                """
            ),
            {"name": name, "description": description},
        ).one()
    return Trait(*row)


def get_active_trait(engine: Engine) -> Trait | None:
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT id, name, description, is_active FROM traits WHERE is_active")
        ).first()
    return Trait(*row) if row else None
