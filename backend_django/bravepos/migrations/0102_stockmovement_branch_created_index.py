from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


def _drop_invalid_leftover(schema_editor, name):
    """An interrupted ``CREATE INDEX CONCURRENTLY`` leaves an INVALID index of
    the same name behind, and every later ``migrate`` would then fail with
    "relation already exists".  Drop such a leftover first, so re-running the
    deploy is enough.  A valid index is never touched."""
    with schema_editor.connection.cursor() as cursor:
        cursor.execute(
            "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
            "WHERE c.relname = %s AND NOT i.indisvalid", [name])
        invalid = cursor.fetchone() is not None
    if invalid:
        schema_editor.execute(f'DROP INDEX CONCURRENTLY IF EXISTS "{name}"')


class AddIndexConcurrentlyWherePossible(AddIndexConcurrently):
    """``CREATE INDEX CONCURRENTLY`` on PostgreSQL; a plain ``CREATE INDEX``
    anywhere else (the SQLite the tests run on has no such thing)."""

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.vendor == "postgresql":
            _drop_invalid_leftover(schema_editor, self.index.name)
            return super().database_forwards(app_label, schema_editor, from_state, to_state)
        return migrations.AddIndex.database_forwards(
            self, app_label, schema_editor, from_state, to_state)

    def database_backwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.vendor == "postgresql":
            return super().database_backwards(app_label, schema_editor, from_state, to_state)
        return migrations.AddIndex.database_backwards(
            self, app_label, schema_editor, from_state, to_state)


class Migration(migrations.Migration):
    """An index for ``StockMovement`` by (branch, created_at).

    The stock read behind the Grab sync looks back over a branch's recent
    movements; ``bravepos_stockmovement`` is large and busy, so the index is
    built ``CONCURRENTLY`` — no write lock, sales keep going — which Postgres
    only allows outside a transaction (``atomic = False``).

    ``deploy.sh`` applies this automatically.  A concurrent build waits for
    every transaction already open on the database (other apps share it), so
    a long-running or idle-in-transaction session elsewhere makes ``migrate``
    wait; the old code keeps serving meanwhile.  If the build is interrupted,
    the INVALID leftover is dropped on the next run (see
    ``_drop_invalid_leftover``).
    """

    atomic = False

    dependencies = [
        ("bravepos", "0101_grab_stock_sync"),
    ]

    operations = [
        AddIndexConcurrentlyWherePossible(
            model_name="stockmovement",
            index=models.Index(
                fields=["branch", "created_at"], name="stockmov_branch_created_idx"),
        ),
    ]
