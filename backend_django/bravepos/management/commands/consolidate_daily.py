"""Roll a day's Brave POS sales up into one consolidated figure per branch.

This is the Brave POS counterpart of the SilomPOS consolidation job: for a
given local date it groups every sold bill by branch, merges the line items,
and produces the branch-day totals a consolidated Peak receipt would be built
from.

It deliberately stops one step short of issuing that receipt. No receipt is
sent to Peak and nothing is written to the database — the command only *reads*
orders and prints the result, so it is safe to run repeatedly, on prod,
mid-day, or over a backfill range. The receipt submission (and whatever row
records it) is a separate change; :func:`_branch_payload` is the seam it will
consume.

The one exception is ``--create-peak-contact``, which creates the standing
"bravepos" customer every consolidated receipt is booked to — the Brave POS
counterpart of the SilomPOS job's fixed ``PEAK_CONTACT_ID``. Run it once per
Peak account and put the id it prints in ``PEAK_BRAVEPOS_CONTACT_ID``:

    python manage.py consolidate_daily --create-peak-contact

Run it once a day, after the last branch has closed, for the previous day:

    15 4 * * * cd /srv/bravepos && .venv/bin/python manage.py consolidate_daily --json >> /var/log/bravepos/consolidate.log

What is counted, and why:

* **Cancelled bills are excluded.** A void sets ``status='cancel'`` and the
  bill's money never existed — same rule ``_shift_summary`` uses for its
  ``sold`` set, so the two reports agree.
* **Bills that already have their own Peak receipt are excluded.** When a
  customer scans the receipt QR and submits the full-tax-invoice form, that
  single bill is documented at Peak on its own (``peak_queue_id`` is set).
  Consolidating it again would book the same sale twice. A bill with only
  ``pos_tax_invoice`` set — a slip the cashier printed in-app — never reached
  Peak and so stays in the consolidation.
* **Line items are keyed by product *and* unit price.** Grouping on the
  product alone would collapse a mid-day price change into one row whose
  ``qty × price`` no longer equals what was actually charged, and the
  difference would have to be smuggled out as a fake discount. One row per
  price keeps every row arithmetically true.

Both of those exclusions are decided at the moment the day is billed, and a
day can change afterwards: a customer asks for a full tax invoice on Thursday
for a bill rung on Monday, or a mistake is voided the next morning. The
receipt already filed for that day is then wrong — the sale is billed twice,
or billed when it should not be. So ``--issue`` ends with a **reissue sweep**:
every ``ConsolidatedReceipt`` a write path flagged ``needs_reissue`` is voided
at Peak and replaced with one rebuilt from the orders as they stand now. This
is the SilomPOS ``is_reconsolidate`` filter under our name for it, and unlike
ordinary issuing it runs for any date — a flagged row is a replacement for a
document already known to be wrong, not a new document nobody asked for.
"""
from __future__ import annotations

import json
import uuid
from datetime import date as date_cls, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from django.core.management.base import BaseCommand, CommandError
from django.db.models import Prefetch, Q
from django.utils import timezone as djtz

from bravepos.models import Branch, ConsolidatedReceipt, Order, OrderItem
from bravepos.peak import (
    CONSOLIDATION_CONTACT_ID_ENV,
    CONSOLIDATION_CONTACT_NAME,
    PeakVoidFailed,
    consolidation_contact_id,
    create_consolidation_contact,
    issue_consolidated_receipt,
    retire_consolidated_receipt,
)


ZERO = Decimal("0")
SATANG = Decimal("0.01")

# Branches that must never be consolidated. Their sales are not real takings,
# so a receipt for them would file fictitious revenue with the Revenue
# Department. Excluded whenever the command runs across "every branch" — which
# is how the nightly cron runs it, so the rule holds even though nobody passes
# a flag. Naming one of these explicitly with --branch still works, for when
# you want to look at its figures without issuing anything.
EXCLUDED_BRANCH_NAMES = {"test branch"}


def _money(value) -> Decimal:
    """Round to satang, half-up. Applied once per reported figure, never
    inside the accumulation loop, so per-bill rounding can't drift."""
    return (value or ZERO).quantize(SATANG, rounding=ROUND_HALF_UP)


def _f(value) -> float:
    """Satang-rounded Decimal as a JSON-friendly float."""
    return float(_money(value))


class Command(BaseCommand):
    help = (
        "Consolidate a day's sales into one payload per branch. "
        "Read-only: submits nothing to Peak and writes nothing to the DB."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--date",
            help="Local (Asia/Bangkok) date to consolidate, YYYY-MM-DD. "
                 "Default: yesterday.",
        )
        parser.add_argument(
            "--days", type=int, default=1,
            help="Consolidate this many consecutive days, ending on --date. "
                 "Use for backfills.",
        )
        parser.add_argument(
            "--branch", action="append", default=[], metavar="ID_OR_NAME",
            help="Limit to this branch (id, code, or name). Repeatable.",
        )
        parser.add_argument(
            "--create-peak-contact", action="store_true",
            help=f"Create the '{CONSOLIDATION_CONTACT_NAME}' customer in Peak "
                 f"and print its contact id. Run this ONCE per Peak account, "
                 f"then put the id in {CONSOLIDATION_CONTACT_ID_ENV}.",
        )
        parser.add_argument(
            "--issue", action="store_true",
            help="Actually issue the consolidated receipts to Peak. "
                 "Yesterday only. Without this the command is read-only.",
        )
        parser.add_argument(
            "--reissue", action="store_true",
            help="With --issue: void the existing receipt for a branch-day "
                 "and replace it. Use after a late or voided bill.",
        )
        parser.add_argument(
            "--backfill", action="store_true",
            help="With --issue: lift the yesterday-only rule and issue every "
                 "day in the --date/--days range. For catching up history.",
        )
        parser.add_argument(
            "--skip-reissue-sweep", action="store_true",
            help="With --issue: do NOT correct branch-days flagged stale by a "
                 "late tax invoice or void. They stay flagged for a later run.",
        )
        parser.add_argument(
            "--skip-date", action="append", default=[], metavar="YYYY-MM-DD",
            help="Leave this date out entirely — not consolidated, not "
                 "issued. Repeatable. Use for days already billed by hand.",
        )
        parser.add_argument(
            "--json", action="store_true",
            help="Emit the payload as JSON instead of a human summary.",
        )
        parser.add_argument(
            "--out", metavar="PATH",
            help="Write the JSON payload to this file as well.",
        )

    # ── entry point ──────────────────────────────────────────────────────
    def handle(self, *args, **opts):
        end = self._resolve_date(opts.get("date"))
        days = opts["days"]
        if days < 1:
            raise CommandError("--days must be at least 1")

        branches = self._resolve_branches(opts["branch"])
        contact_id = self._resolve_contact(opts["create_peak_contact"])

        if opts["issue"]:
            # Yesterday only, unless --backfill says otherwise. A receipt is a
            # filed document, and a stray --days would otherwise issue a dozen
            # of them in one go without anyone having asked for it.
            yesterday = djtz.localdate() - timedelta(days=1)
            if not opts["backfill"] and (days != 1 or end != yesterday):
                raise CommandError(
                    f"--issue only runs for yesterday ({yesterday}); "
                    f"got {end} over {days} day(s). Add --backfill to issue "
                    f"a range deliberately."
                )
            if not contact_id:
                raise CommandError(
                    f"Cannot issue without the '{CONSOLIDATION_CONTACT_NAME}' "
                    f"Peak customer. Run --create-peak-contact once, then set "
                    f"{CONSOLIDATION_CONTACT_ID_ENV}."
                )
        elif opts["reissue"]:
            raise CommandError("--reissue only means anything with --issue.")
        elif opts["backfill"]:
            raise CommandError("--backfill only means anything with --issue.")

        skipped = {self._parse_date(s, "--skip-date") for s in opts["skip_date"]}
        dates = [
            d for d in (end - timedelta(days=i) for i in range(days))
            if d not in skipped
        ]
        dates.reverse()
        if not dates:
            raise CommandError("--skip-date excluded every day in the range.")
        for d in sorted(skipped):
            self.stdout.write(f"skipping {d}")

        payload = {
            "platform": "bravepos",
            # The customer every consolidated receipt in this payload is
            # booked to — one standing contact, not the walk-in buyers on the
            # bills.  Carried here so the receipt step reads it off the
            # payload instead of resolving it a second time.
            "customer": {
                "name": CONSOLIDATION_CONTACT_NAME,
                "peak_contact_id": contact_id,
            },
            "generated_at": djtz.localtime().isoformat(),
            "days": [self._consolidate(d, branches) for d in dates],
        }

        if opts.get("out"):
            with open(opts["out"], "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=2)

        if opts["json"]:
            self.stdout.write(json.dumps(payload, ensure_ascii=False))
        else:
            self._print_summary(payload)

        if opts["issue"]:
            self._issue(payload, contact_id, reissue=opts["reissue"])
            # After the ordinary billing, not before: a day billed tonight is
            # built from the orders as they stand, so it is never stale, and
            # doing it in this order keeps the flagged-day corrections at the
            # bottom of the log where someone reading it will see them.
            if not opts["skip_reissue_sweep"]:
                self._reissue_sweep(contact_id, branches)

    # ── argument resolution ──────────────────────────────────────────────
    def _resolve_contact(self, create: bool) -> str:
        """Return the Peak contact id consolidated receipts are booked to.

        The ONLY thing in this command that can write to Peak, and only behind
        --create-peak-contact.  Creating is refused once an id is configured:
        Peak doesn't upsert on contact name, so a second run would leave two
        "bravepos" customers and silently split the branch-days between them.
        """
        existing = consolidation_contact_id()

        if create:
            if existing:
                raise CommandError(
                    f"A consolidation contact is already configured "
                    f"({existing}). Creating another would leave two "
                    f"'{CONSOLIDATION_CONTACT_NAME}' customers in Peak — "
                    f"clear {CONSOLIDATION_CONTACT_ID_ENV} first if you "
                    f"really mean to replace it."
                )
            new_id = create_consolidation_contact()
            self.stdout.write(self.style.SUCCESS(
                f"Created Peak customer '{CONSOLIDATION_CONTACT_NAME}' "
                f"→ {new_id}"
            ))
            self.stdout.write(
                f"Persist it now, or the next run creates a second one:\n"
                f"    {CONSOLIDATION_CONTACT_ID_ENV}={new_id}"
            )
            return new_id

        if not existing:
            # Not fatal: the consolidation figures are still correct and
            # useful without it. Only the (not yet built) receipt step needs
            # the id, so this is a warning rather than a hard stop.
            self.stderr.write(self.style.WARNING(
                f"No Peak contact configured — run once with "
                f"--create-peak-contact, then set "
                f"{CONSOLIDATION_CONTACT_ID_ENV}."
            ))
        return existing

    def _parse_date(self, raw: str, flag: str) -> date_cls:
        try:
            return datetime.strptime(raw, "%Y-%m-%d").date()
        except ValueError:
            raise CommandError(f"{flag} must be YYYY-MM-DD, got {raw!r}")

    def _resolve_date(self, raw: str | None) -> date_cls:
        if not raw:
            # Yesterday in Asia/Bangkok, not UTC: a bill rung at 00:30 local
            # is still yesterday's UTC date, and windowing on the wrong one
            # would split a trading day down the middle.
            return djtz.localdate() - timedelta(days=1)
        return self._parse_date(raw, "--date")

    def _resolve_branches(self, wanted: list[str]) -> list[Branch] | None:
        """``None`` means "every branch that traded"; a list narrows to those
        branches. An unmatched --branch is an error rather than a silent
        empty report — a typo'd branch name must not read as "no sales"."""
        if not wanted:
            return None

        found: list[Branch] = []
        for token in wanted:
            match = Q(name__iexact=token) | Q(code__iexact=token)
            try:
                match |= Q(id=uuid.UUID(token))
            except (ValueError, AttributeError, TypeError):
                pass
            branch = Branch.objects.filter(match).first()
            if not branch:
                raise CommandError(f"No branch matches {token!r}")
            found.append(branch)
        return found

    # ── consolidation ────────────────────────────────────────────────────
    def _consolidate(self, day: date_cls, branches: list[Branch] | None) -> dict:
        orders = (
            Order.objects
            .filter(created_at__date=day)
            .select_related("branch")
            .prefetch_related(Prefetch("items", queryset=OrderItem.objects.all()))
            .order_by("created_at")
        )
        if branches is not None:
            orders = orders.filter(branch__in=branches)
        else:
            # Running across every branch: drop the ones that must never be
            # billed. Naming a branch explicitly bypasses this.
            orders = orders.exclude(branch__name__in=EXCLUDED_BRANCH_NAMES)

        # One pass over the day, cancelled and already-invoiced bills
        # included, so the report can say what it left out instead of
        # quietly reporting a smaller number than the till did.
        buckets: dict[str, dict] = {}
        for order in orders:
            bucket = buckets.setdefault(
                str(order.branch_id or ""), self._new_bucket(order.branch),
            )

            if order.status == "cancel":
                bucket["cancelled_count"] += 1
                bucket["cancelled_total"] += order.total or ZERO
                continue
            if order.peak_queue_id:
                bucket["own_receipt_count"] += 1
                bucket["own_receipt_total"] += order.total or ZERO
                continue

            self._add_order(bucket, order)

        day_payload = {
            "date": day.isoformat(),
            "branches": [
                self._branch_payload(b)
                for b in sorted(buckets.values(), key=lambda b: b["branch_name"])
            ],
        }
        return day_payload

    def _new_bucket(self, branch: Branch | None) -> dict:
        return {
            "branch": branch,
            "branch_name": (branch.name if branch else "(no branch)"),
            "items": {},
            "payments": {},
            "order_numbers": [],
            "order_ids": [],
            "items_gross": ZERO,
            "discount_total": ZERO,
            "vat_amount": ZERO,
            "processing_fee": ZERO,
            "processing_fee_vat": ZERO,
            "grand_total": ZERO,
            "cancelled_count": 0,
            "cancelled_total": ZERO,
            "own_receipt_count": 0,
            "own_receipt_total": ZERO,
        }

    def _add_order(self, bucket: dict, order: Order) -> None:
        bucket["order_numbers"].append(order.order_number)
        bucket["order_ids"].append(str(order.id))
        bucket["discount_total"] += order.discount_amount or ZERO
        bucket["vat_amount"] += order.vat_amount or ZERO
        bucket["processing_fee"] += order.processing_fee or ZERO
        bucket["processing_fee_vat"] += order.processing_fee_vat or ZERO
        bucket["grand_total"] += order.total or ZERO

        method = order.payment_method or "cash"
        pay = bucket["payments"].setdefault(method, {"count": 0, "amount": ZERO})
        pay["count"] += 1
        pay["amount"] += order.total or ZERO

        for item in order.items.all():
            qty = Decimal(item.qty or 0)
            price = item.price or ZERO
            if qty <= 0:
                continue

            # Product id when we still have one, otherwise the snapshot name:
            # an item whose product was deleted (product FK is SET_NULL) must
            # still consolidate under its own line rather than vanish.
            ident = str(item.product_id) if item.product_id else f"name:{item.name}"
            key = (ident, price)
            row = bucket["items"].setdefault(key, {
                "product_id": str(item.product_id) if item.product_id else None,
                "sku": (item.product.sku if item.product_id and item.product else ""),
                "name": item.name,
                "category": item.category_name,
                "unit_price": price,
                "qty": ZERO,
                "discount": ZERO,
            })
            row["qty"] += qty
            row["discount"] += item.discount or ZERO
            bucket["items_gross"] += qty * price

    def _branch_payload(self, bucket: dict) -> dict:
        branch = bucket["branch"]
        items = [
            {
                "product_id": row["product_id"],
                "sku": row["sku"],
                "name": row["name"],
                "category": row["category"],
                "qty": float(row["qty"]),
                "unit_price": _f(row["unit_price"]),
                "gross": _f(row["qty"] * row["unit_price"]),
                "discount": _f(row["discount"]),
                "net": _f(row["qty"] * row["unit_price"] - row["discount"]),
            }
            for row in bucket["items"].values()
        ]
        items.sort(key=lambda r: (-r["net"], r["name"]))

        goods_total = bucket["items_gross"] - bucket["discount_total"]
        # Goods plus what the customer covered on top (card processing fee and
        # its VAT) must reconcile to what was actually charged. Reported rather
        # than corrected: a non-zero variance means the order rows disagree
        # with their own line items, which is a data bug to chase, not a
        # rounding artefact to bury in a discount line.
        variance = bucket["grand_total"] - (
            goods_total + bucket["processing_fee"] + bucket["processing_fee_vat"]
        )

        numbers = bucket["order_numbers"]
        return {
            "branch_id": str(branch.id) if branch else None,
            "branch_name": bucket["branch_name"],
            "branch_code": (branch.code if branch else ""),
            # Carried through for the eventual Peak step: a branch left on the
            # default "BSV003" is sent as a payment-method code, a branch with
            # a real chart-of-accounts code as an accountCode.
            "peak_account_code": (
                (branch.peak_account_code or "BSV003").strip() or "BSV003"
                if branch else "BSV003"
            ),
            "order_count": len(numbers),
            "first_order_number": numbers[0] if numbers else "",
            "last_order_number": numbers[-1] if numbers else "",
            # Exactly which bills this branch-day bills, so the receipt row
            # can link them and a later audit can prove what was counted.
            "order_ids": bucket["order_ids"],
            "items": items,
            "items_gross": _f(bucket["items_gross"]),
            "discount_total": _f(bucket["discount_total"]),
            "goods_total": _f(goods_total),
            "vat_amount": _f(bucket["vat_amount"]),
            "processing_fee": _f(bucket["processing_fee"]),
            "processing_fee_vat": _f(bucket["processing_fee_vat"]),
            "grand_total": _f(bucket["grand_total"]),
            "variance": _f(variance),
            "payments": [
                {"method": m, "count": p["count"], "amount": _f(p["amount"])}
                for m, p in sorted(
                    bucket["payments"].items(),
                    key=lambda kv: kv[1]["amount"], reverse=True,
                )
            ],
            "excluded": {
                "cancelled_count": bucket["cancelled_count"],
                "cancelled_total": _f(bucket["cancelled_total"]),
                "own_peak_receipt_count": bucket["own_receipt_count"],
                "own_peak_receipt_total": _f(bucket["own_receipt_total"]),
            },
        }

    # ── issuance ─────────────────────────────────────────────────────────
    def _issue(self, payload: dict, contact_id: str, reissue: bool) -> None:
        """Issue one Peak receipt per branch-day in the payload.

        Each branch is isolated: one branch-day Peak rejects must not stop the
        others from being billed, or a single bad branch leaves the whole
        day's takings unrecorded.
        """
        for day_payload in payload["days"]:
            self._issue_day(day_payload, contact_id, reissue)

    def _issue_day(self, day_payload: dict, contact_id: str, reissue: bool) -> None:
        day = datetime.strptime(day_payload["date"], "%Y-%m-%d").date()
        self.stdout.write(self.style.MIGRATE_HEADING(f"issuing {day}"))

        for bp in day_payload["branches"]:
            label = bp["branch_name"]
            if label in EXCLUDED_BRANCH_NAMES:
                # Reachable only when someone named it with --branch. Viewing
                # its figures is fine; billing them is not, at any prompting.
                self.stderr.write(self.style.WARNING(
                    f"  {label}: never billed — excluded from consolidation"
                ))
                continue
            if not bp["branch_id"]:
                # Nothing to bill against: no branch means no Peak account
                # code and no identity for the receipt's remark.
                self.stderr.write(self.style.WARNING(
                    f"  {label}: skipped — these bills have no branch"
                ))
                continue
            if not bp["items"]:
                continue

            branch = Branch.objects.filter(id=bp["branch_id"]).first()
            if not branch:
                self.stderr.write(self.style.WARNING(
                    f"  {label}: skipped — branch {bp['branch_id']} is gone"
                ))
                continue

            try:
                cr, action = issue_consolidated_receipt(
                    day, branch, bp, contact_id, reissue=reissue,
                )
            except PeakVoidFailed as e:
                # The superseded receipt is still live, so the branch-day is
                # billed (staler than we'd like) rather than billed twice.
                self.stderr.write(self.style.ERROR(
                    f"  {label}: not reissued — {e}"
                ))
                continue
            except Exception as e:
                self.stderr.write(self.style.ERROR(f"  {label}: {e}"))
                continue

            self._report_issued(label, cr, action, bp)

    def _report_issued(self, label: str, cr, action: str, bp: dict) -> None:
        """Say plainly whether the branch-day ended up filed.

        A receipt Peak refuses comes back with no code and the row flagged,
        which used to print as "queued <uuid>" — indistinguishable from one
        still materialising, and so read as fine. The 2026-09-01 credit
        refusals scrolled past a nightly log looking exactly like a normal
        night. A day that is not filed now says so on stderr.
        """
        money = f"({bp['grand_total']:,.2f} THB, {bp['order_count']} bills)"
        if cr.peak_code:
            self.stdout.write(f"  {label}: {action} → {cr.peak_code} {money}")
        elif cr.needs_reissue:
            self.stderr.write(self.style.ERROR(
                f"  {label}: REFUSED by Peak — not filed {money}. "
                f"Flagged; the next --issue run retries it."
            ))
        else:
            self.stdout.write(
                f"  {label}: {action} → queued {cr.peak_queue_id} "
                f"(not yet materialised) {money}"
            )

    # ── reissue sweep ────────────────────────────────────────────────────
    def _reissue_sweep(self, contact_id: str, branches: list[Branch] | None) -> None:
        """Void and replace every branch-day flagged stale since the last run.

        A bill that gained its own Peak tax invoice, or was voided, after its
        branch-day had already been billed leaves that consolidated receipt
        overstating the day — the sale is filed twice, or filed at all when it
        should not be. The write paths flag the row (see
        :func:`flag_branch_day_for_reissue`); this is where the correction
        happens, and it is the whole reason ``needs_reissue`` exists.

        Runs on any date, unlike ordinary issuing. The yesterday-only rule
        exists to stop a stray ``--days`` filing a pile of *new* documents
        nobody asked for; every row here is a replacement for a document
        already known to be wrong, and the flag is the request.

        Each day is rebuilt from the orders as they stand now, so the
        replacement reflects every change since — not just the one that
        happened to set the flag. A day that fails stays flagged and is
        retried on the next run.
        """
        stale = (
            ConsolidatedReceipt.objects
            .filter(needs_reissue=True)
            .select_related("branch")
            .order_by("date", "branch__name")
        )
        if branches is not None:
            stale = stale.filter(branch__in=branches)
        stale = list(stale)
        if not stale:
            return

        self.stdout.write(self.style.MIGRATE_HEADING(
            f"reissuing {len(stale)} stale branch-day(s)"
        ))

        for cr in stale:
            branch = cr.branch
            label = f"{cr.date} {branch.name}"

            if branch.name in EXCLUDED_BRANCH_NAMES:
                # Should not be reachable — these are never billed, so they
                # have no receipt to go stale. If one exists it was filed by
                # hand, and correcting it is a decision for a person.
                self.stderr.write(self.style.WARNING(
                    f"  {label}: flagged but never billed — left alone"
                ))
                continue

            # Rebuild that one branch-day from scratch. Naming the branch
            # bypasses the excluded-names filter inside _consolidate, which is
            # why the check above is done here instead.
            day_payload = self._consolidate(cr.date, [branch])
            bp = next(
                (b for b in day_payload["branches"]
                 if b["branch_id"] == str(branch.id)),
                None,
            )

            if bp is None or not bp["items"]:
                # Nothing left to bill: every bill on the day was cancelled or
                # is now invoiced at Peak in its own right. Peak rejects a
                # receipt with no products, so the correction is to take the
                # old document down and leave the day filed by nothing.
                try:
                    retire_consolidated_receipt(cr)
                except PeakVoidFailed as e:
                    self.stderr.write(self.style.ERROR(
                        f"  {label}: not retired — {e}"
                    ))
                    continue
                except Exception as e:
                    self.stderr.write(self.style.ERROR(f"  {label}: {e}"))
                    continue
                self.stdout.write(
                    f"  {label}: retired — nothing left to bill"
                )
                continue

            try:
                cr, action = issue_consolidated_receipt(
                    cr.date, branch, bp, contact_id, reissue=True,
                )
            except PeakVoidFailed as e:
                # The superseded receipt is still live, so the branch-day stays
                # billed by stale figures rather than by two documents at once.
                # The row keeps its flag and the next run tries again.
                self.stderr.write(self.style.ERROR(
                    f"  {label}: not reissued — {e}"
                ))
                continue
            except Exception as e:
                self.stderr.write(self.style.ERROR(f"  {label}: {e}"))
                continue

            self._report_issued(label, cr, action, bp)

    # ── human output ─────────────────────────────────────────────────────
    def _print_summary(self, payload: dict) -> None:
        cust = payload["customer"]
        self.stdout.write(
            f"customer: {cust['name']} "
            f"({cust['peak_contact_id'] or 'no Peak contact yet'})"
        )
        for day in payload["days"]:
            self.stdout.write(self.style.MIGRATE_HEADING(day["date"]))
            if not day["branches"]:
                self.stdout.write("  no orders")
                continue

            for b in day["branches"]:
                self.stdout.write(
                    f"  {b['branch_name']} ({b['order_count']} bills "
                    f"{b['first_order_number']}–{b['last_order_number']}) "
                    f"→ {b['grand_total']:,.2f} THB"
                )
                self.stdout.write(
                    f"    goods {b['goods_total']:,.2f} "
                    f"(gross {b['items_gross']:,.2f} − discount {b['discount_total']:,.2f}) "
                    f"| VAT in price {b['vat_amount']:,.2f} "
                    f"| fee {b['processing_fee']:,.2f} + VAT {b['processing_fee_vat']:,.2f}"
                )
                if b["payments"]:
                    methods = "  ".join(
                        f"{p['method']} {p['amount']:,.2f}×{p['count']}"
                        for p in b["payments"]
                    )
                    self.stdout.write(f"    payments: {methods}")
                self.stdout.write(f"    {len(b['items'])} product lines")

                ex = b["excluded"]
                if ex["cancelled_count"] or ex["own_peak_receipt_count"]:
                    self.stdout.write(
                        f"    excluded: {ex['cancelled_count']} cancelled "
                        f"({ex['cancelled_total']:,.2f}), "
                        f"{ex['own_peak_receipt_count']} already invoiced at Peak "
                        f"({ex['own_peak_receipt_total']:,.2f})"
                    )
                if b["variance"]:
                    self.stdout.write(self.style.WARNING(
                        f"    variance {b['variance']:,.2f} — order totals "
                        f"disagree with their line items"
                    ))
