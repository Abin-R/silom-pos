"""Pairings are learned once for the whole shop, not once per branch.

Per-branch learning left eleven of twelve branches permanently ignorant: only
Central Krabi rings up enough multi-item receipts (900 in 180 days) to clear
any threshold, so everywhere else the strip could only ever show bestsellers.
Pooling fixes that, and it is safe here because the catalogues are nearly
identical — 10 of the 12 branches carry the same 52 products.

Pooling requires the rules to be keyed by product *name* rather than by a
branch's own Product row, since each branch owns a separate row for the same
item. Hence ``consequent`` (a FK) becomes ``consequent_name`` (text), and
``antecedent_key`` widens to hold names instead of UUIDs.

Existing rows cannot be carried across — their meaning was "this branch's
product id" and there is no id-to-name mapping that stays true once the rule
is meant to apply at a different branch. They are deleted rather than
converted, and ``mine_suggestions`` rebuilds them from scratch on the next
run. Nothing is lost: the rules are derived data, re-computable from the order
history at any time.
"""

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("bravepos", "0051_enable_suggestions_on_test_branch"),
    ]

    operations = [
        # Derived data, rebuilt by the next mine_suggestions run. Clearing it
        # first keeps the schema change from leaving rows whose consequent is
        # silently blank — which would serve nothing while looking healthy.
        migrations.RunPython(
            lambda apps, se: apps.get_model(
                "bravepos", "SuggestionRule").objects.all().delete(),
            migrations.RunPython.noop,
        ),
        migrations.RemoveConstraint(
            model_name="suggestionrule",
            name="uniq_suggestion_rule",
        ),
        migrations.RemoveField(
            model_name="suggestionrule",
            name="consequent",
        ),
        migrations.AddField(
            model_name="suggestionrule",
            name="consequent_name",
            field=models.CharField(blank=True, default="", max_length=200),
        ),
        migrations.AlterField(
            model_name="suggestionrule",
            name="antecedent_key",
            field=models.CharField(blank=True, default="", max_length=420),
        ),
        migrations.AddConstraint(
            model_name="suggestionrule",
            constraint=models.UniqueConstraint(
                fields=("branch", "kind", "antecedent_key", "consequent_name"),
                name="uniq_suggestion_rule",
            ),
        ),
    ]
