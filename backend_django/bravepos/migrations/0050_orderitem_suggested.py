"""Attribution for the upsell strip: did this order line start as a suggestion?

Split out of 0049 deliberately.  ``bravepos_orderitem`` is the hottest table in
the schema; on Postgres 11+ adding a column with a constant default is
metadata-only (no table rewrite), but the ALTER still takes a brief
ACCESS EXCLUSIVE lock.  Alone in its own migration it can be reverted without
taking the suggestion tables with it, and the deploy carrying it can be timed
outside trading hours.
"""
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("bravepos", "0049_suggestions"),
    ]

    operations = [
        migrations.AddField(
            model_name="orderitem",
            name="suggested",
            field=models.BooleanField(default=False),
        ),
    ]
