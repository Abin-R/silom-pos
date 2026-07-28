"""Backoffice user management + audit log.

Run:
    python manage.py test bravepos.tests.test_user_management_and_audit \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

from django.test import Client, TestCase
from django.urls import reverse

from bravepos import audit
from bravepos.models import AuditLog, Branch, Category, Product, Staff


PASSWORD = "correct-horse-battery"


def make_user(username="owner", email="owner@therollingpinn.com",
              password=PASSWORD, role="admin",
              backoffice_access=True, active=True) -> Staff:
    staff = Staff(
        name=(username or email).title(), username=username, email=email,
        role=role, active=active, backoffice_access=backoffice_access,
    )
    staff.set_password(password)
    staff.save()
    return staff


def sign_in(client, staff, password=PASSWORD):
    """Log in through the real login view.

    `Client.force_login` can't be used here: it calls Django's `login()`, which
    int-coerces the session key via `get_user_model()` — and this project's
    users are UUID-keyed `Staff`, not `auth.User`. That mismatch is the whole
    reason `backoffice.middleware.StaffAuthMiddleware` exists.
    """
    response = client.post(reverse("backoffice:login"), {
        "username": staff.username or staff.email,
        "password": password,
    })
    assert response.status_code == 302, f"sign-in failed for {staff}"
    return response


class LoginIdentifierTests(TestCase):
    """The login field takes a username OR an email — same account either way."""

    def setUp(self):
        self.password = PASSWORD
        self.staff = make_user()
        self.url = reverse("backoffice:login")

    def _post(self, identifier, password=None):
        return self.client.post(self.url, {
            "username": identifier,
            "password": password or self.password,
        })

    def test_signs_in_with_username(self):
        response = self._post("owner")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.session["_auth_user_id"], str(self.staff.id))

    def test_signs_in_with_email(self):
        response = self._post("owner@therollingpinn.com")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(self.client.session["_auth_user_id"], str(self.staff.id))

    def test_identifier_is_case_insensitive(self):
        self.assertEqual(self._post("OWNER").status_code, 302)
        # A fresh Client rather than `client.logout()` — the test client's
        # logout() calls `auth.get_user()`, which int-coerces our UUID session
        # key and blows up. The real logout view goes through `request.user`
        # and is unaffected.
        self.client = Client()
        self.assertEqual(self._post("Owner@TheRollingPinn.com").status_code, 302)

    def test_wrong_password_rejected(self):
        response = self._post("owner", password="nope")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_records_last_login(self):
        self.assertIsNone(self.staff.last_login_at)
        self._post("owner")
        self.staff.refresh_from_db()
        self.assertIsNotNone(self.staff.last_login_at)


class BackofficeAccessGateTests(TestCase):
    """`backoffice_access` is what separates a web login from a PIN-only
    staff row. Without it the POS default password must not open the site."""

    def test_staff_without_access_cannot_sign_in(self):
        make_user(username=None, email="admin.cnx@rollingpinn.com",
                  password="admin1234", backoffice_access=False)
        response = self.client.post(reverse("backoffice:login"), {
            "username": "admin.cnx@rollingpinn.com",
            "password": "admin1234",
        })
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("_auth_user_id", self.client.session)

    def test_inactive_account_cannot_sign_in(self):
        make_user(username="gone", email="gone@therollingpinn.com", active=False)
        response = self.client.post(reverse("backoffice:login"), {
            "username": "gone", "password": "correct-horse-battery",
        })
        self.assertNotIn("_auth_user_id", self.client.session)
        self.assertEqual(response.status_code, 200)

    def test_revoking_access_ends_the_session(self):
        staff = make_user(username="temp", email="temp@therollingpinn.com")
        self.client.post(reverse("backoffice:login"), {
            "username": "temp", "password": "correct-horse-battery",
        })
        self.assertEqual(self.client.get(reverse("backoffice:dashboard")).status_code, 200)

        Staff.objects.filter(pk=staff.pk).update(backoffice_access=False)
        response = self.client.get(reverse("backoffice:dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response["Location"])


class UserManagementTests(TestCase):
    def setUp(self):
        self.admin = make_user(username="boss", email="boss@therollingpinn.com")
        sign_in(self.client, self.admin)

    def test_create_generates_a_password_shown_once(self):
        response = self.client.post(reverse("backoffice:user_new"), {
            "name": "Data Team", "username": "data",
            "email": "data@therollingpinn.com", "role": "cashier", "active": "on",
        })
        self.assertEqual(response.status_code, 302)

        created = Staff.objects.get(username="data")
        self.assertTrue(created.backoffice_access)
        issued = self.client.session["issued_credentials"]
        self.assertEqual(issued["username"], "data")
        self.assertTrue(created.check_password(issued["password"]))

        # Landing on the list consumes it; a reload must not show it again.
        page = self.client.get(reverse("backoffice:user_list"))
        self.assertContains(page, issued["password"])
        self.assertNotContains(self.client.get(reverse("backoffice:user_list")),
                               issued["password"])

    def test_username_only_account_gets_placeholder_email(self):
        self.client.post(reverse("backoffice:user_new"), {
            "name": "Ops", "username": "ops", "email": "", "active": "on",
        })
        self.assertTrue(
            Staff.objects.get(username="ops").email.endswith("@users.noreply.rollingpinn.com")
        )

    def test_rejects_account_with_no_identifier(self):
        response = self.client.post(reverse("backoffice:user_new"), {
            "name": "Nameless", "username": "", "email": "", "active": "on",
        })
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "at least one to sign in with")

    def test_rejects_duplicate_identifier(self):
        response = self.client.post(reverse("backoffice:user_new"), {
            "name": "Impostor", "username": "boss", "active": "on",
        })
        self.assertContains(response, "already taken")

    def test_rejects_username_matching_another_accounts_email(self):
        response = self.client.post(reverse("backoffice:user_new"), {
            "name": "Impostor", "username": "boss@therollingpinn.com", "active": "on",
        })
        self.assertContains(response, "already another account")

    def test_cannot_demote_or_deactivate_self(self):
        response = self.client.post(
            reverse("backoffice:user_detail", args=[self.admin.id]),
            {"name": "Boss", "username": "boss", "email": "boss@therollingpinn.com",
             "role": "cashier"},
        )
        self.assertContains(response, "remove your own Admin role")
        self.assertContains(response, "deactivate your own account")
        self.admin.refresh_from_db()
        self.assertEqual(self.admin.role, "admin")

    def test_revoke_keeps_the_row_and_the_pin(self):
        victim = make_user(username="temp", email="temp@therollingpinn.com")
        victim.set_pin("4321")
        victim.save()

        self.client.post(reverse("backoffice:user_delete", args=[victim.id]))
        victim.refresh_from_db()
        self.assertFalse(victim.backoffice_access)
        self.assertIsNone(victim.username)
        self.assertTrue(victim.check_pin("4321"))

    def test_cashier_cannot_reach_user_management(self):
        cashier = make_user(username="till", email="till@therollingpinn.com",
                            role="cashier")
        client = Client()
        sign_in(client, cashier)
        self.assertEqual(client.get(reverse("backoffice:user_list")).status_code, 403)
        self.assertEqual(client.get(reverse("backoffice:audit_log")).status_code, 403)
        self.assertEqual(client.get(reverse("backoffice:user_new")).status_code, 403)
        # …and the sidebar doesn't dangle a link they can't use.
        self.assertNotContains(client.get(reverse("backoffice:dashboard")), "Audit Log")


class AuditCaptureTests(TestCase):
    def setUp(self):
        audit.clear_context()
        AuditLog.objects.all().delete()

    def test_create_update_delete_are_recorded(self):
        branch = Branch.objects.create(name="Samyan", code="SY")
        category = Category.objects.create(name="Drinks", branch=branch)
        product = Product.objects.create(name="Latte", price=100, category=category,
                                         branch=branch)

        created = AuditLog.objects.get(model="Product", action="create")
        self.assertEqual(created.object_label, str(product))
        self.assertEqual(created.branch_id, branch.id)

        product.price = 120  # int, not Decimal — the diff must still normalise
        product.save()
        updated = AuditLog.objects.get(model="Product", action="update")
        self.assertEqual(updated.changes["price"], {"from": "100.00", "to": "120.00"})
        self.assertEqual(list(updated.changes), ["price"])  # nothing phantom

        product.delete()
        self.assertTrue(AuditLog.objects.filter(model="Product", action="delete").exists())

    def test_unchanged_save_writes_nothing(self):
        branch = Branch.objects.create(name="Chidlom")
        AuditLog.objects.all().delete()
        branch.save()
        self.assertEqual(AuditLog.objects.filter(model="Branch").count(), 0)

    def test_secrets_are_redacted(self):
        staff = make_user(username="secretive", email="s@therollingpinn.com")
        entry = AuditLog.objects.filter(model="Staff", action="create").first()
        self.assertEqual(entry.changes["password_hash"], "***")
        self.assertNotIn(staff.password_hash, str(entry.changes))

        branch = Branch.objects.create(name="Keys", omise_secret_key="skey_live_abc123")
        entry = AuditLog.objects.get(model="Branch", action="create",
                                     object_id=str(branch.id))
        self.assertEqual(entry.changes["omise_secret_key"], "***")

    def test_actor_and_request_metadata_are_attributed(self):
        admin = make_user(username="boss", email="boss@therollingpinn.com")
        client = Client(HTTP_USER_AGENT="pytest-agent")
        sign_in(client, admin)
        AuditLog.objects.all().delete()

        client.post(reverse("backoffice:user_new"), {
            "name": "Data", "username": "data", "active": "on",
        })
        entry = AuditLog.objects.get(model="Staff", action="create")
        self.assertEqual(entry.actor_id, admin.id)
        self.assertEqual(entry.actor_label, admin.name)
        self.assertEqual(entry.source, "backoffice")
        self.assertEqual(entry.method, "POST")
        self.assertEqual(entry.user_agent, "pytest-agent")

    def test_login_and_failure_are_recorded(self):
        make_user(username="boss", email="boss@therollingpinn.com",
                  password="correct-horse-battery")
        AuditLog.objects.all().delete()

        self.client.post(reverse("backoffice:login"),
                         {"username": "boss", "password": "wrong"})
        failure = AuditLog.objects.get(action="login_failed")
        self.assertEqual(failure.object_label, "boss")

        self.client.post(reverse("backoffice:login"),
                         {"username": "boss", "password": "correct-horse-battery"})
        self.assertTrue(AuditLog.objects.filter(action="login").exists())

    def test_branch_assignment_is_recorded(self):
        staff = make_user(username="roamer", email="roamer@therollingpinn.com")
        branch = Branch.objects.create(name="Ladprao")
        AuditLog.objects.all().delete()

        staff.branches.add(branch)
        entry = AuditLog.objects.get(model="Staff", action="update")
        self.assertEqual(entry.changes["branches"]["to"], ["Ladprao"])

    def test_pause_suspends_capture(self):
        with audit.pause():
            Branch.objects.create(name="Quiet")
        self.assertEqual(AuditLog.objects.filter(model="Branch").count(), 0)

    def test_audit_log_is_not_itself_audited(self):
        Branch.objects.create(name="Loopy")
        self.assertEqual(AuditLog.objects.filter(model="AuditLog").count(), 0)


class AuditLogPageTests(TestCase):
    def setUp(self):
        self.admin = make_user(username="boss", email="boss@therollingpinn.com")
        sign_in(self.client, self.admin)
        self.branch = Branch.objects.create(name="Samyan", code="SY")

    def test_page_lists_entries(self):
        response = self.client.get(reverse("backoffice:audit_log"))
        self.assertEqual(response.status_code, 200)
        models_shown = {e.model for e in response.context["entries"]}
        self.assertIn("Branch", models_shown)

    def test_action_filter_narrows_results(self):
        # Asserting on the rendered HTML would false-pass here — the topbar's
        # branch selector prints every branch name on every page.
        response = self.client.get(reverse("backoffice:audit_log"), {"action": "delete"})
        self.assertEqual(list(response.context["entries"]), [])

    def test_actor_filter_narrows_results(self):
        response = self.client.get(reverse("backoffice:audit_log"),
                                   {"actor": str(self.admin.id)})
        self.assertTrue(
            all(e.actor_id == self.admin.id for e in response.context["entries"])
        )

    def test_export_is_csv_and_is_itself_audited(self):
        response = self.client.get(reverse("backoffice:audit_log_export"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])
        self.assertIn("When,Actor,Role,Action", response.content.decode())
        self.assertTrue(
            AuditLog.objects.filter(action="export", actor=self.admin).exists()
        )
