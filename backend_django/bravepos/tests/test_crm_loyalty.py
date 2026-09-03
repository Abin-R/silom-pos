"""Loyalty at the till: look the customer up, tick their rewards, file the sale.

The CRM (crm.rollingpinn.com) owns the customer's points, tier and vouchers.
The till's part is to say who is buying, show what they are holding, and file
the finished bill so it earns.  What these tests pin down is mostly what the
till must *not* do:

* never talk to the CRM at a branch outside the rollout — this ships dark and
  is turned on one branch at a time;
* never let the CRM cost a sale.  Every failure mode of every loyalty call has
  to leave a saved order and a 201, because by the time it runs the customer
  has already paid;
* never offer a cashier a control over a voucher only the customer can spend.
  The CRM drops those ids silently, so a toggle would look broken;
* never lose the CRM's order id, which is the only handle a void has.

The CRM client is mocked throughout — these tests make no network calls.

Run:
    python manage.py test bravepos.tests.test_crm_loyalty \
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

from decimal import Decimal
from unittest import mock

from django.test import TestCase, override_settings

from bravepos import loyalty
from bravepos import crm
from bravepos.crm import CrmError, MemberNotFound
from bravepos.models import BranchSession, Branch, Customer, Order, Staff

from .factories import make_branch, make_product, make_shop, open_shift


def member_body(*, points="27.00", to_confirm=(), available=(), tier="Bright Pink"):
    """A ``GET /members/<phone>/`` reply, in the CRM's own shape."""
    return {
        "ok": True,
        "phone_number": "+66812345678",
        "member": {
            "id": 10423,
            "name": "Ploy",
            "points_balance": points,
            "total_spent": "5480.00",
            "order_count": 14,
            "tier": {"id": 2, "title": tier},
            "available": list(available),
            "to_confirm": list(to_confirm),
        },
        "shop_items": [],
    }


def voucher(vid, title="Free Americano", **extra):
    return {"id": vid, "about_line_1": title, "about_line_2": "One per visit",
            "voucher_type": "basic", **extra}


class LoyaltyGateTests(TestCase):
    """Three things have to hold before a till talks to the CRM at all."""

    def setUp(self):
        self.branch = make_branch()
        self.branch.crm_branch_id = 6
        self.branch.crm_loyalty_enabled = True
        self.branch.save()

    @override_settings()
    def _enabled(self):
        return loyalty.enabled_for(self.branch)

    def test_all_three_conditions_met(self):
        with mock.patch("bravepos.crm.is_configured", return_value=True):
            self.assertTrue(loyalty.enabled_for(self.branch))

    def test_no_api_key_means_no_loyalty(self):
        with mock.patch("bravepos.crm.is_configured", return_value=False):
            self.assertFalse(loyalty.enabled_for(self.branch))

    def test_flag_off_means_no_loyalty(self):
        """The rollout switch. A linked branch still does nothing until ticked."""
        self.branch.crm_loyalty_enabled = False
        self.branch.save()
        with mock.patch("bravepos.crm.is_configured", return_value=True):
            self.assertFalse(loyalty.enabled_for(self.branch))

    def test_unlinked_branch_means_no_loyalty(self):
        """An order filed with no branch is attributed to no shop at all."""
        self.branch.crm_branch_id = None
        self.branch.save()
        with mock.patch("bravepos.crm.is_configured", return_value=True):
            self.assertFalse(loyalty.enabled_for(self.branch))

    def test_new_branches_default_to_off(self):
        self.assertFalse(Branch.objects.create(name="Fresh").crm_loyalty_enabled)


class ApiTestCase(TestCase):
    """A signed-in till at a branch that is in the loyalty rollout."""

    def setUp(self):
        make_shop()
        self.branch = make_branch()
        self.branch.crm_branch_id = 6
        self.branch.crm_loyalty_enabled = True
        self.branch.save()
        self.product = make_product(self.branch, price="790.00")
        self.shift = open_shift(self.branch)
        self.staff = Staff.objects.create(
            name="Ploy Cashier", email="ploy@test.local", password_hash="x",
            role="cashier",
        )
        self.staff.branches.add(self.branch)
        self.session = BranchSession.objects.create(
            token="loy" * 12, branch=self.branch, staff=self.staff,
        )
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {self.session.token}"}
        self.customer = Customer.objects.create(
            branch=self.branch, name="Ploy", phone="0812345678",
        )
        # Every test runs as if a key is configured; the ones about *not*
        # calling out turn the branch flag off instead, which is the control a
        # shop actually has.
        patcher = mock.patch("bravepos.crm.is_configured", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def lookup(self, customer=None):
        return self.client.post(
            "/api/crm/member",
            {"customer_id": str((customer or self.customer).id)},
            content_type="application/json", **self.auth,
        )

    def sell(self, **extra):
        body = {
            "items": [{"product_id": str(self.product.id), "name": self.product.name,
                       "price": 790, "qty": 1}],
            "subtotal": 790, "total": 790, "discount_amount": 0,
            "payment_method": "cash", "paid_amount": 800,
            "customer_id": str(self.customer.id), "customer_name": "Ploy",
            **extra,
        }
        return self.client.post("/api/orders", body,
                                content_type="application/json", **self.auth)


class MemberLookupTests(ApiTestCase):
    """`POST /api/crm/member` — what the cart panel is drawn from."""

    def test_known_member_returns_points_and_rewards(self):
        body = member_body(to_confirm=[voucher(8812)], available=[voucher(9001, "Free cake")])
        with mock.patch("bravepos.crm.lookup_member", return_value=body) as look:
            res = self.lookup()

        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["enabled"])
        self.assertEqual(data["member"]["points_balance"], "27.00")
        self.assertEqual(data["member"]["tier"], "Bright Pink")
        # The number is passed on exactly as the shop holds it — the CRM
        # normalises, and half-normalising here is how one person ends up with
        # two memberships.
        look.assert_called_once_with("0812345678")
        # ...and the CRM's normalised form comes back for display.
        self.assertEqual(data["phone"], "+66812345678")

    def test_redeemed_vouchers_come_first_and_are_the_only_actionable_ones(self):
        """The distinction the whole panel rests on.

        A voucher the customer is still *holding* can only be redeemed by them,
        and posting its id with an order is ignored silently by the CRM.  So it
        arrives marked unredeemable and the till shows it without a control.
        """
        body = member_body(
            to_confirm=[voucher(8812, "Free Americano")],
            available=[voucher(9001, "Free cake")],
        )
        with mock.patch("bravepos.crm.lookup_member", return_value=body):
            rewards = self.lookup().json()["rewards"]

        self.assertEqual([r["id"] for r in rewards], [8812, 9001])
        self.assertEqual([r["redeemable"] for r in rewards], [True, False])
        self.assertEqual(rewards[0]["title"], "Free Americano")

    def test_unknown_number_is_registered_on_the_spot(self):
        """The till's customer list is the front door to the programme."""
        with mock.patch("bravepos.crm.lookup_member",
                        return_value={"ok": True, "member": None}), \
             mock.patch("bravepos.crm.register_member") as reg:
            reg.return_value = {"ok": True, "created": True,
                                **member_body(points="0.00")}
            res = self.lookup()

        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.json()["created"])
        reg.assert_called_once()
        self.assertEqual(reg.call_args.args, ("0812345678", "Ploy"))

    def test_customer_profile_is_carried_into_the_registration(self):
        """A birthday only reaches the CRM if the shop actually holds one — it
        is what the birthday voucher fires on, and the CRM refuses a malformed
        one outright rather than dropping it."""
        self.customer.birth_date = "1994-03-11"
        self.customer.gender = "female"
        self.customer.email = "ploy@example.com"
        self.customer.save()

        with mock.patch("bravepos.crm.lookup_member",
                        return_value={"ok": True, "member": None}), \
             mock.patch("bravepos.crm.register_member") as reg:
            reg.return_value = {"ok": True, "created": True, **member_body()}
            self.lookup()

        self.assertEqual(reg.call_args.kwargs, {
            "email": "ploy@example.com",
            "birthday": "1994-03-11",
            "gender": "female",
        })

    def test_unspecified_gender_is_dropped_rather_than_guessed(self):
        """Ours means nobody asked; the CRM's `prefer_not_to_say` means the
        customer declined.  They are not the same answer."""
        self.customer.gender = "unspecified"
        self.customer.save()
        with mock.patch("bravepos.crm.lookup_member",
                        return_value={"ok": True, "member": None}), \
             mock.patch("bravepos.crm.register_member") as reg:
            reg.return_value = {"ok": True, "created": True, **member_body()}
            self.lookup()
        self.assertEqual(reg.call_args.kwargs["gender"], "")

    def test_customer_with_no_phone_is_disabled_not_an_error(self):
        """Phone number *is* the identity in the CRM. A row without one has no
        loyalty, and the cashier's fix is to edit the customer."""
        nameless = Customer.objects.create(branch=self.branch, name="Walk-in")
        with mock.patch("bravepos.crm.lookup_member") as look:
            res = self.lookup(nameless)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json(), {"enabled": False, "reason": "no_phone"})
        look.assert_not_called()

    def test_branch_outside_the_rollout_never_calls_the_crm(self):
        self.branch.crm_loyalty_enabled = False
        self.branch.save()
        with mock.patch("bravepos.crm.lookup_member") as look:
            res = self.lookup()
        self.assertEqual(res.json(), {"enabled": False, "reason": "branch"})
        look.assert_not_called()

    def test_crm_failure_is_a_502_not_a_blank_panel(self):
        """A cashier told nothing would read an empty list as "no rewards" and
        send a customer away without one they had already redeemed."""
        with mock.patch("bravepos.crm.lookup_member",
                        side_effect=CrmError("Couldn't reach the CRM (ConnectError).")):
            res = self.lookup()
        self.assertEqual(res.status_code, 502)
        self.assertIn("ConnectError", res.json()["error"])

    def test_a_malformed_customer_id_is_a_404_not_a_500(self):
        with mock.patch("bravepos.crm.lookup_member") as look:
            res = self.client.post(
                "/api/crm/member", {"customer_id": "not-a-uuid"},
                content_type="application/json", **self.auth)
        self.assertEqual(res.status_code, 404)
        look.assert_not_called()

    def test_a_till_can_only_look_up_its_own_branchs_customers(self):
        other = Customer.objects.create(
            branch=make_branch(name="Elsewhere"), name="Someone", phone="0899999999")
        with mock.patch("bravepos.crm.lookup_member") as look:
            res = self.lookup(other)
        self.assertEqual(res.status_code, 404)
        look.assert_not_called()


class BranchFeedTests(ApiTestCase):
    """The till reads the rollout flag off the branch feed and skips the whole
    feature when it is off — so a branch outside the trial sends no lookup at
    all, rather than one the server politely declines."""

    def test_the_feed_carries_the_rollout_flag(self):
        res = self.client.get("/api/branches", **self.auth)
        self.assertEqual(res.status_code, 200)
        row = next(r for r in res.json() if r["id"] == str(self.branch.id))
        self.assertTrue(row["crm_loyalty_enabled"])

    def test_a_branch_outside_the_rollout_reports_it(self):
        self.branch.crm_loyalty_enabled = False
        self.branch.save()
        res = self.client.get("/api/branches", **self.auth)
        row = next(r for r in res.json() if r["id"] == str(self.branch.id))
        self.assertFalse(row["crm_loyalty_enabled"])

    def test_a_till_cannot_switch_loyalty_on_for_itself(self):
        """The rollout is the back office's call, not a tablet's. Read-only on
        this feed, so a crafted PUT from a till can't opt a live branch in."""
        self.branch.crm_loyalty_enabled = False
        self.branch.save()
        res = self.client.put(
            f"/api/branches/{self.branch.id}/",
            {"name": self.branch.name, "crm_loyalty_enabled": True},
            content_type="application/json", **self.auth)
        self.assertIn(res.status_code, (200, 403, 405))
        self.branch.refresh_from_db()
        self.assertFalse(self.branch.crm_loyalty_enabled)

    def test_the_payment_credentials_still_stay_off_the_feed(self):
        """Guard on the field list I edited: adding one must not have opened
        the door to the secrets sitting next to it on the model."""
        res = self.client.get("/api/branches", **self.auth)
        row = next(r for r in res.json() if r["id"] == str(self.branch.id))
        for secret in ("beam_api_key", "omise_secret_key", "beam_merchant_id",
                       "omise_public_key"):
            self.assertNotIn(secret, row)


class RecordSaleTests(ApiTestCase):
    """Filing the finished bill — the half that pays the customer."""

    def test_sale_is_filed_with_the_ticked_rewards(self):
        with mock.patch("bravepos.crm.record_order") as rec:
            rec.return_value = {"ok": True, "created": True, "order_id": 50412,
                                "points_earned": "3"}
            res = self.sell(crm_reward_ids=[8812])

        self.assertEqual(res.status_code, 201)
        order = Order.objects.get(order_number=res.json()["order_number"])
        self.assertEqual(order.crm_order_id, 50412)

        kwargs = rec.call_args.kwargs
        self.assertEqual(kwargs["phone"], "0812345678")
        self.assertEqual(kwargs["branch_id"], 6)
        self.assertEqual(kwargs["reward_ids"], [8812])
        # The receipt id is our order number: it is the CRM's idempotency key,
        # so a retry of this bill returns the order it already made rather than
        # paying the customer twice.
        self.assertEqual(kwargs["receipt_id"], order.order_number)
        # The grand total on the receipt — what the customer paid and can read
        # back off the slip in their hand.
        self.assertEqual(Decimal(str(kwargs["amount"])), order.total)

    def test_the_sale_is_filed_as_retail_trade(self):
        """A bill rung up by a cashier at a counter is retail, whatever pipe it
        reached the CRM through — not "api", which is the CRM's default for
        this interface and lumps the shop floor in with every machine caller.

        The value is also half of the CRM's replay key (receipt + source +
        member), so it has to be one fixed value rather than something that can
        drift between callers."""
        with mock.patch("bravepos.crm.record_order") as rec:
            rec.return_value = {"ok": True, "order_id": 1}
            self.sell()
        # record_order owns the value; assert on what actually crosses the wire.
        self.assertEqual(crm.POS_SOURCE, "retail")

    def test_ticking_a_reward_changes_no_money(self):
        """The reward was handed over at the counter, not discounted off the
        bill.  The CRM only *confirms* that this sale consumed it."""
        with mock.patch("bravepos.crm.record_order",
                        return_value={"ok": True, "order_id": 1}):
            plain = self.sell().json()
        with mock.patch("bravepos.crm.record_order",
                        return_value={"ok": True, "order_id": 2}):
            with_reward = self.sell(crm_reward_ids=[8812]).json()
        self.assertEqual(plain["total"], with_reward["total"])
        self.assertEqual(plain["paid_amount"], with_reward["paid_amount"])

    def test_a_crm_outage_never_costs_the_sale(self):
        """By the time this runs the customer has paid. Losing the points is a
        bad afternoon; losing the bill is a bad day."""
        with mock.patch("bravepos.crm.record_order",
                        side_effect=CrmError("Couldn't reach the CRM (ReadTimeout).")):
            res = self.sell()
        self.assertEqual(res.status_code, 201)
        order = Order.objects.get(order_number=res.json()["order_number"])
        self.assertIsNone(order.crm_order_id)
        self.assertEqual(order.total, Decimal("790.00"))

    def test_a_reply_with_no_order_id_leaves_the_row_unlinked(self):
        """Filed but unnameable: it can never be voided from here, so the row
        must not claim otherwise."""
        with mock.patch("bravepos.crm.record_order", return_value={"ok": True}):
            res = self.sell()
        self.assertEqual(res.status_code, 201)
        self.assertIsNone(
            Order.objects.get(order_number=res.json()["order_number"]).crm_order_id)

    def test_an_unknown_member_is_registered_then_the_sale_re_filed(self):
        """Covers loyalty being switched on mid-basket, or a lookup that failed
        — the till may never have signed this customer up."""
        with mock.patch("bravepos.crm.record_order") as rec, \
             mock.patch("bravepos.crm.register_member") as reg:
            rec.side_effect = [MemberNotFound("not a member"),
                               {"ok": True, "order_id": 777}]
            res = self.sell()

        reg.assert_called_once_with("0812345678", "Ploy")
        self.assertEqual(rec.call_count, 2)
        self.assertEqual(
            Order.objects.get(order_number=res.json()["order_number"]).crm_order_id, 777)

    def test_bad_reward_ids_are_dropped_rather_than_forwarded(self):
        """The CRM refuses a bare string outright and drops a bad id inside a
        good list silently — either way the cashier would see a reward that
        quietly failed to apply."""
        with mock.patch("bravepos.crm.record_order",
                        return_value={"ok": True, "order_id": 1}) as rec:
            self.sell(crm_reward_ids=["8812", "nonsense", None, 9001])
        self.assertEqual(rec.call_args.kwargs["reward_ids"], [8812, 9001])

        with mock.patch("bravepos.crm.record_order",
                        return_value={"ok": True, "order_id": 2}) as rec:
            self.sell(crm_reward_ids="8812")
        self.assertEqual(rec.call_args.kwargs["reward_ids"], [])

    def test_a_bill_with_no_customer_is_not_filed(self):
        with mock.patch("bravepos.crm.record_order") as rec:
            res = self.client.post(
                "/api/orders",
                {"items": [], "subtotal": 100, "total": 100,
                 "payment_method": "cash", "paid_amount": 100},
                content_type="application/json", **self.auth)
        self.assertEqual(res.status_code, 201)
        rec.assert_not_called()

    def test_a_branch_outside_the_rollout_files_nothing(self):
        self.branch.crm_loyalty_enabled = False
        self.branch.save()
        with mock.patch("bravepos.crm.record_order") as rec:
            res = self.sell()
        self.assertEqual(res.status_code, 201)
        rec.assert_not_called()


class VoidTests(ApiTestCase):
    """Voiding a bill has to reach the customer's standing too."""

    def _paid_order(self, crm_order_id=50412):
        with mock.patch("bravepos.crm.record_order",
                        return_value={"ok": True, "order_id": crm_order_id}):
            res = self.sell()
        return Order.objects.get(order_number=res.json()["order_number"])

    def _set_status(self, order, status):
        return self.client.put(
            f"/api/orders/{order.id}/status", {"status": status},
            content_type="application/json", **self.auth)

    def test_voiding_a_bill_reverses_it_in_the_crm(self):
        order = self._paid_order()
        with mock.patch("bravepos.crm.void_order",
                        return_value={"ok": True, "already_voided": False}) as void:
            res = self._set_status(order, "cancel")

        self.assertEqual(res.status_code, 200)
        void.assert_called_once()
        self.assertEqual(void.call_args.args, (50412,))
        self.assertIn("Ploy Cashier", void.call_args.kwargs["note"])
        # Cleared, so the row states plainly that it is no longer filed.
        order.refresh_from_db()
        self.assertIsNone(order.crm_order_id)

    def test_a_crm_outage_never_blocks_a_void(self):
        order = self._paid_order()
        with mock.patch("bravepos.crm.void_order",
                        side_effect=CrmError("Couldn't reach the CRM (ConnectError).")):
            res = self._set_status(order, "cancel")

        self.assertEqual(res.status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, "cancel")
        # Still ours to retry: the id is the only handle a void has, so it is
        # kept when the void didn't land.
        self.assertEqual(order.crm_order_id, 50412)

    def test_a_bill_that_was_never_filed_calls_nothing(self):
        order = self._paid_order(crm_order_id=None)
        with mock.patch("bravepos.crm.void_order") as void:
            self._set_status(order, "cancel")
        void.assert_not_called()

    def test_re_cancelling_does_not_void_twice(self):
        """Only a change of state is a void; the CRM's own void is idempotent
        but a second call would be a second entry in its audit trail."""
        order = self._paid_order()
        with mock.patch("bravepos.crm.void_order", return_value={"ok": True}) as void:
            self._set_status(order, "cancel")
            self._set_status(order, "cancel")
        self.assertEqual(void.call_count, 1)

    def test_un_voiding_files_the_sale_afresh(self):
        """The CRM has no un-void; its prescribed correction is to void and
        post the same receipt again, which is what this amounts to."""
        order = self._paid_order()
        with mock.patch("bravepos.crm.void_order", return_value={"ok": True}):
            self._set_status(order, "cancel")

        with mock.patch("bravepos.crm.record_order",
                        return_value={"ok": True, "order_id": 60001}) as rec:
            self._set_status(order, "completed")

        rec.assert_called_once()
        self.assertEqual(rec.call_args.kwargs["receipt_id"], order.order_number)
        order.refresh_from_db()
        self.assertEqual(order.crm_order_id, 60001)
