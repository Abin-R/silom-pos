"""Mine "customers who ordered this also ordered…" from real basket history.

Feeds the cashier's upsell strip.  Everything expensive happens here, on a
timer, so the serving endpoint is a two-query lookup instead of a computation
sitting in front of a sale.

Three tiers are written, and the endpoint walks them in this order:

  * ``rule``    — association rules from the Apriori algorithm, via the
                  ``efficient-apriori`` package.  Its one contribution over the
                  tier below is *multi-item* antecedents ({Latte, Croissant} →
                  Cookie).  It needs volume, so it is gated behind
                  ``MIN_BASKETS_FOR_MINING`` and at a quiet branch produces
                  nothing at all.  That is expected, not a fault.
  * ``pair``    — pairwise co-occurrence lift, computed by plain counting.
                  Produces useful output at volumes Apriori cannot clear, so in
                  practice this is the tier that carries the feature.
  * ``popular`` — branch top sellers.  Works from the first order ever rung up,
                  which is what a branch that opened this morning gets.

``efficient_apriori`` is imported lazily, inside the tier that uses it, and
nowhere else in the codebase.  ``deploy.sh`` imports the entire URLconf as its
go/no-go gate, so a module-scope import of a package missing from the box would
abort a deploy; confined here, a missing package degrades this command to the
counting tiers and can never reach the till.  It is not in the web tier's
import graph at all.

Run it on a timer (the crontab lives on the server, like the two commands
beside this one):

    30 4 * * 1  cd /home/azureuser/silom-pos/backend_django && $VENV_PY manage.py mine_suggestions --json
    0  5 * * *  cd /home/azureuser/silom-pos/backend_django && $VENV_PY manage.py mine_suggestions --popular-only --json

Weekly, not nightly: with a 180-day window one day of orders moves support by
well under a percent, so a nightly Apriori run would burn CPU on a shared box
to produce a near-identical table.  The cheap daily ``--popular-only`` pass is
one pass per branch and gives a brand-new branch day-two relevance rather than
making it wait until Monday.  04:30 sits after ``consolidate_daily``'s 04:15 so
the two never contend.

Safe to re-run, safe to run while the tills are trading: each branch is
rewritten inside its own transaction, so a concurrent ``/api/suggestions``
sees either the whole old rule set or the whole new one, never a half-built
table, and a failure mining one branch cannot wipe another.
"""
from __future__ import annotations

import itertools
import json
from collections import Counter, defaultdict
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone as djtz

from bravepos import audit
from bravepos.models import Branch, OrderItem, SuggestionRule

# ── Thresholds ──────────────────────────────────────────────────────────────
# Tuned for a coffee shop, not for a textbook.  Tutorial values (min_support
# 0.5) come from dense synthetic data and would return literally nothing here.

# Apriori is only asked to run once there is enough history for its answer to
# mean anything.  Counted in *multi-item* baskets, because a single-line sale
# is not part of the population Apriori is handed — counting those would let a
# shop of solo coffees clear the gate with no co-occurrence data at all.
# Below this the pair tier is strictly better: it produces output, and its lift
# comes from the same counts anyway.
MIN_BASKETS_FOR_MINING = 200

# Support is driven by an absolute basket count, not a fixed fraction.  A flat
# 0.02 means 20 co-occurrences over 1000 baskets (fine) and 4 over 200 (noise
# wearing a percentage).  The 0.005 floor exists only to bound Apriori's
# candidate generation over a couple of hundred SKUs.
MIN_SUPPORT_BASKETS = 12
MIN_SUPPORT_FLOOR = 0.005

# Deliberately loose.  A coffee-shop consequent almost never reaches 0.5, and
# we are not looking for a confident rule — we are looking for a better-than-
# chance one, which is lift's job.  Confidence only breaks ties.
MIN_CONFIDENCE = 0.15

# The filter that actually matters, applied by us rather than the library.
# Lift <= 1 means the pair is no more likely than chance, i.e. worse than just
# showing the top seller — and the popularity tier already does that, better.
MIN_LIFT_RULE = 1.2
MIN_LIFT_PAIR = 1.05

# A pair seen three times is an anecdote.
PAIR_MIN_COUNT = 4

# Antecedents of size 1 and 2 only.  Size 3 would need a subset explosion at
# serve time for antecedents that will never clear the support floor here.
MAX_ITEMSET_LENGTH = 3

TOP_POPULAR = 12
MAX_PER_ANTECEDENT = 5
MAX_ROWS_PER_BRANCH = 5000

DEFAULT_DAYS = 180


class Command(BaseCommand):
    help = "Rebuild the upsell suggestion rules from recent order history."

    def add_arguments(self, parser):
        parser.add_argument(
            '--days', type=int, default=DEFAULT_DAYS,
            help=f'How much history to mine (default {DEFAULT_DAYS}).',
        )
        parser.add_argument(
            '--branch', default='',
            help='Limit to one branch, by id or name.',
        )
        parser.add_argument(
            '--popular-only', action='store_true',
            help='Refresh only the popularity tier — the cheap daily pass.',
        )
        parser.add_argument(
            '--dry-run', action='store_true',
            help='Report what would be written without touching the database.',
        )
        parser.add_argument(
            '--json', action='store_true',
            help='Emit one JSON object per branch, for cron logs.',
        )
        parser.add_argument(
            '--report', action='store_true',
            help='Also report how many order lines came from the strip.',
        )

    # ── Entry point ─────────────────────────────────────────────────────
    def handle(self, *args, **opts):
        since = djtz.now() - timedelta(days=opts['days'])
        branches = Branch.objects.filter(active=True)
        if opts['branch']:
            sel = opts['branch']
            branches = branches.filter(id=sel) if _looks_like_uuid(sel) else \
                branches.filter(name__iexact=sel)

        for branch in branches.order_by('name'):
            try:
                stats = self._mine_branch(branch, since, opts)
            except Exception as e:            # noqa: BLE001 — one bad branch
                # must not abort the sweep; the next run retries it.
                self.stderr.write(f'{branch.name}: FAILED — {e}')
                continue
            self._report(branch, stats, opts)

        if opts['report']:
            self._attribution_report(since, opts)

    # ── One branch ──────────────────────────────────────────────────────
    def _mine_branch(self, branch, since, opts):
        baskets = _load_baskets(branch, since)
        n = len(baskets)
        multi = [b for b in baskets if len(b) >= 2]

        singles = Counter()
        for basket in baskets:
            singles.update(basket)

        rows = list(_popular_rows(branch, singles, n))
        stats = {
            'baskets': n,
            'multi_item': len(multi),
            'median_size': _median([len(b) for b in baskets]),
            'rules': 0,
            'pairs': 0,
            'popular': len(rows),
            'apriori': 'skipped',
        }

        if not opts['popular_only']:
            pair_rows = list(_pair_rows(branch, multi, singles, n))
            rows.extend(pair_rows)
            stats['pairs'] = len(pair_rows)

            if len(multi) >= MIN_BASKETS_FOR_MINING:
                rule_rows, note = _rule_rows(branch, multi)
                rows.extend(rule_rows)
                stats['rules'] = len(rule_rows)
                stats['apriori'] = note
            else:
                stats['apriori'] = f'gated (multi<{MIN_BASKETS_FOR_MINING})'

        rows = rows[:MAX_ROWS_PER_BRANCH]
        stats['written'] = len(rows)

        if opts['dry_run']:
            stats['written'] = 0
            stats['would_write'] = len(rows)
            return stats

        with transaction.atomic():
            # Bulk machine writes have no actor and would bury the entries a
            # human needs.  SuggestionRule is in audit.NEVER already; pausing
            # is belt-and-braces against a future registration.
            with audit.pause():
                qs = SuggestionRule.objects.filter(branch=branch)
                if opts['popular_only']:
                    qs = qs.filter(kind='popular')
                qs.delete()
                SuggestionRule.objects.bulk_create(rows, batch_size=500)
        return stats

    # ── Output ──────────────────────────────────────────────────────────
    def _report(self, branch, stats, opts):
        if opts['json']:
            self.stdout.write(json.dumps({'branch': branch.name, **stats}))
            return
        line = (
            f"{branch.name:<16} baskets={stats['baskets']:<6} "
            f"multi={stats['multi_item']:<5} median={stats['median_size']:<4} "
            f"rules={stats['rules']:<4} pairs={stats['pairs']:<4} "
            f"popular={stats['popular']:<3} apriori={stats['apriori']}"
        )
        if stats['baskets'] == 0:
            line += '  — nothing to mine; the strip will show favourites'
        self.stdout.write(line)

    def _attribution_report(self, since, opts):
        """Is the strip earning its screen space?

        Deliberately a log line rather than a screen: the command runs weekly
        anyway, and a report page for one number is not worth the surface.
        """
        from django.db.models import Count, F, Sum

        def by_branch(**extra):
            return (
                OrderItem.objects
                .filter(order__created_at__gte=since, **extra)
                .exclude(order__status='cancel')
                .values('order__branch__name')
                .annotate(
                    lines=Count('id'),
                    sales=Sum(F('price') * F('qty')),
                )
            )

        totals = {r['order__branch__name']: r for r in by_branch()}
        suggested = by_branch(suggested=True)
        for row in suggested:
            name = row['order__branch__name']
            total = totals.get(name) or {'lines': 0, 'sales': 0}
            pct = (row['lines'] / total['lines'] * 100) if total['lines'] else 0
            payload = {
                'branch': name, 'suggested_lines': row['lines'],
                'suggested_sales': float(row['sales'] or 0),
                'share_of_lines_pct': round(pct, 1),
            }
            self.stdout.write(
                json.dumps(payload) if opts['json'] else
                f"{name:<16} upsell lines={row['lines']} "
                f"sales={row['sales']} ({pct:.1f}% of lines)"
            )


# ── Basket loading ──────────────────────────────────────────────────────────
def _load_baskets(branch, since):
    """Every multi-line sale as a set of product ids.

    One query, one join, bounded memory.  ``.exclude(status='cancel')`` matches
    every other report in this project — a voided bill is not evidence of
    anything a guest wanted.
    """
    rows = (
        OrderItem.objects
        .filter(
            order__branch=branch,
            order__created_at__gte=since,
            product_id__isnull=False,
        )
        .exclude(order__status='cancel')
        .values_list('order_id', 'product_id')
        .order_by('order_id')
        .iterator(chunk_size=5000)
    )
    baskets = []
    for _order_id, group in itertools.groupby(rows, key=lambda r: r[0]):
        # A set, so a product that somehow landed on two lines of one bill is
        # one observation, not two.
        baskets.append({pid for _oid, pid in group})
    return baskets


# ── Tier 1: popularity ──────────────────────────────────────────────────────
def _popular_rows(branch, singles, n):
    for pid, count in singles.most_common(TOP_POPULAR):
        yield SuggestionRule(
            branch=branch, kind='popular',
            antecedent_key='', antecedent_size=0,
            consequent_id=pid,
            support=count / n if n else 0,
            confidence=0, lift=0,
            basket_count=count,
            score=count / n if n else 0,
        )


# ── Tier 2: pairwise lift ───────────────────────────────────────────────────
def _pair_rows(branch, multi, singles, n):
    """Plain counting, both directions.

    This is what Apriori would produce for 2-item rules, minus the thresholds
    that stop it producing anything at this volume.  Cheap enough to compute
    over every basket every time.
    """
    if not n:
        return
    pairs = Counter()
    for basket in multi:
        for combo in itertools.combinations(sorted(basket, key=str), 2):
            pairs[combo] += 1

    per_antecedent = defaultdict(int)
    for (a, b), count in pairs.most_common():
        if count < PAIR_MIN_COUNT:
            break                     # most_common is descending — nothing left
        expected = (singles[a] / n) * (singles[b] / n)
        if expected <= 0:
            continue
        lift = (count / n) / expected
        if lift < MIN_LIFT_PAIR:
            continue
        for lhs, rhs in ((a, b), (b, a)):
            if per_antecedent[lhs] >= MAX_PER_ANTECEDENT:
                continue
            per_antecedent[lhs] += 1
            yield SuggestionRule(
                branch=branch, kind='pair',
                antecedent_key=str(lhs), antecedent_size=1,
                consequent_id=rhs,
                support=count / n,
                confidence=count / singles[lhs] if singles[lhs] else 0,
                lift=lift,
                basket_count=count,
                # Score is the capped lift: a pair seen 4 times out of 400
                # baskets can produce a lift in the hundreds, which would then
                # outrank every honest rule in the table.  The raw lift above
                # is left uncapped so the admin list shows what was measured.
                score=min(lift, 10.0),
            )


# ── Tier 3: Apriori ─────────────────────────────────────────────────────────
def _rule_rows(branch, multi):
    """Association rules with multi-item antecedents.

    Every figure here — support, the threshold, the reported basket count — is
    relative to the multi-item baskets, because those are the only transactions
    Apriori is given.  Mixing in the single-line sales would report a support
    the library never computed.

    The import is local on purpose — see the module docstring.  A box without
    the package logs a note and keeps the two counting tiers, which is a
    degraded feature rather than a broken cron.
    """
    try:
        from efficient_apriori import apriori
    except ImportError:
        return [], 'efficient-apriori not installed'

    transactions = [tuple(str(pid) for pid in basket) for basket in multi]
    if not transactions:
        return [], 'no multi-item baskets'

    n = len(transactions)
    min_support = max(MIN_SUPPORT_BASKETS / n, MIN_SUPPORT_FLOOR)
    _itemsets, rules = apriori(
        transactions,
        min_support=min_support,
        min_confidence=MIN_CONFIDENCE,
        max_length=MAX_ITEMSET_LENGTH,
    )

    rows, per_antecedent = [], defaultdict(int)
    for rule in sorted(rules, key=lambda r: -r.lift):
        # One consequent per chip — the strip suggests products, not baskets.
        if len(rule.rhs) != 1 or rule.lift < MIN_LIFT_RULE:
            continue
        size = len(rule.lhs)
        if size > 2:
            continue
        key = '|'.join(sorted(str(x) for x in rule.lhs))
        if per_antecedent[key] >= MAX_PER_ANTECEDENT:
            continue
        per_antecedent[key] += 1
        rows.append(SuggestionRule(
            branch=branch, kind='rule',
            antecedent_key=key, antecedent_size=size,
            consequent_id=rule.rhs[0],
            support=rule.support,
            confidence=rule.confidence,
            lift=rule.lift,
            basket_count=int(round(rule.support * n)),
            # Multi-item antecedents are the whole reason this tier exists, so
            # a {A,B} → C rule outranks an equally-lifted {A} → C.
            score=min(rule.lift, 10.0) + size,
        ))
    return rows, f'ran (min_support={min_support:.4f})'


# ── Small helpers ───────────────────────────────────────────────────────────
def _median(values):
    if not values:
        return 0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _looks_like_uuid(value):
    import uuid as _uuid
    try:
        _uuid.UUID(str(value))
        return True
    except (TypeError, ValueError):
        return False
