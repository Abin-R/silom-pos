from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations, models


class AddIndexConcurrentlyWherePossible(AddIndexConcurrently):
    """``CREATE INDEX CONCURRENTLY`` on PostgreSQL; a plain ``CREATE INDEX``
    anywhere else (the SQLite the tests run on has no such thing)."""

    def database_forwards(self, app_label, schema_editor, from_state, to_state):
        if schema_editor.connection.vendor == "postgresql":
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
    movements; ``stock_stockmovement`` is large and busy, so the index is built
    ``CONCURRENTLY`` — no write lock — which Postgres only allows outside a
    transaction (``atomic = False``).  If a build is interrupted it leaves an
    INVALID index behind: drop ``stockmov_branch_created_idx`` and run this
    again.  Not applied by anything in this change.
    """

    atomic = False

    dependencies = [
        ("bravepos", "0092_grab_stock_sync"),
    ]

    operations = [
        AddIndexConcurrentlyWherePossible(
            model_name="stockmovement",
            index=models.Index(
                fields=["branch", "created_at"], name="stockmov_branch_created_idx"),
        ),
    ]
