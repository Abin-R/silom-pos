"""Upsell suggestions (renumbered from 0038 when this rebased onto main): the mined rule table, admin overrides, and the
per-branch kill switch.

Two brand-new tables plus one defaulted boolean on ``bravepos_branch``
(a handful of rows).  Nothing here touches a hot table, so it is safe to
apply while the tills are trading.  The ``OrderItem`` column ships
separately in 0039 so it can be reverted on its own.
"""

from django.db import migrations, models
import django.db.models.deletion
import uuid


class Migration(migrations.Migration):

    dependencies = [
        ("bravepos", "0048_undo_tax_invoice_phone_writes"),
    ]

    operations = [
        migrations.AddField(
            model_name="branch",
            name="suggestions_enabled",
            field=models.BooleanField(default=False),
        ),
        migrations.CreateModel(
            name="SuggestionOverride",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "mode",
                    models.CharField(
                        choices=[("pin", "Always suggest"), ("block", "Never suggest")],
                        default="pin",
                        max_length=8,
                    ),
                ),
                ("sort_order", models.IntegerField(default=0)),
                ("note", models.CharField(blank=True, default="", max_length=200)),
                ("active", models.BooleanField(default=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "branch",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="suggestion_overrides",
                        to="bravepos.branch",
                    ),
                ),
                (
                    "product",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="bravepos.product",
                    ),
                ),
                (
                    "trigger",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="bravepos.product",
                    ),
                ),
            ],
            options={
                "ordering": ["sort_order", "created_at"],
            },
        ),
        migrations.CreateModel(
            name="SuggestionRule",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[
                            ("rule", "Association rule"),
                            ("pair", "Pairwise lift"),
                            ("popular", "Popularity"),
                        ],
                        default="pair",
                        max_length=8,
                    ),
                ),
                (
                    "antecedent_key",
                    models.CharField(blank=True, default="", max_length=80),
                ),
                ("antecedent_size", models.IntegerField(default=0)),
                ("support", models.FloatField(default=0)),
                ("confidence", models.FloatField(default=0)),
                ("lift", models.FloatField(default=0)),
                ("basket_count", models.IntegerField(default=0)),
                ("score", models.FloatField(default=0)),
                ("generated_at", models.DateTimeField(auto_now=True)),
                (
                    "branch",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="suggestion_rules",
                        to="bravepos.branch",
                    ),
                ),
                (
                    "consequent",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="bravepos.product",
                    ),
                ),
            ],
            options={
                "ordering": ["-score"],
                "indexes": [
                    models.Index(
                        fields=["branch", "antecedent_key", "-score"],
                        name="bravepos_su_branch__f64945_idx",
                    )
                ],
            },
        ),
        migrations.AddConstraint(
            model_name="suggestionrule",
            constraint=models.UniqueConstraint(
                fields=("branch", "kind", "antecedent_key", "consequent"),
                name="uniq_suggestion_rule",
            ),
        ),
        migrations.AddIndex(
            model_name="suggestionoverride",
            index=models.Index(
                fields=["branch", "active"], name="bravepos_su_branch__aab460_idx"
            ),
        ),
        migrations.AddConstraint(
            model_name="suggestionoverride",
            constraint=models.UniqueConstraint(
                condition=models.Q(("trigger__isnull", False)),
                fields=("branch", "mode", "trigger", "product"),
                name="uniq_suggestion_override_scoped",
            ),
        ),
        migrations.AddConstraint(
            model_name="suggestionoverride",
            constraint=models.UniqueConstraint(
                condition=models.Q(("trigger__isnull", True)),
                fields=("branch", "mode", "product"),
                name="uniq_suggestion_override_global",
            ),
        ),
    ]
