"""Stock-out reasons replace the free-text remark, and the export reports them.

Staff were typing the remark by hand, so the same reason arrived spelled six
different ways and nothing could be grouped or counted.  These tests pin the
three properties that make the replacement worth having:

  * every branch has a reason list — including branches created *after* the
    feature shipped, which is where the DrawerCategory precedent has a gap;
  * a saved document keeps the reason it was saved with, even if the reason
    row is later renamed or deleted;
  * the CSV export carries the SilomPOS column layout the shop reconciles
    against, plus the reason.

Run:
    python manage.py test bravepos.tests.test_stock_out_reasons \\
        --settings=bravepos_api.settings_test
"""
from __future__ import annotations

import csv
import io
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse

from bravepos.models import (
    DEFAULT_STOCK_OUT_REASONS, SUPERSEDED_STOCK_OUT_REASONS, Branch,
    BranchSession, Settings, Staff, StockDocument, StockMovement,
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


class StockMovementExportTests(TestCase):
    """The file has to drop into the shop's existing spreadsheet, so the column
    layout is pinned against the SilomPOS sample it replaces."""

    def setUp(self):
        Settings.objects.get_or_create(id="shop")
        self.password = "correct-horse-battery"
        self.admin = Staff(
            name="Bo", username="bo", email="bo@therollingpinn.com",
            role="admin", active=True, backoffice_access=True,
        )
        self.admin.set_password(self.password)
        self.admin.save()
        self.branch = make_branch(name="Paragon")
        self.product = make_product(self.branch, name="Breakfast Confetti Cookie", stock=50)

        self.staff = Staff.objects.create(
            name="Nok", email="nok2@test.local", password_hash="x", role="admin",
        )
        self.staff.branches.add(self.branch)
        self.session = BranchSession.objects.create(
            token="tk2" * 12, branch=self.branch, staff=self.staff,
        )
        self.client.post(reverse("backoffice:login"),
                         {"username": "bo", "password": self.password})

    def _stock_out(self, reason, qty, barcode="38022"):
        res = self.client.post(
            "/api/stock-documents",
            {
                "type": "out", "reason": reason,
                "items": [{
                    "product_id": str(self.product.id), "barcode": barcode,
                    "product_name": self.product.name, "qty": qty,
                    "price": "10.00", "discount": "0", "total": str(10 * qty),
                }],
            },
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.session.token}",
        )
        self.assertEqual(res.status_code, 201, res.content)

    def _export(self, **params):
        params.setdefault("branch", str(self.branch.id))
        response = self.client.get(reverse("backoffice:stock_movement_export"), params)
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])
        body = response.content.decode("utf-8-sig")
        return list(csv.reader(io.StringIO(body)))

    def test_header_block_matches_the_sample(self):
        rows = self._export()
        # Generic title, exactly as the sample carries it even under a
        # stock-out filter.
        self.assertEqual(rows[0][0], "รายงานการ รับเข้า-จ่ายออก แยกตามสินค้า")
        self.assertEqual(rows[1][0], "Shop")
        self.assertEqual(rows[2][0], "Branch")
        self.assertEqual(rows[2][1], "Paragon")
        self.assertEqual(rows[4][0], "From")
        self.assertEqual(rows[5][0], "To")
        # Blank spacer rows land where the sample has them (rows 4 and 7).
        self.assertEqual(rows[3], [])
        self.assertEqual(rows[6], [])

    def test_column_headers_match_the_sample_then_one_column_per_day(self):
        rows = self._export()
        self.assertEqual(rows[7][:12], [
            "ลำดับ", "รหัส", "สินค้า", "เอกสารรับเข้า", "เอกสารจ่ายออก",
            "จำนวนรับเข้า", "จำนวนจ่ายออก", "มูลค่ารับเข้า", "มูลค่าจ่ายออก",
            "ส่วนลดรับเข้า", "ส่วนลดจ่ายออก", "เหตุผล",
        ])
        self.assertEqual(len(rows[7]), 13, "one trailing column for a single-day window")

    def test_a_stock_out_row_carries_qty_and_reason(self):
        self._stock_out("Expired", 3)
        rows = self._export()
        row = rows[8]
        self.assertEqual(row[0], "1")            # ลำดับ
        self.assertEqual(row[1], "38022")        # รหัส
        self.assertEqual(row[2], "Breakfast Confetti Cookie")
        self.assertEqual(row[3], "0")            # no stock-in documents
        self.assertEqual(row[4], "1")            # one stock-out document
        self.assertEqual(row[6], "3")            # จำนวนจ่ายออก, integer like the sample
        self.assertEqual(row[11], "Expired")
        self.assertEqual(row[12], "3")           # the per-day column

    def test_multiple_reasons_for_one_product_are_listed(self):
        self._stock_out("Expired", 3)
        self._stock_out("Damaged (In-Store)", 2)
        rows = self._export()
        self.assertEqual(rows[8][4], "2")        # two documents
        self.assertEqual(rows[8][6], "5")        # 3 + 2
        self.assertEqual(rows[8][11], "Expired, Damaged (In-Store)")

    def test_totals_row_places_รวม_and_sums_the_numeric_block(self):
        self._stock_out("Expired", 3)
        self._stock_out("Damaged (In-Store)", 2)
        total = self._export()[-1]
        self.assertEqual(total[4], "รวม")
        self.assertEqual(total[6], "5")          # จำนวนจ่ายออก
        self.assertEqual(total[0], "")           # ลำดับ stays blank, as in the sample
        self.assertEqual(total[12], "")          # per-day column is not summed

    def test_stock_in_is_excluded_by_default(self):
        """The sample is a stock-out report; ?type=out is the default."""
        self.client.post(
            "/api/stock-documents",
            {"type": "in", "vendor": "Supplier", "items": [{
                "product_id": str(self.product.id), "product_name": self.product.name,
                "qty": 7, "price": "10.00", "discount": "0", "total": "70.00",
            }]},
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {self.session.token}",
        )
        rows = self._export()
        self.assertEqual(rows[-1][4], "รวม")
        self.assertEqual(len(rows), 9, "only the totals row follows the header")

        both = self._export(type="all")
        self.assertEqual(both[8][5], "7")        # จำนวนรับเข้า now reported

    def test_export_survives_a_deleted_product(self):
        """Lines snapshot name/barcode, so stock that genuinely moved keeps
        being reported after the product row is gone."""
        self._stock_out("Damaged (In-Store)", 4)
        self.product.delete()
        rows = self._export()
        self.assertEqual(rows[8][2], "Breakfast Confetti Cookie")
        self.assertEqual(rows[8][6], "4")

    def test_export_is_scoped_to_the_selected_branch(self):
        self._stock_out("Damaged (In-Store)", 4)
        other = make_branch(name="Central World")
        rows = self._export(branch=str(other.id))
        self.assertEqual(rows[2][1], "Central World")
        self.assertEqual(len(rows), 9, "the other branch's stock-out must not leak in")


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
