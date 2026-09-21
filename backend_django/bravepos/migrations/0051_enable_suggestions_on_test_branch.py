"""Turn the upsell strip on for "test branch", and nowhere else.

0049 added the flag defaulting to False, which is what keeps this feature away
from every real shop.  But a flag nobody sets is a feature nobody sees, and
"remember to flip it in the backoffice after deploying" is the kind of step
that gets forgotten — leaving the whole thing dark and looking broken.

So the rollout is stated here instead, in the deploy itself: exactly one
branch, by name, chosen because it is the one the shop already uses for
trying things out.

Matching on the name rather than an id because ids differ between the
production database and any local copy, and a hardcoded UUID would silently
enable nothing — or worse, something else — depending on where this runs.
A shop with no branch of that name gets a no-op, which is the right outcome:
nothing switched on that nobody asked for.

Reversing this switches it back off, which is the honest inverse. It does not
restore the flag on any *other* branch, because this migration never touched
one.
"""
from django.db import migrations

BRANCH_NAME = "test branch"


def enable(apps, schema_editor):
    Branch = apps.get_model("bravepos", "Branch")
    Branch.objects.filter(name__iexact=BRANCH_NAME).update(suggestions_enabled=True)


def disable(apps, schema_editor):
    Branch = apps.get_model("bravepos", "Branch")
    Branch.objects.filter(name__iexact=BRANCH_NAME).update(suggestions_enabled=False)


class Migration(migrations.Migration):

    dependencies = [
        ("bravepos", "0050_orderitem_suggested"),
    ]

    operations = [
        migrations.RunPython(enable, disable),
    ]
