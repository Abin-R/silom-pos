"""A customer with no phone number stores NULL, not "".

The till has been unable to save a customer without a number: the Add Customer
form treats an empty phone box as valid, posts ``phone: null`` for it, and the
column would not accept null — so the cashier got a 400 with the raw response
body in the alert.  The back-office customer form had the same fault from the
other side; it posts "" and the column took it, which is how the book ended up
with the string form.

Both are the same missing decision — what "no phone" is stored as — and this
settles it on NULL.  "" is an ordinary value that happens to be empty: two of
them are equal, they sort and compare like text, and a query for a number has
to remember to exclude them.  NULL is the database's own word for "nothing
here", and two of them are never equal, so any number of customers can have no
number while a real number can still be held to one customer.

Existing rows are converted after the column is widened to accept null, in that
order: the update would fail against the old NOT NULL column.

``Customer.save()`` folds "" to None from here on, so the two forms cannot both
come back through a screen that was written before this migration.
"""
from django.db import migrations, models


def blank_to_null(apps, schema_editor):
    """Every "" already in the book becomes NULL."""
    Customer = apps.get_model("bravepos", "Customer")
    Customer.objects.filter(phone="").update(phone=None)


def null_to_blank(apps, schema_editor):
    """And back, so the column can go NOT NULL again on the way down."""
    Customer = apps.get_model("bravepos", "Customer")
    Customer.objects.filter(phone__isnull=True).update(phone="")


class Migration(migrations.Migration):

    dependencies = [
        ("bravepos", "0045_merge_customers_by_phone"),
    ]

    operations = [
        migrations.AlterField(
            model_name="customer",
            name="phone",
            field=models.CharField(
                blank=True, default=None, max_length=32, null=True),
        ),
        migrations.RunPython(blank_to_null, null_to_blank),
    ]
