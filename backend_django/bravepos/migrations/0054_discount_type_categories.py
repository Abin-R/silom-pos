from django.db import migrations, models


def forwards(apps, schema_editor):
    """A preset that was limited to picked products stays limited to them."""
    DiscountType = apps.get_model("bravepos", "DiscountType")
    DiscountType.objects.filter(all_products=False).update(applies_to="products")


def backwards(apps, schema_editor):
    DiscountType = apps.get_model("bravepos", "DiscountType")
    # A category preset has no product-list equivalent; the closest safe
    # reading is "not every product", which offers it on its picked products
    # (none) rather than widening it to the whole catalogue.
    DiscountType.objects.exclude(applies_to="all").update(all_products=False)


class Migration(migrations.Migration):

    dependencies = [
        ("bravepos", "0053_discount_types"),
    ]

    operations = [
        migrations.AddField(
            model_name="discounttype",
            name="applies_to",
            field=models.CharField(
                choices=[
                    ("all", "All products"),
                    ("products", "Selected products"),
                    ("categories", "Selected categories"),
                ],
                default="all",
                max_length=12,
            ),
        ),
        migrations.AddField(
            model_name="discounttype",
            name="categories",
            field=models.ManyToManyField(
                blank=True, related_name="discount_types", to="bravepos.category",
            ),
        ),
        migrations.RunPython(forwards, backwards),
        migrations.RemoveField(
            model_name="discounttype",
            name="all_products",
        ),
    ]
