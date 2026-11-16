from django.db import migrations, models


class Migration(migrations.Migration):
    """The merchant ID a branch"s Grab outlet goes by, for the Grab stock sync.

    Additive only: a new column that defaults to blank, and a constraint that
    exempts blank, so no existing row can clash.
    """

    dependencies = [
        ("bravepos", "0091_tillbeamcharge_items"),
    ]

    operations = [
        migrations.AddField(
            model_name="branch",
            name="grab_merchant_id",
            field=models.CharField(blank=True, default="", max_length=32),
        ),
        migrations.AddConstraint(
            model_name="branch",
            constraint=models.UniqueConstraint(
                condition=models.Q(("grab_merchant_id", ""), _negated=True),
                fields=("grab_merchant_id",),
                name="branch_grab_merchant_id_unique_when_set",
            ),
        ),
    ]
