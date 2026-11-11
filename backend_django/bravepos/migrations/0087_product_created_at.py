from django.db import migrations, models


class Migration(migrations.Migration):
    """Product.created_at, for the catalogue's "Newest" sort.

    Added as a plain nullable column first and only then given auto_now_add:
    adding an auto_now_add column in one step makes Django stamp every
    existing row with the migration's own time, which would be a creation
    date nobody had — those rows stay null instead.
    """

    dependencies = [
        ('bravepos', '0086_remove_product_type'),
    ]

    operations = [
        migrations.AddField(
            model_name='product',
            name='created_at',
            field=models.DateTimeField(null=True, blank=True),
        ),
        migrations.AlterField(
            model_name='product',
            name='created_at',
            field=models.DateTimeField(auto_now_add=True, null=True, blank=True),
        ),
    ]
