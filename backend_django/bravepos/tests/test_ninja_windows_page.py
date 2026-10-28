"""Backoffice › Ninja windows reads and writes through the Shopify app's API."""
from unittest import mock

import httpx
from django.test import TestCase

from bravepos.models import Settings, Staff

API_ENV = {"NINJA_WINDOWS_API_URL": "https://shop.example/api/ninja/location-windows/",
           "NINJA_WINDOWS_API_TOKEN": "t0ken"}
LOCATIONS = {"locations": [
    {"location_name": "Silom", "window_start": "09:00", "window_end": "10:00"},
    {"location_name": "Ari", "window_start": None, "window_end": None},
]}


def _resp(status, body):
    return httpx.Response(status, json=body, request=httpx.Request("GET", API_ENV["NINJA_WINDOWS_API_URL"]))


@mock.patch.dict("os.environ", API_ENV)
class NinjaWindowsPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.password = "correct-horse-battery"
        admin = Staff(name="Ninja Admin", username="ninja", email="ninja@therollingpinn.com",
                      role="admin", active=True, backoffice_access=True)
        admin.set_password(cls.password)
        admin.save()
        Settings.objects.get_or_create(id="shop")

    def setUp(self):
        self.assertTrue(self.client.login(username="ninja", password=self.password))

    @mock.patch("backoffice.views.httpx.get", return_value=_resp(200, LOCATIONS))
    def test_lists_every_location_with_its_window(self, get):
        page = self.client.get("/backoffice/ninja-windows").content.decode()
        self.assertEqual(get.call_args.kwargs["headers"], {"X-Api-Token": "t0ken"})
        self.assertIn("Silom", page)
        self.assertIn('value="09:00" selected', page)
        self.assertIn('value="10:00" selected', page)
        self.assertIn("No window set", page)

    @mock.patch("backoffice.views.httpx.get", return_value=_resp(200, LOCATIONS))
    @mock.patch("backoffice.views.httpx.post", return_value=_resp(201, {"location": {}}))
    def test_save_posts_the_full_list(self, post, _get):
        r = self.client.post("/backoffice/ninja-windows", {
            "location_2": "Ari", "start_2": ["14:00", "09:00"], "end_2": ["15:00", "10:00"]})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(post.call_args.kwargs["json"], {"location_name": "Ari", "windows": [
            {"start": "09:00", "end": "10:00"}, {"start": "14:00", "end": "15:00"}]})

    @mock.patch("backoffice.views.httpx.get", return_value=_resp(200, LOCATIONS))
    @mock.patch("backoffice.views.httpx.post", return_value=_resp(201, {"location": {}}))
    def test_one_save_sends_only_the_changed_rows(self, post, _get):
        r = self.client.post("/backoffice/ninja-windows", {
            "location_1": "Silom", "start_1": "09:00", "end_1": "10:00",
            "location_2": "Ari", "start_2": "13:00", "end_2": "14:30"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual([c.kwargs["json"]["location_name"] for c in post.call_args_list], ["Ari"])

    @mock.patch("backoffice.views.httpx.get", return_value=_resp(200, LOCATIONS))
    @mock.patch("backoffice.views.httpx.post", return_value=_resp(201, {"location": {}}))
    def test_removing_every_window_clears_it(self, post, _get):
        self.client.post("/backoffice/ninja-windows", {"location_1": "Silom"})
        self.assertEqual(post.call_args.kwargs["json"], {"location_name": "Silom", "windows": []})

    @mock.patch("backoffice.views.httpx.get", return_value=_resp(200, LOCATIONS))
    @mock.patch("backoffice.views.httpx.post", return_value=_resp(201, {"location": {}}))
    def test_a_bad_row_keeps_its_input_and_the_rest_save(self, post, _get):
        r = self.client.post("/backoffice/ninja-windows", {
            "location_1": "Silom", "start_1": "11:00", "end_1": "12:00",
            "location_2": "Ari", "start_2": "15:00", "end_2": ""})
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "Pick both a start and an end time")
        self.assertContains(r, 'value="15:00" selected')
        self.assertEqual([c.kwargs["json"]["location_name"] for c in post.call_args_list], ["Silom"])

    @mock.patch("backoffice.views.httpx.get", return_value=_resp(200, LOCATIONS))
    @mock.patch("backoffice.views.httpx.post")
    def test_bad_windows_are_refused_without_calling_the_api(self, post, _get):
        for starts, ends, message in ((["12:00"], ["11:00"], "must end after it starts"),
                                      (["09:00", "09:30"], ["10:00", "11:00"], "overlap")):
            with self.subTest(message=message):
                r = self.client.post("/backoffice/ninja-windows", {
                    "location_2": "Ari", "start_2": starts, "end_2": ends})
                self.assertContains(r, message)
        post.assert_not_called()

    @mock.patch("backoffice.views.httpx.get", return_value=_resp(200, {"locations": [
        {"location_name": "Silom", "windows": [{"start": "09:00", "end": "10:00"},
                                               {"start": "14:00", "end": "15:00"}]}]}))
    def test_reads_the_list_shape(self, _get):
        page = self.client.get("/backoffice/ninja-windows").content.decode()
        self.assertIn('value="14:00" selected', page)
        self.assertIn("2 windows", page)

    @mock.patch("backoffice.views.httpx.get", return_value=_resp(401, {"error": "unauthorized"}))
    def test_api_failure_is_shown_not_raised(self, _get):
        self.assertContains(self.client.get("/backoffice/ninja-windows"),
                            "load locations: unauthorized")
