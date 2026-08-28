"""App distribution: the public /app/ install page and the builds behind it.

The page is unauthenticated and the download link is a bare token, so the
tests that matter are the ones about what is reachable: an unlisted build must
not be, and the link handed to a tablet must land on Drive rather than on this
server.

Run:
    python manage.py test bravepos.tests.test_app_releases \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

from datetime import datetime, timezone

from django.test import TestCase
from django.urls import reverse

from bravepos import appdist
from bravepos.models import AppRelease, Branch, Settings, Staff

FILE_ID = "19OZFIx8-gwZflKdr7Dy8oqbBXDB3wEcg"
BUILD_ID = "9da26a2a-53e4-4973-b009-bb7a6b7982fb"


def make_release(version="1.4.1", published=True, day=18, **kwargs):
    """A release published at a fixed instant.

    Ordering is by ``published_at``, so tests that care which build is current
    have to pin it — rows created in the same millisecond would otherwise sort
    by whichever the database felt like.
    """
    return AppRelease.objects.create(
        version=version, published=published,
        version_code=kwargs.pop("version_code", 0),
        build_id=kwargs.pop("build_id", BUILD_ID),
        drive_file_id=kwargs.pop("drive_file_id", FILE_ID),
        published_at=datetime(2026, 8, day, 12, 0, tzinfo=timezone.utc),
        token=AppRelease.new_token(), **kwargs,
    )


class DriveLinkParsingTests(TestCase):
    """Whatever someone copies out of Drive has to work — nobody should have to
    know which run of characters in the URL is the file ID."""

    def test_every_shape_drive_hands_out(self):
        for link in (
            f"https://drive.google.com/file/d/{FILE_ID}/view?usp=sharing",
            f"https://drive.google.com/open?id={FILE_ID}",
            f"https://drive.google.com/uc?export=download&id={FILE_ID}",
            f"https://drive.usercontent.google.com/download?id={FILE_ID}&export=download",
            f"  {FILE_ID}  ",
        ):
            with self.subTest(link=link):
                self.assertEqual(appdist.parse_drive_file_id(link), FILE_ID)

    def test_nonsense_yields_nothing(self):
        for link in ("", "   ", "not a link", "https://example.com/thing.apk"):
            with self.subTest(link=link):
                self.assertEqual(appdist.parse_drive_file_id(link), "")


class BuildIdParsingTests(TestCase):
    """The build id turns up in the artifact filename, the expo.dev URL, and
    the build list — take it from any of them."""

    def test_every_place_it_appears(self):
        for text in (
            f"application-{BUILD_ID}.apk",
            f"https://expo.dev/accounts/a/projects/b/builds/{BUILD_ID}",
            BUILD_ID.upper(),
            f"  {BUILD_ID}  ",
        ):
            with self.subTest(text=text):
                self.assertEqual(appdist.parse_build_id(text), BUILD_ID)

    def test_nothing_uuid_shaped_yields_nothing(self):
        for text in ("", "9da26a2a", "not-a-build", "1.4.1"):
            with self.subTest(text=text):
                self.assertEqual(appdist.parse_build_id(text), "")


class CurrentReleaseTests(TestCase):
    def test_current_is_the_most_recently_published(self):
        make_release(version="1.4.0", day=10)
        newest = make_release(version="1.4.1", day=18)
        self.assertEqual(AppRelease.current(), newest)

    def test_unlisting_the_top_build_promotes_the_one_below(self):
        older = make_release(version="1.4.0", day=10)
        newest = make_release(version="1.4.1", day=18)

        newest.published = False
        newest.save()

        self.assertEqual(AppRelease.current(), older)

    def test_no_published_builds_means_no_current(self):
        make_release(published=False)
        self.assertIsNone(AppRelease.current())

    def test_builds_may_share_a_version_code(self):
        """Every EAS build to date is versionCode 1 — `preview` in eas.json has
        no `autoIncrement`. A unique constraint here would allow exactly one
        build to ever be listed."""
        make_release(version="1.4.0", version_code=1, day=10)
        make_release(version="1.4.1", version_code=1, day=18)
        self.assertEqual(AppRelease.objects.count(), 2)

    def test_label_prefers_the_version_code_and_falls_back_to_the_build(self):
        self.assertEqual(make_release(version_code=42, day=1).label, "build 42")
        self.assertEqual(make_release(version_code=0, day=2).label, "9da26a2a")
        self.assertEqual(make_release(version_code=0, build_id="", day=3).label, "")

    def test_filename_identifies_the_build(self):
        self.assertEqual(
            make_release(version="1.4.1", version_code=42, day=1).filename,
            "bravepos-1.4.1-42.apk")
        self.assertEqual(
            make_release(version="1.4.1", day=2).filename,
            "bravepos-1.4.1-9da26a2a.apk")
        self.assertEqual(
            make_release(version="1.4.1", build_id="", day=3).filename,
            "bravepos-1.4.1.apk")


class InstallPageTests(TestCase):
    def test_page_is_public(self):
        make_release()
        response = self.client.get(reverse("app_install"))
        self.assertEqual(response.status_code, 200)

    def test_empty_state_when_nothing_published(self):
        response = self.client.get(reverse("app_install"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "No build published yet")

    def test_current_build_is_offered_and_older_ones_listed(self):
        older = make_release(version="1.4.0", day=10)
        newest = make_release(version="1.4.1", day=18)

        response = self.client.get(reverse("app_install"))

        self.assertEqual(response.context["current"], newest)
        self.assertEqual(list(response.context["older"]), [older])
        self.assertContains(response, "Download APK")

    def test_unlisted_builds_are_not_shown(self):
        withdrawn = make_release(version="9.9.9", published=False, day=20)
        make_release()

        response = self.client.get(reverse("app_install"))

        self.assertNotContains(response, "9.9.9")
        self.assertNotContains(response, withdrawn.token)

    def test_qr_encodes_the_public_install_url(self):
        make_release()
        response = self.client.get(reverse("app_install"))
        self.assertContains(response, appdist.install_page_url())


class DownloadRedirectTests(TestCase):
    def test_download_hands_off_to_drive(self):
        """A redirect, not a proxy: 160 MB must not travel through gunicorn."""
        release = make_release()

        response = self.client.get(
            reverse("app_download", kwargs={"token": release.token}))

        self.assertEqual(response.status_code, 302)
        self.assertIn("drive.usercontent.google.com", response["Location"])
        self.assertIn(FILE_ID, response["Location"])
        # Without confirm=t, Drive answers a file this size with a virus-scan
        # interstitial and the tablet gets a web page instead of an APK.
        self.assertIn("confirm=t", response["Location"])

    def test_token_stays_out_of_the_referer_google_sees(self):
        release = make_release()
        response = self.client.get(
            reverse("app_download", kwargs={"token": release.token}))
        self.assertEqual(response["Referrer-Policy"], "no-referrer")

    def test_unlisted_build_is_not_downloadable(self):
        release = make_release(published=False)
        response = self.client.get(
            reverse("app_download", kwargs={"token": release.token}))
        self.assertEqual(response.status_code, 404)

    def test_unknown_token_is_a_404(self):
        response = self.client.get(
            reverse("app_download", kwargs={"token": "not-a-real-token"}))
        self.assertEqual(response.status_code, 404)


class QrLibraryTests(TestCase):
    def test_library_is_served_and_revalidates(self):
        response = self.client.get(reverse("app_qr_js"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("javascript", response["Content-Type"])
        self.assertTrue(response["ETag"])

        again = self.client.get(reverse("app_qr_js"),
                                HTTP_IF_NONE_MATCH=response["ETag"])
        self.assertEqual(again.status_code, 304)


class BackofficeAppReleaseTests(TestCase):
    """Listing a build is admin-only — the page it feeds is public."""

    @classmethod
    def setUpTestData(cls):
        Settings.objects.get_or_create(id="shop")
        Branch.objects.create(name="Silom", code="SLM")
        cls.password = "correct-horse-battery"
        for login, role in (("boss", "admin"), ("till", "cashier")):
            member = Staff(
                name=login, username=login, email=f"{login}@therollingpinn.com",
                role=role, active=True, backoffice_access=True,
            )
            member.set_password(cls.password)
            member.save()

    def sign_in(self, username):
        self.assertEqual(
            self.client.post(reverse("backoffice:login"), {
                "username": username, "password": self.password,
            }).status_code, 302, f"{username} could not sign in")

    def test_admin_can_list_a_build_from_a_pasted_drive_link(self):
        self.sign_in("boss")

        response = self.client.post(reverse("backoffice:app_release_new"), {
            "drive_link": f"https://drive.google.com/file/d/{FILE_ID}/view?usp=sharing",
            "version": "1.4.1",
            "version_code": "",
            "build_id": f"application-{BUILD_ID}.apk",
            "size_mb": "162.8",
            "notes": "Printer fix.",
            "published": "on",
            "published_on": "2026-08-18",
        })

        self.assertEqual(response.status_code, 302)
        release = AppRelease.objects.get()
        self.assertEqual(release.drive_file_id, FILE_ID)
        self.assertEqual(release.build_id, BUILD_ID)
        self.assertEqual(release.version_code, 0, "blank means not recorded")
        self.assertEqual(release.size_mb, 162.8)
        self.assertTrue(release.token, "a build with no token is unreachable")

    def test_cashier_cannot_list_a_build(self):
        self.sign_in("till")
        response = self.client.get(reverse("backoffice:app_release_new"))
        self.assertEqual(response.status_code, 403)
        self.assertEqual(AppRelease.objects.count(), 0)

    def test_a_second_build_at_the_same_version_code_is_accepted(self):
        """Rejecting a repeat would cap the whole list at one build — every EAS
        build so far is versionCode 1."""
        make_release(version="1.4.0", version_code=1, day=10)
        self.sign_in("boss")

        response = self.client.post(reverse("backoffice:app_release_new"), {
            "drive_link": FILE_ID, "version": "1.4.1", "version_code": "1",
            "build_id": BUILD_ID, "published": "on",
        })

        self.assertEqual(response.status_code, 302)
        self.assertEqual(AppRelease.objects.count(), 2)

    def test_a_non_numeric_build_number_is_rejected(self):
        self.sign_in("boss")

        response = self.client.post(reverse("backoffice:app_release_new"), {
            "drive_link": FILE_ID, "version": "1.4.1",
            "version_code": "not a number", "published": "on",
        })

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "whole number")
        self.assertEqual(AppRelease.objects.count(), 0)

    def test_a_rejected_form_comes_back_with_what_was_typed(self):
        self.sign_in("boss")

        response = self.client.post(reverse("backoffice:app_release_new"), {
            "drive_link": "", "version": "1.4.2", "version_code": "not a number",
            "notes": "Printer fix.",
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["form"]["version"], "1.4.2")
        self.assertEqual(response.context["form"]["version_code"], "not a number")
        self.assertEqual(response.context["form"]["notes"], "Printer fix.")

    def test_editing_keeps_the_download_link_working(self):
        release = make_release()
        token = release.token
        self.sign_in("boss")

        self.client.post(
            reverse("backoffice:app_release_detail", kwargs={"release_id": release.id}),
            {
                "drive_link": FILE_ID, "version": "1.4.1", "build_id": BUILD_ID,
                "notes": "Reworded.", "published": "on",
                "published_on": "2026-08-18",
            },
        )

        release.refresh_from_db()
        self.assertEqual(release.token, token, "editing must not orphan a shared link")
        self.assertEqual(release.notes, "Reworded.")
