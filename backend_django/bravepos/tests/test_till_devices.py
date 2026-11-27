"""Till devices: recording each tablet's app build from its API calls, and the
backoffice Devices page that lists them.

Run:
    python manage.py test bravepos.tests.test_till_devices \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest import mock

from django.test import Client, RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone as djtz

from bravepos import audit, till_devices, views
from bravepos.models import BranchSession, Staff, TillDevice

from .factories import make_branch

PUBLISHED = "2026-11-20T02:15:00.000Z"


def headers(**overrides):
    h = {
        "HTTP_X_DEVICE_ID": "dev-aaaa1111",
        "HTTP_X_DEVICE_PLATFORM": "android",
        "HTTP_X_DEVICE_MANUFACTURER": "samsung",
        "HTTP_X_DEVICE_MODEL": "SM-X230",
        "HTTP_X_DEVICE_OS": "16",
        "HTTP_X_APP_VERSION": "1.4.1",
        "HTTP_X_APP_RUNTIME": "1.4.1",
        "HTTP_X_APP_CHANNEL": "production",
        "HTTP_X_APP_UPDATE_ID": "01a1f6bd-0aa0-7f94-b198-618620558b03",
        "HTTP_X_APP_UPDATE_PUBLISHED": PUBLISHED,
        "HTTP_X_APP_EMBEDDED": "0",
    }
    h.update(overrides)
    return {k: v for k, v in h.items() if v is not None}


class RecordTests(TestCase):
    def setUp(self):
        self.addCleanup(audit.clear_context)
        till_devices._last_write.clear()
        self.addCleanup(till_devices._last_write.clear)
        self.branch = make_branch(name="Central World")
        self.staff = Staff.objects.create(
            name="Cashier1", email="c1@test.local", password_hash="x", role="cashier")
        self.staff.branches.add(self.branch)
        self.sess = BranchSession.objects.create(
            token="tok" * 10, branch=self.branch, staff=self.staff, device_id="dev-login-id")

    def call(self, **h):
        req = RequestFactory().get("/", HTTP_AUTHORIZATION=f"Bearer {self.sess.token}", **h)
        return views.get_session(req)

    def test_headers_create_a_row_with_every_field(self):
        self.assertIsNotNone(self.call(**headers()))
        d = TillDevice.objects.get()
        self.assertEqual(d.device_id, "dev-aaaa1111")
        self.assertEqual(d.branch, self.branch)
        self.assertEqual(d.staff, self.staff)
        self.assertEqual((d.platform, d.manufacturer, d.model, d.os_version),
                         ("android", "samsung", "SM-X230", "16"))
        self.assertEqual((d.app_version, d.runtime_version, d.channel),
                         ("1.4.1", "1.4.1", "production"))
        self.assertEqual(d.short_update, "558b03")
        self.assertEqual(d.update_published_at, datetime(2026, 11, 20, 2, 15, tzinfo=timezone.utc))
        self.assertFalse(d.is_embedded)

    def test_an_old_bundle_is_keyed_by_its_login_device_id(self):
        self.call()  # no headers at all
        d = TillDevice.objects.get()
        self.assertEqual(d.device_id, "dev-login-id")
        self.assertEqual(d.branch, self.branch)
        self.assertFalse(d.reports_version)

    def test_no_headers_never_wipe_what_a_newer_bundle_reported(self):
        self.call(**headers(HTTP_X_DEVICE_ID="dev-login-id"))
        till_devices._last_write.clear()
        self.call()
        self.assertEqual(TillDevice.objects.get().app_version, "1.4.1")

    def test_no_device_id_anywhere_records_nothing(self):
        BranchSession.objects.filter(pk=self.sess.pk).update(device_id="")
        self.call()
        self.assertFalse(TillDevice.objects.exists())

    def test_repeat_calls_are_throttled(self):
        self.call(**headers())
        with mock.patch.object(TillDevice.objects, "update_or_create") as write:
            for _ in range(5):
                self.call(**headers())
        write.assert_not_called()

    def test_a_new_ota_is_written_at_once(self):
        self.call(**headers())
        self.call(**headers(HTTP_X_APP_UPDATE_ID="01a1f83a-3594-7537-0000-0000efb17b20"))
        self.assertEqual(TillDevice.objects.get().short_update, "b17b20")

    def test_written_again_once_the_throttle_window_passes(self):
        self.call(**headers())
        fp, at = till_devices._last_write["dev-aaaa1111"]
        till_devices._last_write["dev-aaaa1111"] = (fp, at - till_devices.THROTTLE_SECONDS - 1)
        TillDevice.objects.update(last_seen_at=djtz.now() - timedelta(hours=1))
        self.call(**headers())
        self.assertGreater(TillDevice.objects.get().last_seen_at, djtz.now() - timedelta(minutes=1))

    def test_a_failing_write_never_breaks_the_api_call(self):
        with mock.patch.object(TillDevice.objects, "update_or_create", side_effect=RuntimeError("db down")):
            self.assertIsNotNone(self.call(**headers()))

    def test_overlong_and_junk_headers_are_tolerated(self):
        self.call(**headers(HTTP_X_DEVICE_MODEL="M" * 500, HTTP_X_APP_UPDATE_PUBLISHED="not a date",
                            HTTP_X_APP_EMBEDDED="maybe"))
        d = TillDevice.objects.get()
        self.assertEqual(len(d.model), 64)
        self.assertIsNone(d.update_published_at)
        self.assertIsNone(d.is_embedded)

    def test_through_a_real_api_endpoint(self):
        res = self.client.get("/api/self-orders/pending",
                              HTTP_AUTHORIZATION=f"Bearer {self.sess.token}", **headers())
        self.assertLess(res.status_code, 500)
        self.assertTrue(TillDevice.objects.filter(device_id="dev-aaaa1111").exists())


class DevicesPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.password = "correct-horse-battery"
        cls.user = Staff(name="Office", username="office", email="o@therollingpinn.com",
                         role="admin", active=True, backoffice_access=True)
        cls.user.set_password(cls.password)
        cls.user.save()
        cls.cw = make_branch(name="Central World")
        cls.silom = make_branch(name="Silom")
        new = datetime(2026, 11, 20, 2, 15, tzinfo=timezone.utc)
        old = datetime(2026, 11, 13, 5, 2, tzinfo=timezone.utc)

        def dev(device_id, branch, **kw):
            base = dict(platform="android", manufacturer="samsung", model="SM-X230",
                        os_version="16", app_version="1.4.1", runtime_version="1.4.1",
                        channel="production")
            base.update(kw)
            return TillDevice.objects.create(device_id=device_id, branch=branch, **base)

        cls.latest = dev("latest-1", cls.cw, update_id="u-new-20558b03", update_published_at=new)
        cls.behind = dev("behind-1", cls.cw, model="SM-X216B", update_id="u-old-5ac26e7a",
                         update_published_at=old)
        cls.sideload = dev("sideload", cls.silom, model="SM-T225", channel="preview",
                           update_id="u-x-e0df9d4f", update_published_at=old)
        cls.unknown = TillDevice.objects.create(device_id="unknown-1", branch=cls.silom)
        cls.newer = dev("newer-1", cls.cw, model="SM-S911B", app_version="1.5.0",
                        runtime_version="1.5.0", update_id="u-15-efb17b20", update_published_at=new)
        cls.stale = dev("stale-1", cls.silom, update_id="u-new-20558b03", update_published_at=new)
        TillDevice.objects.filter(pk=cls.stale.pk).update(last_seen_at=djtz.now() - timedelta(days=60))

    def setUp(self):
        res = self.client.post(reverse("backoffice:login"),
                               {"username": "office", "password": self.password})
        self.assertEqual(res.status_code, 302)

    def statuses(self, response):
        return {d.device_id: d.status for g in response.context["branch_groups"] for d in g["devices"]}

    def test_it_needs_a_login(self):
        res = Client().get(reverse("backoffice:devices"))
        self.assertEqual(res.status_code, 302)
        self.assertIn(reverse("backoffice:login"), res["Location"])

    def test_groups_by_branch_and_tags_each_device(self):
        res = self.client.get(reverse("backoffice:devices"))
        self.assertEqual(res.status_code, 200)
        self.assertEqual([g["name"] for g in res.context["branch_groups"]], ["Central World", "Silom"])
        self.assertEqual(self.statuses(res), {
            "latest-1": "latest", "behind-1": "behind", "newer-1": "latest",
            "sideload": "old_app", "unknown-1": "unknown",
        })
        self.assertContains(res, "SM-X216B")

    def test_an_older_app_version_is_flagged(self):
        res = self.client.get(reverse("backoffice:devices"))
        flags = {d.device_id: d.version_behind for g in res.context["branch_groups"] for d in g["devices"]}
        self.assertTrue(flags["latest-1"])   # 1.4.1 while 1.5.0 exists
        self.assertFalse(flags["newer-1"])
        self.assertFalse(flags["sideload"])  # its own problem is "Old app"

    def test_stale_devices_hidden_unless_asked(self):
        self.assertNotIn("stale-1", self.statuses(self.client.get(reverse("backoffice:devices"))))
        self.assertIn("stale-1", self.statuses(self.client.get(reverse("backoffice:devices") + "?all=1")))

    def test_branch_filter_keeps_fleet_wide_status(self):
        res = self.client.get(reverse("backoffice:devices") + f"?branch={self.cw.id}")
        self.assertEqual([g["name"] for g in res.context["branch_groups"]], ["Central World"])
        self.assertEqual(self.statuses(res)["behind-1"], "behind")

    def test_an_ipad_only_update_leaves_android_tills_latest(self):
        # 2026-11-27: an iOS-only OTA on 1.5.0 made every Android 1.5.0 till
        # read "Behind" an iPad.  Each platform is judged on its own updates.
        TillDevice.objects.create(
            device_id="ipad-1", branch=self.cw, platform="ios", manufacturer="Apple",
            model="iPad", os_version="18", app_version="1.5.0", runtime_version="1.5.0",
            channel="production", update_id="u-ios-9d1c",
            update_published_at=datetime(2026, 11, 27, 9, 0, tzinfo=timezone.utc))
        res = self.client.get(reverse("backoffice:devices"))
        statuses = self.statuses(res)
        self.assertEqual(statuses["newer-1"], "latest")
        self.assertEqual(statuses["ipad-1"], "latest")
        self.assertEqual(res.context["behind_count"], 1)   # only behind-1

    def test_version_summary(self):
        res = self.client.get(reverse("backoffice:devices"))
        self.assertEqual(res.context["version_counts"], [("1.5.0", 1), ("1.4.1", 3), ("Unknown", 1)])
