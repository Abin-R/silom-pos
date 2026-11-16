import datetime
from unittest.mock import patch

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from bravepos.audit import AUDITED
from bravepos.models import ProductionCutoffPolicy, Settings
from bravepos.production_policy import (
    deadline_provenance,
    earliest_open_date,
    is_open,
    latest_locked_date,
    locks_at,
    update_cutoff,
)
from bravepos.serializers import SettingsSerializer
from bravepos.tests.test_user_management_and_audit import make_user, sign_in


UTC_PLUS_SEVEN = timezone.get_current_timezone()


def aware(year, month, day, hour, minute=0):
    return timezone.make_aware(datetime.datetime(year, month, day, hour, minute), UTC_PLUS_SEVEN)


class ProductionCutoffPolicyTests(TestCase):
    delivery_day = datetime.date(2026, 11, 17)

    def setUp(self):
        Settings.objects.update_or_create(
            id="shop", defaults={"production_cutoff": datetime.time(18, 0)},
        )
        ProductionCutoffPolicy.objects.all().delete()
        ProductionCutoffPolicy.objects.create(
            effective_from=datetime.date.min,
            cutoff=datetime.time(18, 0),
            activated_at=aware(2020, 1, 1, 0),
            source=ProductionCutoffPolicy.Source.LEGACY_BASELINE,
        )

    def test_runtime_resolution_uses_latest_policy_id_for_same_boundary(self):
        ProductionCutoffPolicy.objects.create(
            effective_from=self.delivery_day,
            cutoff=datetime.time(17, 0),
            activated_at=aware(2026, 11, 1, 0),
            source=ProductionCutoffPolicy.Source.ADMIN_CHANGE,
        )
        latest = ProductionCutoffPolicy.objects.create(
            effective_from=self.delivery_day,
            cutoff=datetime.time(16, 0),
            activated_at=aware(2026, 11, 1, 0),
            source=ProductionCutoffPolicy.Source.ADMIN_CHANGE,
        )

        self.assertEqual(locks_at(self.delivery_day), aware(2026, 11, 16, 16))
        self.assertEqual(deadline_provenance(self.delivery_day), latest.source)

    def test_explicit_cutoff_keeps_arithmetic_compatibility(self):
        self.assertEqual(
            locks_at(self.delivery_day, cutoff=datetime.time(12, 15)),
            aware(2026, 11, 16, 12, 15),
        )
        self.assertFalse(is_open(self.delivery_day, aware(2026, 11, 16, 12, 15), datetime.time(12, 15)))

    def test_earlier_change_closes_an_open_date_at_activation_not_in_the_past(self):
        changed_at = aware(2026, 11, 16, 17, 30)
        with patch("bravepos.production_policy.timezone.now", return_value=changed_at):
            policy = update_cutoff(datetime.time(16, 0))

        self.assertEqual(policy.effective_from, self.delivery_day)
        self.assertEqual(locks_at(self.delivery_day), changed_at)
        self.assertFalse(is_open(self.delivery_day, changed_at))
        self.assertEqual(Settings.objects.get(id="shop").production_cutoff, datetime.time(16, 0))

    def test_change_does_not_reopen_date_closed_under_previous_schedule(self):
        changed_at = aware(2026, 11, 16, 19)
        with patch("bravepos.production_policy.timezone.now", return_value=changed_at):
            policy = update_cutoff(datetime.time(23, 0))

        self.assertEqual(policy.effective_from, self.delivery_day + datetime.timedelta(days=1))
        self.assertEqual(locks_at(self.delivery_day), aware(2026, 11, 16, 18))
        self.assertFalse(is_open(self.delivery_day, changed_at))
        self.assertTrue(is_open(self.delivery_day + datetime.timedelta(days=1), changed_at))

    def test_repeated_change_at_same_open_boundary_appends_policy_history(self):
        changed_at = aware(2026, 11, 16, 15)
        with patch("bravepos.production_policy.timezone.now", return_value=changed_at):
            first = update_cutoff(datetime.time(19, 0))
            second = update_cutoff(datetime.time(19, 0))

        self.assertEqual(first.effective_from, second.effective_from)
        self.assertLess(first.id, second.id)
        self.assertEqual(
            ProductionCutoffPolicy.objects.filter(
                effective_from=self.delivery_day,
                source=ProductionCutoffPolicy.Source.ADMIN_CHANGE,
            ).count(),
            2,
        )

    def test_date_helpers_honor_policy_deadlines(self):
        before_cutoff = aware(2026, 11, 16, 17, 59)
        at_cutoff = aware(2026, 11, 16, 18)

        self.assertEqual(earliest_open_date(before_cutoff), self.delivery_day)
        self.assertEqual(latest_locked_date(before_cutoff), datetime.date(2026, 11, 16))
        self.assertEqual(earliest_open_date(at_cutoff), self.delivery_day + datetime.timedelta(days=1))
        self.assertEqual(latest_locked_date(at_cutoff), self.delivery_day)

    def test_pos_settings_serializer_partial_update_keeps_concurrent_cutoff(self):
        settings_row = Settings.objects.get(id="shop")
        Settings.objects.filter(id="shop").update(production_cutoff=datetime.time(20, 0))
        serializer = SettingsSerializer(
            settings_row,
            data={"shop_name": "Changed from a stale POS form", "production_cutoff": "10:00:00"},
            partial=True,
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)
        serializer.save()
        settings_row.refresh_from_db()
        self.assertEqual(settings_row.shop_name, "Changed from a stale POS form")
        self.assertEqual(settings_row.production_cutoff, datetime.time(20, 0))

    def test_admin_shop_settings_appends_policy_after_validation(self):
        admin = make_user(username="cutoff-admin", email="cutoff-admin@example.com")
        sign_in(self.client, admin)
        changed_at = aware(2026, 11, 16, 15)

        with patch("bravepos.production_policy.timezone.now", return_value=changed_at):
            response = self.client.post(reverse("backoffice:shop_settings"), {
                "shop_name": "Policy Shop",
                "production_cutoff": "19:00",
                "production_cutoff_baseline": "18:00",
            })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            ProductionCutoffPolicy.objects.filter(
                source=ProductionCutoffPolicy.Source.ADMIN_CHANGE,
                effective_from=self.delivery_day,
            ).count(),
            1,
        )
        self.assertEqual(Settings.objects.get(id="shop").production_cutoff, datetime.time(19, 0))

    def test_stale_shop_cutoff_change_is_rejected_without_replaying_the_value(self):
        admin = make_user(username="stale-cutoff-admin", email="stale-cutoff-admin@example.com")
        sign_in(self.client, admin)
        changed_at = aware(2026, 11, 16, 15)
        with patch("bravepos.production_policy.timezone.now", return_value=changed_at):
            update_cutoff(datetime.time(20, 0))

        response = self.client.post(reverse("backoffice:shop_settings"), {
            "shop_name": "Stale form should not save",
            "production_cutoff": "19:00",
            "production_cutoff_baseline": "18:00",
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Nothing was saved")
        self.assertEqual(Settings.objects.get(id="shop").production_cutoff, datetime.time(20, 0))
        self.assertEqual(
            ProductionCutoffPolicy.objects.filter(
                source=ProductionCutoffPolicy.Source.ADMIN_CHANGE,
            ).count(),
            1,
        )

    def test_policy_history_is_audited(self):
        self.assertIn("ProductionCutoffPolicy", AUDITED)
