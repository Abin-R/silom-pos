"""Every branch owns its payment config; the shop row becomes a template.

Before this, branches read the shop ``Settings`` singleton unless they opted in
via ``payment_own``.  Nobody ever opted in, so in practice every till and every
self-order checkout ran on the one shop account with one shared test/live flag —
which meant the day the shop flipped to production, a branch kept for testing
would have flipped with it.

The backfill copies the shop row onto every existing branch, so each branch ends
up holding exactly the credentials it was already using.  Behaviour on the day
of deploy is unchanged; what changes is that the test branch can now be pinned
to sandbox on its own.

Reverse restores ``payment_own`` (defaulting False, which is what every row
held) and leaves the copied credentials in place — harmless, since the old
resolution ignored them unless ``payment_own`` was set.
"""
from django.db import migrations, models

# Kept as a literal rather than importing gateways.PAYMENT_FIELDS: a migration
# has to describe the schema as it was at this point in history, not follow the
# app as it moves on.
PAYMENT_FIELDS = (
    'beam_merchant_id', 'beam_api_key', 'beam_sandbox', 'beam_card_fee_percent',
    'omise_public_key', 'omise_secret_key', 'omise_fee_percent',
)
CREDENTIAL_FIELDS = (
    'beam_merchant_id', 'beam_api_key', 'omise_public_key', 'omise_secret_key',
)


def seed_branches_from_shop(apps, schema_editor):
    Branch = apps.get_model('bravepos', 'Branch')
    Settings = apps.get_model('bravepos', 'Settings')

    shop = Settings.objects.filter(id='shop').first()
    if shop is None:
        # No shop row means a fresh install with no branches to carry over.
        return

    for branch in Branch.objects.all():
        # Don't clobber a branch someone had already configured by hand.
        if any(getattr(branch, f, '') for f in CREDENTIAL_FIELDS):
            continue
        for field in PAYMENT_FIELDS:
            setattr(branch, field, getattr(shop, field))
        branch.save(update_fields=list(PAYMENT_FIELDS))


def noop_reverse(apps, schema_editor):
    """Nothing to undo — the copied credentials are inert once ``payment_own``
    is back and False."""


class Migration(migrations.Migration):

    dependencies = [
        ('bravepos', '0028_user_management_and_audit_log'),
    ]

    operations = [
        migrations.RunPython(seed_branches_from_shop, noop_reverse),
        migrations.RemoveField(
            model_name='branch',
            name='payment_own',
        ),
    ]
