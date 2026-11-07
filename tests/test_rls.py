from sqlalchemy import text


def test_every_public_table_has_rls(db):
    """Supabase's REST API exposes public tables to anyone with the anon key.
    If this fails, add `ALTER TABLE <name> ENABLE ROW LEVEL SECURITY` to the migration."""
    with db() as s:
        exposed = (
            s.execute(
                text(
                    "SELECT relname FROM pg_class "
                    "WHERE relnamespace = 'public'::regnamespace AND relkind IN ('r', 'p') "
                    "AND NOT relrowsecurity"
                )
            )
            .scalars()
            .all()
        )
    assert exposed == [], f"Tables without RLS: {exposed}"
