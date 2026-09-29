"""Stock-out reasons replace the free-text remark.

Staff were typing the remark by hand, so the same reason arrived spelled six
different ways and nothing could be grouped or counted.  These tests pin the
two properties that make the replacement worth having:

  * every branch has a reason list — including branches created *after* the
    feature shipped, which is where the DrawerCategory precedent has a gap;
  * a saved document keeps the reason it was saved with, even if the reason
    row is later renamed or deleted.

Run:
    python manage.py test bravepos.tests.test_stock_out_reasons \\
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

from decimal import Decimal

from django.test import TestCase

from bravepos.models import (
    DEFAULT_STOCK_OUT_REASONS, SUPERSEDED_STOCK_OUT_REASONS, Branch,
    BranchSession, Staff, StockDocument, StockMovement,
    StockOutReason,
)

from .factories import make_branch, make_product, make_shop


class StockOutReasonSeedTests(TestCase):
    def test_a_new_branch_gets_the_default_reasons(self):
        """The migration only seeds branches that already existed.  Without the
        post_save receiver a branch opened later has an empty dropdown, and
        since the reason is the only remark field it could record no stock-out
        at all."""
        branch = Branch.objects.create(name="Brand New", code="NEW")
        names = list(
            StockOutReason.objects.filter(branch=branch)
            .order_by("sort_order").values_list("name", flat=True)
        )
        self.assertEqual(names, [n for n, _th in DEFAULT_STOCK_OUT_REASONS])

    def test_reasons_carry_thai_labels(self):
        branch = make_branch(name="Thai Labels")
        expired = StockOutReason.objects.get(branch=branch, name="Expired")
        self.assertEqual(expired.name_th, "หมดอายุ")

    def test_reseeding_a_branch_does_not_duplicate(self):
        """post_save fires on every save; only creation should seed."""
        branch = make_branch(name="Resaved")
        branch.name = "Resaved twice"
        branch.save()
        self.assertEqual(
            StockOutReason.objects.filter(branch=branch).count(),
            len(DEFAULT_STOCK_OUT_REASONS),
        )

    def test_reasons_are_scoped_to_their_branch(self):
        a, b = make_branch(name="A"), make_branch(name="B")
        StockOutReason.objects.create(branch=a, name="A-only")
        self.assertFalse(StockOutReason.objects.filter(branch=b, name="A-only").exists())


class StockOutReasonApiTests(TestCase):
    def setUp(self):
        make_shop()
        self.branch = make_branch()
        self.product = make_product(self.branch, stock=50)
        self.staff = Staff.objects.create(
            name="Nok", email="nok@test.local", password_hash="x", role="admin",
        )
        self.staff.branches.add(self.branch)
        self.session = BranchSession.objects.create(
            token="tok" * 12, branch=self.branch, staff=self.staff,
        )
        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {self.session.token}"}

    def test_list_returns_only_this_branch_active_reasons(self):
        other = make_branch(name="Elsewhere")
        StockOutReason.objects.create(branch=other, name="Not mine")
        hidden = StockOutReason.objects.get(branch=self.branch, name="Stock Transfer")
        hidden.active = False
        hidden.save()

        res = self.client.get("/api/stock-out-reasons?active=true", **self.auth)
        self.assertEqual(res.status_code, 200, res.content)
        names = [r["name"] for r in res.json()]
        self.assertNotIn("Not mine", names)
        self.assertNotIn("Stock Transfer", names)
        self.assertIn("Expired", names)

    def test_reasons_require_a_session(self):
        self.assertEqual(self.client.get("/api/stock-out-reasons").status_code, 401)

    def _stock_out(self, reason="Expired", qty=3):
        return self.client.post(
            "/api/stock-documents",
            {
                "type": "out",
                "reason": reason,
                "receiver": "Kitchen",
                "items": [{
                    "product_id": str(self.product.id),
                    "barcode": "38022", "product_name": self.product.name,
                    "qty": qty, "price": "10.00", "discount": "0", "total": "30.00",
                }],
            },
            content_type="application/json",
            **self.auth,
        )

    def test_stock_out_stores_the_reason(self):
        res = self._stock_out()
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.json()["reason"], "Expired")
        self.assertEqual(StockDocument.objects.get().reason, "Expired")

    def test_the_reason_reaches_the_movement_ledger(self):
        """The Inventory screen shows movements, not documents — a reason that
        stops at the document is invisible where staff actually look."""
        self._stock_out()
        self.assertEqual(StockMovement.objects.get(type="out").note, "Expired")

    def test_renaming_a_reason_does_not_rewrite_saved_documents(self):
        """The reason is snapshotted as text for the same reason
        ShiftMovement.category is: history must not move under later edits."""
        self._stock_out()
        row = StockOutReason.objects.get(branch=self.branch, name="Expired")
        row.name = "Spoilage"
        row.save()
        self.assertEqual(StockDocument.objects.get().reason, "Expired")

    def test_deleting_a_reason_does_not_touch_saved_documents(self):
        self._stock_out()
        StockOutReason.objects.filter(branch=self.branch, name="Expired").delete()
        self.assertEqual(StockDocument.objects.get().reason, "Expired")

    def test_reason_is_ignored_on_a_stock_in(self):
        """Only stock-out has reasons; a stray field must not land on stock-in."""
        res = self.client.post(
            "/api/stock-documents",
            {
                "type": "in", "reason": "Expired", "vendor": "Supplier",
                "items": [{
                    "product_id": str(self.product.id), "product_name": self.product.name,
                    "qty": 5, "price": "10.00", "discount": "0", "total": "50.00",
                }],
            },
            content_type="application/json",
            **self.auth,
        )
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(StockDocument.objects.get(type="in").reason, "")


class StockDocumentReasonMigrationTests(TestCase):
    def test_existing_branches_were_seeded_by_the_migration(self):
        """make_branch goes through the signal, so assert against a branch the
        migration itself would have covered — the data migration and the
        receiver must produce the same list."""
        branch = make_branch(name="Seeded")
        self.assertEqual(
            StockOutReason.objects.filter(branch=branch).count(),
            len(DEFAULT_STOCK_OUT_REASONS),
        )
        self.assertEqual(
            [r.name_th for r in StockOutReason.objects.filter(branch=branch).order_by("sort_order")],
            [th for _n, th in DEFAULT_STOCK_OUT_REASONS],
        )


class StockOutReasonRecodeTests(TestCase):
    """Migration 0040 re-points a live branch at the shop's sheet codes.

    Every branch in production carries the 0034 list, so the swap — not the
    fresh-install path the other tests cover — is what actually runs.  What it
    must not do is take a reason an admin made themselves along with it.
    """

    def setUp(self):
        self.branch = make_branch(name="Recoded")
        # Roll the branch back to the list 0034 seeded.
        StockOutReason.objects.filter(branch=self.branch).delete()
        for i, name in enumerate(SUPERSEDED_STOCK_OUT_REASONS):
            StockOutReason.objects.create(branch=self.branch, name=name, sort_order=i)

    def _migrate(self):
        import importlib

        from django.apps import apps as registry

        module = importlib.import_module(
            "bravepos.migrations.0040_stock_out_reason_codes"
        )
        module.to_sheet_codes(registry, None)
        return module

    def _names(self):
        return list(
            StockOutReason.objects.filter(branch=self.branch)
            .order_by("sort_order").values_list("name", flat=True)
        )

    def test_the_old_list_is_replaced_by_the_sheet_codes(self):
        self._migrate()
        self.assertEqual(self._names(), [n for n, _th in DEFAULT_STOCK_OUT_REASONS])

    def test_a_hand_added_reason_survives_below_the_defaults(self):
        StockOutReason.objects.create(
            branch=self.branch, name="Dropped on the floor", sort_order=99,
        )
        self._migrate()
        self.assertEqual(
            self._names(),
            [n for n, _th in DEFAULT_STOCK_OUT_REASONS] + ["Dropped on the floor"],
        )

    def test_a_renamed_default_is_a_human_decision_and_stays(self):
        row = StockOutReason.objects.get(branch=self.branch, name="Complimentary")
        row.name = "On the house"
        row.save()
        self._migrate()
        self.assertIn("On the house", self._names())
        self.assertNotIn("Complimentary", self._names())

    def test_running_it_twice_changes_nothing(self):
        self._migrate()
        first = self._names()
        self._migrate()
        self.assertEqual(self._names(), first)

    def test_other_branches_are_recoded_too(self):
        other = make_branch(name="Samyan")
        self._migrate()
        self.assertEqual(
            list(
                StockOutReason.objects.filter(branch=other)
                .order_by("sort_order").values_list("name", flat=True)
            ),
            [n for n, _th in DEFAULT_STOCK_OUT_REASONS],
        )

    def test_saved_documents_keep_the_wording_they_were_saved_with(self):
        """The whole point of the text snapshot: retiring a code must not
        rewrite a stock-out that already happened."""
        doc = StockDocument.objects.create(
            branch=self.branch, type="out", reason="Waste / Expired",
        )
        self._migrate()
        doc.refresh_from_db()
        self.assertEqual(doc.reason, "Waste / Expired")


class StockDocumentDecimalTests(TestCase):
    """Guard the quantity formatter: the sample prints whole numbers, but the
    column is a Decimal and a fractional unit must not silently truncate."""

    def test_whole_and_fractional_quantities(self):
        from backoffice.views import _stock_qty
        self.assertEqual(_stock_qty(Decimal("3.00")), "3")
        self.assertEqual(_stock_qty(Decimal("0")), "0")
        self.assertEqual(_stock_qty(Decimal("2.50")), "2.50")
