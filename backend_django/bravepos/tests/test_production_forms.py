"""Focused parser coverage for compact backoffice production forms."""
from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime, time, timedelta
from pathlib import Path
from unittest.mock import patch

from django.test import RequestFactory, SimpleTestCase, TestCase
from django.urls import reverse
from django.utils import timezone

from backoffice import production_forms
from backoffice.production_views import _request_payload
from bravepos import production
from bravepos.models import (
    Branch, Category, Product, ProductionCutoffPolicy, ProductionRequestItem,
    Settings,
)
from bravepos.tests.test_user_management_and_audit import make_user, sign_in


class ProductionPayloadTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    def post(self, payload):
        return self.factory.post("/backoffice/production/request", {
            "production_payload": json.dumps(payload),
        })

    def test_request_payload_keeps_blank_and_zero_distinct(self):
        payload = production_forms.parse_payload(self.post({
            "version": 1, "kind": "request", "request_id": None,
            "request_revision": 0,
            "rows": [
                {"key": "product", "value": "", "base_value": "0"},
                {"key": "other", "value": "0", "base_value": ""},
            ],
        }), "request")
        self.assertEqual(payload.rows[0]["value"], None)
        self.assertEqual(payload.rows[0]["base_value"], 0)
        self.assertEqual(payload.rows[1]["value"], 0)
        self.assertEqual(payload.rows[1]["base_value"], None)

    def test_packing_requires_a_revision_for_every_row(self):
        request = self.post({
            "version": 1, "kind": "packing", "rows": [
                {"key": "item", "value": "0", "base_value": "", "revision": 4},
            ],
        })
        payload = production_forms.parse_payload(request, "packing")
        self.assertEqual(payload.rows[0]["revision"], 4)
        bad = self.post({
            "version": 1, "kind": "packing", "rows": [
                {"key": "item", "value": "0", "base_value": ""},
            ],
        })
        with self.assertRaises(production_forms.PayloadError):
            production_forms.parse_payload(bad, "packing")

    def test_rejects_duplicate_rows_and_wrong_kind(self):
        duplicate = self.post({
            "version": 1, "kind": "request", "request_id": None, "request_revision": 0,
            "rows": [{"key": "one", "value": "1", "base_value": ""},
                     {"key": "one", "value": "2", "base_value": ""}],
        })
        with self.assertRaises(production_forms.PayloadError):
            production_forms.parse_payload(duplicate, "request")
        wrong = self.post({"version": 1, "kind": "packing", "rows": []})
        with self.assertRaises(production_forms.PayloadError):
            production_forms.parse_payload(wrong, "request")

    def test_rejects_value_and_row_limits(self):
        too_large = self.post({
            "version": 1, "kind": "request", "request_id": None, "request_revision": 0,
            "rows": [{"key": "one", "value": "100000", "base_value": ""}],
        })
        with self.assertRaises(production_forms.PayloadError):
            production_forms.parse_payload(too_large, "request")
        rows = [{"key": str(index), "value": "", "base_value": ""}
                for index in range(production_forms.MAX_ROWS + 1)]
        too_many = self.post({
            "version": 1, "kind": "request", "request_id": None, "request_revision": 0,
            "rows": rows,
        })
        with self.assertRaises(production_forms.PayloadError):
            production_forms.parse_payload(too_many, "request")

    def test_fallback_payload_checks_the_body_limit_before_field_parsing(self):
        request = self.factory.post("/backoffice/production/request", {
            "request_id": "", "request_revision": "0", "qty_product": "1",
        })
        request.META["CONTENT_LENGTH"] = str(production_forms.MAX_BODY_BYTES + 1)
        with self.assertRaises(production_forms.PayloadError):
            _request_payload(request)


class ProductionFormContextTests(TestCase):
    def setUp(self):
        self.branch = Branch.objects.create(name="Form branch")
        self.category = Category.objects.create(branch=self.branch, name="Cookies")
        self.product = Product.objects.create(branch=self.branch, name="Form cookie", category=self.category)
        self.user = make_user(username="forms", email="forms@example.com", role="stock_supervisor")
        sign_in(self.client, self.user)
        self.day = timezone.localdate() + timedelta(days=3)
        self.url = reverse("backoffice:production_request") + f"?branch={self.branch.id}&date={self.day.isoformat()}"

    def test_request_get_exposes_baseline_and_strict_payload_saves(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        baseline = response.context["payload_baseline"]
        self.assertEqual(baseline["kind"], "request")
        row = next(row for row in baseline["rows"] if row["key"] == str(self.product.id))
        row.update(value="7", base_value="")
        response = self.client.post(self.url, {
            "branch_id": str(self.branch.id), "delivery_date": self.day.isoformat(),
            "production_payload": json.dumps(baseline),
        })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ProductionRequestItem.objects.get().requested_qty, 7)

    def test_packing_get_exposes_cell_revision_and_saves_changed_cell(self):
        request_row = production.save_request(self.branch, self.day, {str(self.product.id): 7}, self.user)
        item = request_row.items.get()
        packer = make_user(username="form-packer", email="packer@example.com", role="packer")
        self.client.post(reverse("backoffice:logout"))
        sign_in(self.client, packer)
        url = reverse("backoffice:production_packing") + f"?date={self.day.isoformat()}"
        response = self.client.get(url)
        baseline = response.context["payload_baseline"]
        self.assertEqual(baseline["kind"], "packing")
        row = next(row for row in baseline["rows"] if row["key"] == str(item.id))
        self.assertEqual(row["revision"], 1)
        row.update(value="0", base_value="")
        response = self.client.post(url, {
            "delivery_date": self.day.isoformat(), "mode": "live", "category": "",
            "production_payload": json.dumps(baseline),
        })
        self.assertEqual(response.status_code, 302)
        item.refresh_from_db()
        self.assertEqual(item.packed_qty, 0)

    def test_request_small_fallback_accepts_900_rendered_input_names(self):
        Product.objects.bulk_create([
            Product(branch=self.branch, category=self.category, name=f"Cookie {number}")
            for number in range(899)
        ])
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        baseline = response.context["payload_baseline"]
        self.assertEqual(len(baseline["rows"]), production_forms.MAX_FALLBACK_FIELDS)

        data = {
            "branch_id": str(self.branch.id),
            "delivery_date": self.day.isoformat(),
            "request_id": "",
            "request_revision": "0",
        }
        data.update({f"qty_{row['key']}": "" for row in baseline["rows"]})
        response = self.client.post(self.url, data)
        self.assertEqual(response.status_code, 302)

    def test_large_request_disables_both_save_controls_until_compact_ready(self):
        Product.objects.bulk_create([
            Product(branch=self.branch, category=self.category, name=f"Cookie {number}")
            for number in range(900)
        ])
        response = self.client.get(self.url)
        self.assertTrue(response.context["compact_required"])
        self.assertGreaterEqual(
            len(re.findall(
                r"data-compact-save\s+disabled aria-describedby=\"prCapacity\"",
                response.content.decode(),
            )),
            2,
        )

    def test_request_history_keeps_packed_only_item_without_packing_details(self):
        request_row = production.save_request(
            self.branch, self.day, {str(self.product.id): 3}, self.user,
        )
        item = request_row.items.get()
        production.save_packed(self.day, {str(item.id): 0}, self.user)
        production.save_request(self.branch, self.day, {str(self.product.id): 0}, self.user)

        response = self.client.get(reverse("backoffice:production_requests"))
        self.assertEqual(response.status_code, 200)
        row = next(row for row in response.context["rows"] if row["req"].id == request_row.id)
        self.assertEqual(row["summary"], "No current requested quantities")
        self.assertEqual((row["products"], row["pieces"]), (0, 0))
        self.assertContains(response, "No current requested quantities")
        self.assertNotContains(response, self.product.name)
        self.assertNotContains(response, "Packed by")


class ProductionCompactTemplateTests(SimpleTestCase):
    """Run the rendered compact-submit functions in Node without a browser DOM."""

    template_dir = Path(__file__).resolve().parents[2] / "backoffice" / "templates" / "backoffice"

    def test_compact_submit_omits_1003_legacy_fields_and_restores_them(self):
        node_program = r'''
const fs = require('fs');

function functionSource(source, name) {
  const start = source.indexOf('function ' + name + '() {');
  if (start < 0) throw Error('Missing ' + name);
  const brace = source.indexOf('{', start);
  let depth = 0;
  for (let index = brace; index < source.length; index += 1) {
    if (source[index] === '{') depth += 1;
    if (source[index] === '}' && --depth === 0) return source.slice(start, index + 1);
  }
  throw Error('Unclosed ' + name);
}

for (const [filename, prefix] of [
  ['production_request.html', 'qty_'],
  ['production_packing.html', 'packed_'],
]) {
  const source = fs.readFileSync(filename, 'utf8');
  let boxes = Array.from({length: 1003}, (_, index) => ({name: prefix + index, disabled: false}));
  let payload = {name: 'production_payload', disabled: true, value: ''};
  let baseline = {name: 'packing_baseline', disabled: false, value: '{}'};
  if (filename === 'production_request.html') baseline.disabled = true;
  function compact() { payload.value = '{"version":1}'; }
  eval(functionSource(source, 'submitCompact'));
  eval(functionSource(source, 'restoreLegacyInputs'));
  submitCompact();
  const multipartNames = boxes.concat([payload, baseline]).filter((field) => !field.disabled).map((field) => field.name);
  if (multipartNames.length !== 1 || multipartNames[0] !== 'production_payload') {
    throw Error(filename + ' still submits legacy quantity fields');
  }
  restoreLegacyInputs();
  const restoredNames = boxes.concat([payload, baseline]).filter((field) => !field.disabled).map((field) => field.name);
  const expectedRestored = filename === 'production_packing.html' ? 1004 : 1003;
  if (restoredNames.length !== expectedRestored || restoredNames.filter((name) => name.startsWith(prefix)).length !== 1003) {
    throw Error(filename + ' does not restore legacy quantity fields');
  }
}
'''
        result = subprocess.run(
            ["node", "-e", node_program],
            cwd=self.template_dir,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_templates_use_multipart_and_server_capacity_gates(self):
        request = (self.template_dir / "production_request.html").read_text()
        packing = (self.template_dir / "production_packing.html").read_text()
        for template in (request, packing):
            self.assertIn('enctype="multipart/form-data"', template)
            self.assertRegex(
                template,
                r"data-compact-save\s*{% if compact_required %}\s*disabled",
            )
            self.assertIn('event.defaultPrevented', template)
            self.assertIn('restoreLegacyInputs();', template)
        self.assertIn('form="pkForm" name="reload_saved"', packing)
        self.assertIn('if (!boxes.length) return;', packing)
        self.assertIn('cell.conflict.packed_qty', packing)


class ProductionLegacyDeadlinePresentationTests(TestCase):
    """Only deadlines already past at baseline adoption are unknowable."""

    def setUp(self):
        self.now = timezone.make_aware(datetime(2026, 11, 16, 13, 0))
        self.historic_day = self.now.date() - timedelta(days=1)
        self.future_day = self.now.date() + timedelta(days=2)
        Settings.objects.update_or_create(
            id="shop", defaults={"production_cutoff": time(18, 0)},
        )
        ProductionCutoffPolicy.objects.all().delete()
        ProductionCutoffPolicy.objects.create(
            effective_from=datetime.min.date(),
            cutoff=time(18, 0),
            activated_at=timezone.make_aware(datetime(2026, 11, 16, 12, 0)),
            source=ProductionCutoffPolicy.Source.LEGACY_BASELINE,
        )
        self.user = make_user(username="legacy-kitchen", email="legacy-kitchen@example.com", role="viewer")
        sign_in(self.client, self.user)

    def _get_kitchen(self, day):
        with patch("backoffice.production_views.timezone.now", return_value=self.now):
            return self.client.get(
                reverse("backoffice:production_kitchen") + f"?date={day.isoformat()}"
            )

    def test_historic_kitchen_omits_clamped_adoption_timestamp(self):
        self.assertTrue(production.is_legacy_deadline(self.historic_day))
        response = self._get_kitchen(self.historic_day)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.context["deadline_legacy"])
        self.assertContains(response, "original deadline is unavailable")
        self.assertNotContains(response, "requests closed at")

    def test_future_baseline_deadline_remains_exact_and_known(self):
        self.assertFalse(production.is_legacy_deadline(self.future_day))
        response = self._get_kitchen(self.future_day)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.context["deadline_legacy"])
        self.assertEqual(response.context["deadline"], timezone.make_aware(
            datetime.combine(self.future_day - timedelta(days=1), time(18, 0))
        ))
        self.assertContains(response, "Not final")
        self.assertNotContains(response, "original deadline is unavailable")
