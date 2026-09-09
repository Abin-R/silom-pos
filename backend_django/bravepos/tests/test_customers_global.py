"""The customer book is shop-wide.

It used to be scoped per branch, which meant a regular who registered at
EmQuartier was a stranger at Silom: a second row, a second half-history, and a
second CRM membership their points were not on.  These tests pin the two
halves of the fix — every till reads one list, and the rows that boundary
already split are folded back together.
"""
from decimal import Decimal

from django.db import IntegrityError, transaction
from django.test import TestCase

from bravepos.customers import (
    merge_duplicates_by_phone, normalise_phone, phone_search_digits,
    restore_snapshot,
)
from bravepos.models import (
    BranchSession, Customer, CustomerMergeBackup, Order, ParkedOrder, Staff,
)

from .factories import make_branch, make_shop


class GlobalCustomerBookTests(TestCase):
    """A till signed in at Silom, and a customer who belongs to Thonglor."""

    def setUp(self):
        make_shop()
        self.branch = make_branch(name="Silom")
        self.other_branch = make_branch(name="Thonglor")
        self.staff = Staff.objects.create(
            name="Ploy", email="ploy@test.local", password_hash="x", role="cashier",
        )
        self.staff.branches.add(self.branch)
        self.session = BranchSession.objects.create(
            token="cus" * 12, branch=self.branch, staff=self.staff,
        )
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {self.session.token}"}
        self.outsider = Customer.objects.create(
            branch=self.other_branch, name="Somchai", phone="0899999999",
        )

    def test_a_till_lists_customers_from_every_branch(self):
        res = self.client.get("/api/customers", **self.auth)
        self.assertEqual(res.status_code, 200)
        names = [c["name"] for c in res.json()]
        self.assertIn("Somchai", names)

    def test_a_till_can_edit_a_customer_from_another_branch(self):
        res = self.client.patch(
            f"/api/customers/{self.outsider.id}",
            {"phone": "0811111111"},
            content_type="application/json", **self.auth,
        )
        self.assertEqual(res.status_code, 200)
        self.outsider.refresh_from_db()
        self.assertEqual(self.outsider.phone, "0811111111")

    def test_a_new_customer_is_stamped_with_the_till_s_branch(self):
        """Home branch is still recorded — it is just no longer a fence."""
        res = self.client.post(
            "/api/customers", {"name": "Nok", "phone": "0822222222"},
            content_type="application/json", **self.auth,
        )
        self.assertEqual(res.status_code, 201)
        self.assertEqual(Customer.objects.get(name="Nok").branch, self.branch)

    def test_the_book_still_needs_a_session(self):
        """Shop-wide is not public."""
        self.assertEqual(self.client.get("/api/customers").status_code, 401)

    def test_customer_stats_are_lifetime_across_branches(self):
        """What a cashier needs is what this person is worth to the business,
        not the slice of it rung up on this particular till."""
        for i, branch in enumerate((self.branch, self.other_branch)):
            Order.objects.create(
                branch=branch, order_number=f"GLB-{i}",
                subtotal=Decimal("100"), total=Decimal("100"),
                payment_method="cash", status="completed",
                customer=self.outsider, staff="Ploy",
            )
        res = self.client.get(
            f"/api/customers/{self.outsider.id}/stats", **self.auth)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["bill_count"], 2)
        self.assertEqual(res.json()["success_total"], 200.0)


class CustomerSearchAndPagingTests(TestCase):
    """The book is the whole shop's, so the till asks for a page and lets the
    server reach the rest — without breaking the builds that just ask for
    everything."""

    def setUp(self):
        make_shop()
        self.branch = make_branch(name="Silom")
        self.staff = Staff.objects.create(
            name="Ploy", email="ploy@test.local", password_hash="x", role="cashier",
        )
        self.staff.branches.add(self.branch)
        self.session = BranchSession.objects.create(
            token="pag" * 12, branch=self.branch, staff=self.staff,
        )
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {self.session.token}"}

    def get(self, qs=""):
        res = self.client.get(f"/api/customers{qs}", **self.auth)
        self.assertEqual(res.status_code, 200)
        return res.json()

    def test_no_params_still_returns_the_whole_book_as_a_plain_array(self):
        """Tills already in the shop's hands predate paging and read the body
        as a list. Capping the default would hide customers from them."""
        for i in range(30):
            Customer.objects.create(branch=self.branch, name=f"C{i:02d}")
        body = self.get()
        self.assertIsInstance(body, list)
        self.assertEqual(len(body), 30)

    def test_a_page_is_capped_and_walkable(self):
        for i in range(10):
            Customer.objects.create(branch=self.branch, name=f"C{i:02d}")
        first = self.get("?limit=4")
        self.assertEqual([c["name"] for c in first],
                         ["C00", "C01", "C02", "C03"])
        second = self.get("?limit=4&offset=4")
        self.assertEqual([c["name"] for c in second],
                         ["C04", "C05", "C06", "C07"])

    def test_a_silly_page_size_does_not_break_the_picker(self):
        Customer.objects.create(branch=self.branch, name="Ploy")
        for qs in ("?limit=abc", "?limit=0", "?limit=-5",
                   "?limit=99999", "?limit=5&offset=-3", "?limit=5&offset=xyz"):
            with self.subTest(qs=qs):
                self.assertIsInstance(self.get(qs), list)

    def test_search_finds_a_number_stored_in_the_other_format(self):
        """The picker stores E.164; the cashier reads 0644184887 off a card."""
        Customer.objects.create(
            branch=self.branch, name="Ploy", phone="+66644184887")
        self.assertEqual(len(self.get("?q=0644184887")), 1)
        self.assertEqual(len(self.get("?q=%2B66644184887")), 1)
        self.assertEqual(len(self.get("?q=644184887")), 1)
        self.assertEqual(len(self.get("?q=0644")), 1)

    def test_a_hand_typed_number_is_found_the_way_it_was_typed(self):
        """The digit key spans +66/0, not punctuation: it compares against the
        stored string, and `064-418-4887` contains no run of bare digits to
        match. Such rows are found by typing what is in them.

        Not closed by canonicalising stored numbers, tempting as that is:
        `loyalty.member_for_customer` sends `customer.phone` to the CRM
        verbatim, and a silently reformatted number can look like a new member
        and strand the real one's points."""
        Customer.objects.create(
            branch=self.branch, name="Nok", phone="064-418-4887")
        self.assertEqual(len(self.get("?q=064-418")), 1)
        self.assertEqual(len(self.get("?q=Nok")), 1)

    def test_search_matches_either_half_of_a_name(self):
        Customer.objects.create(
            branch=self.branch, name="Somchai", last_name="Jaidee")
        self.assertEqual(len(self.get("?q=somchai")), 1)
        self.assertEqual(len(self.get("?q=jaidee")), 1)
        self.assertEqual(len(self.get("?q=nobody")), 0)

    def test_a_search_with_no_digits_does_not_match_every_number(self):
        Customer.objects.create(branch=self.branch, name="Ploy", phone="0644184887")
        self.assertEqual(len(self.get("?q=zzz")), 0)

    def test_search_reaches_a_customer_beyond_the_first_page(self):
        """The point of searching server-side: the answer is not required to
        be in the page the till happens to be holding."""
        for i in range(50):
            Customer.objects.create(branch=self.branch, name=f"C{i:02d}")
        Customer.objects.create(branch=self.branch, name="Zebra", phone="0644184887")
        self.assertEqual(len(self.get("?limit=5")), 5)
        found = self.get("?limit=5&q=0644184887")
        self.assertEqual([c["name"] for c in found], ["Zebra"])

    def test_paging_does_not_apply_to_reading_one_customer(self):
        """Slicing the queryset would break every detail route."""
        c = Customer.objects.create(branch=self.branch, name="Ploy")
        res = self.client.get(f"/api/customers/{c.id}?limit=1&q=nope", **self.auth)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["name"], "Ploy")


class NormalisePhoneTests(TestCase):

    def test_one_number_typed_several_ways_is_one_key(self):
        for raw in ("0644184887", "064-418-4887", "064 418 4887",
                    "+66644184887", "0066644184887"):
            with self.subTest(raw=raw):
                self.assertEqual(normalise_phone(raw), "0644184887")

    def test_nothing_to_compare_is_not_a_match(self):
        for raw in ("", "   ", None, "-"):
            with self.subTest(raw=raw):
                self.assertEqual(normalise_phone(raw), "")

    def test_a_foreign_number_is_left_alone_rather_than_guessed_at(self):
        self.assertEqual(normalise_phone("+1 415 555 0123"), "14155550123")


class MergeDuplicatesTests(TestCase):
    """Folding the rows the branch boundary created.

    The duplicates here are one number spelled two ways — ``0812345678`` and
    ``+66812345678`` — because that is what a duplicate looks like now.  A
    stored number belongs to one customer (0047), so two rows carrying the
    identical string cannot coexist in the book; what does survive is the same
    person written the way each screen writes it, which is precisely what the
    merge's normalised key is for.
    """

    def setUp(self):
        make_shop()
        self.silom = make_branch(name="Silom")
        self.thonglor = make_branch(name="Thonglor")

    def merge(self):
        # Just the counts — the undo tape has its own tests below.
        out = merge_duplicates_by_phone(Customer, Order, ParkedOrder)
        return {"groups": out["groups"], "removed": out["removed"]}

    def _order(self, customer, number, branch=None, name=""):
        return Order.objects.create(
            branch=branch or self.silom, order_number=number,
            subtotal=Decimal("100"), total=Decimal("100"),
            payment_method="cash", status="completed",
            customer=customer, customer_name=name or customer.name, staff="Ploy",
        )

    def test_the_row_with_the_most_history_survives(self):
        big = Customer.objects.create(branch=self.silom, name="Ploy", phone="0812345678")
        small = Customer.objects.create(branch=self.thonglor, name="Ploy S", phone="081-234-5678")
        self._order(big, "M-1")
        self._order(big, "M-2")
        self._order(small, "M-3", branch=self.thonglor)

        self.assertEqual(self.merge(), {"groups": 1, "removed": 1})
        self.assertEqual(Customer.objects.count(), 1)
        survivor = Customer.objects.get()
        self.assertEqual(survivor.id, big.id)
        self.assertEqual(survivor.orders.count(), 3)

    def test_what_the_receipt_said_is_not_rewritten(self):
        """A bill is a record of what was printed, not a pointer to be tidied."""
        big = Customer.objects.create(branch=self.silom, name="Ploy", phone="0812345678")
        small = Customer.objects.create(
            branch=self.thonglor, name="Ploy S", phone="+66812345678")
        self._order(big, "M-1")
        self._order(big, "M-2")
        moved = self._order(small, "M-3", branch=self.thonglor)

        self.merge()
        moved.refresh_from_db()
        self.assertEqual(moved.customer_id, big.id)
        self.assertEqual(moved.customer_name, "Ploy S")

    def test_a_blank_field_is_filled_from_the_row_being_absorbed(self):
        """Merging only ever adds to what the shop knows about someone."""
        big = Customer.objects.create(branch=self.silom, name="Ploy", phone="0812345678")
        self._order(big, "M-1")
        Customer.objects.create(
            branch=self.thonglor, name="Ploy S", phone="+66812345678",
            email="ploy@example.com", tax_id="0105563083534", last_name="Sri",
        )

        self.merge()
        survivor = Customer.objects.get()
        self.assertEqual(survivor.email, "ploy@example.com")
        self.assertEqual(survivor.tax_id, "0105563083534")
        self.assertEqual(survivor.last_name, "Sri")
        # Its own values win over the absorbed row's.
        self.assertEqual(survivor.name, "Ploy")
        self.assertEqual(survivor.branch, self.silom)

    def test_a_survivor_with_no_home_branch_inherits_one(self):
        """`branch` is the one carried field whose column name differs from
        its attribute, so it is the one a careless `update_fields` drops."""
        big = Customer.objects.create(branch=None, name="Ploy", phone="0812345678")
        self._order(big, "M-1")
        Customer.objects.create(
            branch=self.thonglor, name="Ploy S", phone="+66812345678")

        self.merge()
        survivor = Customer.objects.get()
        self.assertEqual(survivor.id, big.id)
        self.assertEqual(survivor.branch, self.thonglor)

    def test_a_held_cart_follows_the_merge(self):
        """ParkedOrder holds a bare UUID, so nothing in the database would
        follow it: a cart held over lunch would come back attached to a
        customer that no longer exists."""
        big = Customer.objects.create(branch=self.silom, name="Ploy", phone="0812345678")
        self._order(big, "M-1")
        small = Customer.objects.create(
            branch=self.thonglor, name="Ploy S", phone="+66812345678")
        parked = ParkedOrder.objects.create(
            branch=self.thonglor, name="Table 4", items=[],
            customer_id=small.id, customer_name="Ploy S",
        )

        self.merge()
        parked.refresh_from_db()
        self.assertEqual(parked.customer_id, big.id)

    def test_customers_without_a_phone_are_never_merged(self):
        """Two walk-ins are not the same person just because nobody asked."""
        Customer.objects.create(branch=self.silom, name="Walk-in")
        Customer.objects.create(branch=self.thonglor, name="Walk-in")

        self.assertEqual(self.merge(), {"groups": 0, "removed": 0})
        self.assertEqual(Customer.objects.count(), 2)

    def test_different_numbers_stay_different_people(self):
        Customer.objects.create(branch=self.silom, name="Ploy", phone="0812345678")
        Customer.objects.create(branch=self.thonglor, name="Nok", phone="0899999999")

        self.assertEqual(self.merge(), {"groups": 0, "removed": 0})
        self.assertEqual(Customer.objects.count(), 2)

    def test_running_it_twice_changes_nothing_the_second_time(self):
        """The migration may be replayed on a database that has already had it."""
        Customer.objects.create(branch=self.silom, name="Ploy", phone="0812345678")
        Customer.objects.create(branch=self.thonglor, name="Ploy S", phone="+66812345678")

        self.assertEqual(self.merge(), {"groups": 1, "removed": 1})
        self.assertEqual(self.merge(), {"groups": 0, "removed": 0})
        self.assertEqual(Customer.objects.count(), 1)

    def test_three_rows_for_one_person_collapse_to_one(self):
        # One number, three spellings — the country picker's, the local form,
        # and a row typed by hand with separators. The column holds one
        # customer per stored number, so this is the shape a duplicate takes.
        spellings = ("0812345678", "+66812345678", "081-234-5678")
        for i, (branch, phone) in enumerate(
                zip((self.silom, self.thonglor, self.silom), spellings)):
            Customer.objects.create(
                branch=branch, name=f"Ploy {i}", phone=phone)

        self.assertEqual(self.merge(), {"groups": 1, "removed": 2})
        self.assertEqual(Customer.objects.count(), 1)


class MergeUndoTapeTests(TestCase):
    """The merge deletes rows, so it records how to put them back first.

    A backup of the customer table alone would not be enough: restoring the
    rows without the bill links would leave every moved sale attached to the
    survivor, which is worse than not restoring at all.
    """

    def setUp(self):
        make_shop()
        self.silom = make_branch(name="Silom")
        self.thonglor = make_branch(name="Thonglor")
        self.big = Customer.objects.create(
            branch=self.silom, name="Ploy", phone="0812345678",
            email="ploy@old.com")
        self.small = Customer.objects.create(
            branch=self.thonglor, name="Ploy Sri", phone="+66812345678",
            last_name="Sri")
        for i in range(2):
            self._order(self.big, f"U-{i}")
        self.moved = self._order(self.small, "U-9", branch=self.thonglor)
        self.parked = ParkedOrder.objects.create(
            branch=self.thonglor, name="Table 2", items=[],
            customer_id=self.small.id, customer_name="Ploy Sri")

    def _order(self, customer, number, branch=None):
        return Order.objects.create(
            branch=branch or self.silom, order_number=number,
            subtotal=Decimal("100"), total=Decimal("100"),
            payment_method="cash", status="completed",
            customer=customer, customer_name=customer.name, staff="Ploy")

    def merge(self):
        return merge_duplicates_by_phone(
            Customer, Order, ParkedOrder, CustomerMergeBackup, note="test")

    def test_the_tape_is_written_before_anything_is_deleted(self):
        result = self.merge()
        backup = CustomerMergeBackup.objects.get(id=result["backup_id"])
        # Both customers, as they stood — including the one now deleted.
        names = {r["name"] for r in backup.payload["customers"]}
        self.assertEqual(names, {"Ploy", "Ploy Sri"})
        # And the bill that is about to move, with its previous owner.
        self.assertEqual(backup.payload["moved_orders"],
                         {str(self.moved.id): str(self.small.id)})
        self.assertEqual(backup.payload["moved_parked"],
                         {str(self.parked.id): str(self.small.id)})

    def test_merging_without_a_tape_still_works(self):
        """Tests, and a database already backed up another way."""
        out = merge_duplicates_by_phone(Customer, Order, ParkedOrder)
        self.assertEqual(out["backup_id"], None)
        self.assertEqual(Customer.objects.count(), 1)
        self.assertEqual(CustomerMergeBackup.objects.count(), 0)

    def test_restoring_puts_the_book_back_exactly(self):
        self.merge()
        self.assertEqual(Customer.objects.count(), 1)

        restore_snapshot(Customer, Order, ParkedOrder,
                         CustomerMergeBackup.objects.first())

        self.assertEqual(Customer.objects.count(), 2)
        big = Customer.objects.get(id=self.big.id)
        small = Customer.objects.get(id=self.small.id)
        self.assertEqual(big.orders.count(), 2)
        self.assertEqual(small.orders.count(), 1)
        # The field the merge filled in on the survivor is undone too.
        self.assertEqual(big.last_name, "")
        self.assertEqual(small.last_name, "Sri")
        self.assertEqual(
            ParkedOrder.objects.get(id=self.parked.id).customer_id, small.id)

    def test_no_bill_is_lost_either_way(self):
        self.merge()
        self.assertEqual(Order.objects.count(), 3)
        restore_snapshot(Customer, Order, ParkedOrder,
                         CustomerMergeBackup.objects.first())
        self.assertEqual(Order.objects.count(), 3)

    def test_a_customer_registered_since_the_merge_is_left_alone(self):
        """Restoring undoes the merge, not the trading that happened after it."""
        self.merge()
        newcomer = Customer.objects.create(
            branch=self.silom, name="Newcomer", phone="0877777777")

        restore_snapshot(Customer, Order, ParkedOrder,
                         CustomerMergeBackup.objects.first())

        self.assertTrue(Customer.objects.filter(id=newcomer.id).exists())

    def test_restoring_twice_lands_in_the_same_place(self):
        self.merge()
        backup = CustomerMergeBackup.objects.first()
        restore_snapshot(Customer, Order, ParkedOrder, backup)
        first = sorted(Customer.objects.values_list("name", flat=True))
        restore_snapshot(Customer, Order, ParkedOrder, backup)
        self.assertEqual(sorted(Customer.objects.values_list("name", flat=True)),
                         first)
        self.assertEqual(Order.objects.count(), 3)

    def test_the_tape_survives_being_used(self):
        """Restoring must not consume it — a first restore can be wrong too."""
        self.merge()
        restore_snapshot(Customer, Order, ParkedOrder,
                         CustomerMergeBackup.objects.first())
        self.assertEqual(CustomerMergeBackup.objects.count(), 1)


class NoPhoneOnFileTests(TestCase):
    """"No phone number" is stored as NULL, and is saveable more than once.

    The till's Add Customer form treats an empty phone box as valid and posts
    ``phone: null`` for it, which the column used to refuse — so a cashier
    adding a customer who only gave a name got a 400 and an alert full of JSON.
    The back-office form had the opposite half of the same fault: it posts ""
    and the column took it, which is how both spellings ended up in the book.
    """

    def setUp(self):
        make_shop()
        self.branch = make_branch(name="Silom")
        self.staff = Staff.objects.create(
            name="Ploy", email="ploy@nophone.local", password_hash="x",
            role="cashier",
        )
        self.staff.branches.add(self.branch)
        self.session = BranchSession.objects.create(
            token="nop" * 12, branch=self.branch, staff=self.staff,
        )
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {self.session.token}"}

    def post(self, **body):
        return self.client.post("/api/customers", body,
                                content_type="application/json", **self.auth)

    def test_the_till_can_save_a_customer_who_gave_only_a_name(self):
        """What pos.tsx sends when the phone box is left empty."""
        res = self.post(name="Walk-in", phone=None)

        self.assertEqual(res.status_code, 201)
        self.assertIsNone(Customer.objects.get(name="Walk-in").phone)

    def test_more_than_one_customer_can_have_no_number(self):
        """The reason it is NULL and not "": two NULLs are never equal."""
        for name in ("Walk-in A", "Walk-in B", "Walk-in C"):
            self.assertEqual(self.post(name=name, phone=None).status_code, 201)
        self.assertEqual(Customer.objects.filter(phone__isnull=True).count(), 3)

    def test_an_empty_string_is_stored_as_nothing(self):
        """The back office posts "" for the same fact; one column, one form."""
        res = self.post(name="Walk-in", phone="")

        self.assertEqual(res.status_code, 201)
        self.assertIsNone(Customer.objects.get(name="Walk-in").phone)

    def test_a_field_of_spaces_is_no_number_either(self):
        c = Customer.objects.create(branch=self.branch, name="Walk-in", phone="   ")
        self.assertIsNone(c.phone)

    def test_clearing_a_number_clears_it_to_nothing(self):
        c = Customer.objects.create(
            branch=self.branch, name="Ploy", phone="0812345678")

        res = self.client.patch(
            f"/api/customers/{c.id}", {"phone": ""},
            content_type="application/json", **self.auth,
        )

        self.assertEqual(res.status_code, 200)
        c.refresh_from_db()
        self.assertIsNone(c.phone)

    def test_a_real_number_is_stored_exactly_as_it_was_typed(self):
        """Only emptiness is normalised.

        The CRM is handed ``customer.phone`` verbatim and keys a membership on
        it, so a number tidied on the way in reads there as a different member.
        """
        for typed in ("+66812345678", "081-234-5678", "0812345678"):
            with self.subTest(phone=typed):
                c = Customer.objects.create(
                    branch=self.branch, name=f"C {typed}", phone=typed)
                c.refresh_from_db()
                self.assertEqual(c.phone, typed)

    def test_nobody_is_left_holding_an_empty_string(self):
        """The one thing that must stay true for every reader downstream."""
        self.post(name="Via the till", phone=None)
        self.post(name="Via the back office", phone="")
        Customer.objects.create(branch=self.branch, name="Direct", phone="")

        self.assertFalse(Customer.objects.filter(phone="").exists())

    def test_a_customer_with_no_number_still_lists_and_reads(self):
        c = Customer.objects.create(branch=self.branch, name="Walk-in")

        listed = self.client.get("/api/customers", **self.auth).json()
        self.assertIn("Walk-in", [row["name"] for row in listed])
        one = self.client.get(f"/api/customers/{c.id}", **self.auth).json()
        self.assertIsNone(one["phone"])

    def test_searching_by_number_skips_the_ones_that_have_none(self):
        Customer.objects.create(branch=self.branch, name="Walk-in")
        Customer.objects.create(
            branch=self.branch, name="Ploy", phone="0812345678")

        res = self.client.get("/api/customers?q=0812345678", **self.auth)

        self.assertEqual([row["name"] for row in res.json()], ["Ploy"])


class OneNumberOneCustomerTests(TestCase):
    """A stored phone number belongs to one customer, and the database says so.

    0045 folded the duplicates the branch-scoped book had made, but nothing
    stopped new ones: the till warned and then offered "Save anyway", the
    tax-invoice buyer form never checked at all, and two tills saving the same
    number in the same second both passed their own check — a check that runs a
    moment before the save cannot hold that gap, and only the database can.

    It matters because the number is what the CRM calls someone.  A membership
    is keyed on it and never renamed, so a second row on a number it already
    knows rings up against the first row's member, under the first row's name.
    """

    def setUp(self):
        make_shop()
        self.branch = make_branch(name="Silom")
        self.staff = Staff.objects.create(
            name="Ploy", email="ploy@uniq.local", password_hash="x", role="cashier",
        )
        self.staff.branches.add(self.branch)
        self.session = BranchSession.objects.create(
            token="uni" * 12, branch=self.branch, staff=self.staff,
        )
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {self.session.token}"}

    def post(self, **body):
        return self.client.post("/api/customers", body,
                                content_type="application/json", **self.auth)

    def test_two_customers_cannot_hold_the_same_number(self):
        Customer.objects.create(branch=self.branch, name="Ploy", phone="0812345678")
        with self.assertRaises(IntegrityError), transaction.atomic():
            Customer.objects.create(
                branch=self.branch, name="Ploy again", phone="0812345678")

    def test_the_till_is_told_who_already_has_the_number(self):
        """A 400 naming them, not a 500 — the cashier's next move depends on it."""
        Customer.objects.create(branch=self.branch, name="Somchai", phone="0812345678")

        res = self.post(name="Somchai again", phone="0812345678")

        self.assertEqual(res.status_code, 400)
        self.assertIn("Somchai", str(res.json()["phone"]))

    def test_editing_a_customer_onto_a_taken_number_is_refused(self):
        Customer.objects.create(branch=self.branch, name="Somchai", phone="0812345678")
        other = Customer.objects.create(
            branch=self.branch, name="Nok", phone="0898887777")

        res = self.client.patch(
            f"/api/customers/{other.id}", {"phone": "0812345678"},
            content_type="application/json", **self.auth,
        )

        self.assertEqual(res.status_code, 400)
        other.refresh_from_db()
        self.assertEqual(other.phone, "0898887777")

    def test_a_customer_keeps_its_own_number_through_an_edit(self):
        """The row must not be found to clash with itself."""
        c = Customer.objects.create(branch=self.branch, name="Nok", phone="0898887777")

        res = self.client.patch(
            f"/api/customers/{c.id}", {"name": "Nok S", "phone": "0898887777"},
            content_type="application/json", **self.auth,
        )

        self.assertEqual(res.status_code, 200)
        c.refresh_from_db()
        self.assertEqual(c.name, "Nok S")

    def test_customers_with_no_number_are_exempt(self):
        """The reason the column is NULL and not "": two NULLs are never equal."""
        for name in ("Walk-in A", "Walk-in B", "Walk-in C"):
            self.assertEqual(self.post(name=name, phone=None).status_code, 201)
        self.assertEqual(Customer.objects.filter(phone__isnull=True).count(), 3)

    def test_one_number_spelled_two_ways_is_still_let_through(self):
        """What ``unique`` does not catch, stated so nobody assumes otherwise.

        The index compares stored characters, and the country picker writes
        ``+66…`` where a row typed before it holds ``0…``.  The stored number
        is deliberately left as it was keyed — ``loyalty.member_for_customer``
        hands it to the CRM verbatim, and a tidied one reads there as a new
        member — so closing this means comparing on the normalised key, which
        is what the till's own check and ``merge_customers`` already do.
        """
        Customer.objects.create(branch=self.branch, name="Ploy", phone="0812345678")
        Customer.objects.create(
            branch=self.branch, name="Ploy again", phone="+66812345678")

        self.assertEqual(Customer.objects.count(), 2)
        # And the merge is what folds them back together.
        merge_duplicates_by_phone(Customer, Order, ParkedOrder)
        self.assertEqual(Customer.objects.count(), 1)
