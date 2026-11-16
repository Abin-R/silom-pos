"""Report production history records that need a human investigation.

This command is intentionally read-only.  A catalogue change can make an old
snapshot disagree with its current product without corrupting historical
production records, so every finding is a warning rather than an automatic
repair candidate.
"""
from collections import defaultdict
from datetime import date
from uuid import UUID

from django.core.management.base import BaseCommand, CommandError

from bravepos.models import ProductionRequestItem


def _current_product_key(barcode, name):
    barcode = (barcode or "").strip()
    return f"b:{barcode}" if barcode else f"n:{(name or '').strip().lower()}"


class Command(BaseCommand):
    help = "Report ambiguous production request history without changing it."

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=10000,
                            help="Maximum items to inspect (1 to 50,000; default 10,000).")
        parser.add_argument("--date", dest="delivery_date", type=date.fromisoformat,
                            help="Limit inspection to one delivery date (YYYY-MM-DD).")
        parser.add_argument("--branch", type=UUID, help="Limit inspection to one branch UUID.")

    def handle(self, *args, **options):
        limit = options["limit"]
        if not 1 <= limit <= 50000:
            raise CommandError("--limit must be from 1 to 50,000.")
        queryset = ProductionRequestItem.objects.all()
        if options["delivery_date"]:
            queryset = queryset.filter(request__delivery_date=options["delivery_date"])
        if options["branch"]:
            queryset = queryset.filter(request__branch_id=options["branch"])
        # Deliberately request only columns that existed before the additive
        # production-safety migration.  That makes this useful in the rollout
        # preflight window as well as afterwards; no policy/revision fields or
        # production service helpers are resolved here.
        items = list(queryset.order_by("id").values(
            "id", "request_id", "product_id", "match_key", "requested_qty", "packed_qty",
            "request__branch_id", "product__branch_id", "product__barcode", "product__name",
        )[:limit + 1])
        truncated = len(items) > limit
        items = items[:limit]
        self.stdout.write(f"Inspected {len(items)} production item(s); limit={limit}.")
        if truncated:
            self.stdout.write(self.style.WARNING(
                "INCOMPLETE: more items match this scope. Narrow --date and --branch "
                "or increase --limit before treating this inspection as complete."
            ))
        findings = 0

        by_request_product = defaultdict(list)
        by_request_key = defaultdict(list)
        current_keys = {}
        for item in items:
            if item["product_id"]:
                by_request_product[(item["request_id"], item["product_id"])].append(item)
            by_request_key[(item["request_id"], item["match_key"])].append(item)

            if item["product_id"] and item["product__branch_id"] != item["request__branch_id"]:
                findings += 1
                self.stdout.write(
                    "INSPECT cross-branch product link: request={request} item={item} "
                    "request_branch={request_branch} product={product} product_branch={product_branch}".format(
                        request=item["request_id"],
                        item=item["id"],
                        request_branch=item["request__branch_id"],
                        product=item["product_id"],
                        product_branch=item["product__branch_id"],
                    )
                )
            if item["product_id"]:
                current_key = _current_product_key(item["product__barcode"], item["product__name"])
                current_keys[item["id"]] = current_key
                if current_key != item["match_key"]:
                    findings += 1
                    self.stdout.write(
                        "INSPECT snapshot/current key mismatch: request={request} item={item} "
                        "saved_key={saved!r} current_key={current!r} product={product}".format(
                            request=item["request_id"],
                            item=item["id"],
                            saved=item["match_key"],
                            current=current_key,
                            product=item["product_id"],
                        )
                    )

        for (request_id, product_id), duplicates in by_request_product.items():
            if len(duplicates) > 1:
                findings += 1
                self.stdout.write(
                    "INSPECT duplicate product link: request={request} product={product} items={items}".format(
                        request=request_id,
                        product=product_id,
                        items=",".join(str(item["id"]) for item in duplicates),
                    )
                )
        for (request_id, match_key), collisions in by_request_key.items():
            # The database constraint normally prevents this.  Report it if a
            # legacy import or temporarily-disabled constraint left evidence.
            if len(collisions) > 1 and any(
                item["requested_qty"] == 0 and item["packed_qty"] is None for item in collisions
            ):
                findings += 1
                self.stdout.write(
                    "INSPECT retained-item key collision: request={request} key={key!r} items={items}".format(
                        request=request_id,
                        key=match_key,
                        items=",".join(str(item["id"]) for item in collisions),
                    )
                )

        # A current catalogue key can also collide with another saved row's
        # immutable snapshot.  This is the ambiguity ordinary writers refuse:
        # a product edit/reuse must never make a retained historical identity
        # look interchangeable with a different product.
        reported_current_collisions = set()
        for item in items:
            current_key = current_keys.get(item["id"])
            if current_key is None:
                continue
            for saved in by_request_key[(item["request_id"], current_key)]:
                if saved["id"] == item["id"]:
                    continue
                pair = tuple(sorted((str(item["id"]), str(saved["id"]))))
                if pair in reported_current_collisions:
                    continue
                if not any(row["requested_qty"] == 0 and row["packed_qty"] is None
                           for row in (item, saved)):
                    continue
                reported_current_collisions.add(pair)
                findings += 1
                self.stdout.write(
                    "INSPECT current-key collision involving retained item: request={request} "
                    "current_item={current} retained_item={retained} key={key!r}".format(
                        request=item["request_id"], current=item["id"], retained=saved["id"],
                        key=current_key,
                    )
                )

        if findings:
            self.stdout.write(self.style.WARNING(
                f"{findings} production integrity finding(s); inspect before attempting a repair."
            ))
        else:
            self.stdout.write("No production integrity findings in the inspected items.")
