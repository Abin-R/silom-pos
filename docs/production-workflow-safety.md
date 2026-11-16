# Production workflow safety

## What stays the same

- Supervisors enter requested quantities in category lists for any active branch.
- Kitchen staff read combined product totals.
- Packers enter quantities in category grids with branches down the side.
- An empty packed quantity means unconfirmed. Zero means confirmed none sent.
- Production saves do not create stock documents or change product stock.
- POS cashiers enter stock-in from their independent count. POS tokens do not authenticate backoffice production pages.
- Existing backoffice Manager and administrator access stays unchanged.

## Safe saves

Every form submits its displayed delivery date and branch, where applicable.
The server never substitutes another tab's branch or a new default date during Save.
A supervisor request closes at its delivery date's deadline, including while a save waits for a database lock.
Packing can continue after that deadline because the deadline freezes requested quantities, not packing.

Supervisor forms carry a request revision. Packing forms carry a revision for each changed cell.
A stale save rejects the complete submitted batch. Edits to unrelated packing cells do not cause a conflict.
The error page retains entered quantities and identifies current saved quantities separately.
Reload discards the draft only after explicit confirmation. It does not save or merge the draft.
Closed-date drafts remain read-only on their original date.

Normal entry, keyboard navigation, search, and Fill from requested keep their existing organization.
Large forms submit one compact JSON payload rather than thousands of individual fields.
Supported bounds are 10,000 rows/cells, 2 MiB of UTF-8 production JSON, and 8 MiB for the encoded body.
The first limit reached applies. No save silently truncates the payload.
Small forms support submission without JavaScript. Forms above 900 quantity inputs require the compact handler.

## Product identity and history

Existing lines use request-item identity and saved product snapshots.
A catalogue rename or reused barcode does not transfer a saved line or its packed history to another product.
Ambiguous catalogue records require inspection. The application must not guess a repair.
Empty request parents and zero-quantity lines can remain as internal revision records.
These records do not appear as new requests or kitchen work unless they contain a requested or confirmed packed quantity.

Packed-history export uses the same mode, date, category, and branch inclusion as the history screen.
It exports packed quantities, including confirmed zero, without requested-only rows or columns.

## Cutoff policy adoption

Cutoff changes append policy history instead of recalculating already-closed dates.
Only delivery dates still open can receive a changed cutoff.
The same protection covers dates with no request and dates nobody opened.
The POS settings endpoint cannot modify the production cutoff.

The adoption migration records the current cutoff as a legacy baseline.
It does not recover earlier cutoff changes, overwritten product labels, or previously corrupted product links.
The permanent-deadline guarantee starts at adoption. Legacy labels must not imply a verified original closing time.

## Coordinated rollout

1. Take a normal database backup under the established deployment procedure.
2. Pause production-request, packing, and cutoff writes before changing writers.
3. Run the read-only production integrity diagnostic before enabling new writers.
4. Apply the additive migration and deploy the matching application version together.
5. Run the diagnostic again and inspect flagged records without modifying them automatically.
6. Confirm the new application rejects an old unversioned form without changing quantities.
7. Check supervisor saves, kitchen totals, packing, and history exports with authorized accounts.
8. Resume production writes only after these checks succeed.

The migration does not rewrite requested quantities, packed quantities, packed metadata, or saved product snapshots.
POS trading and independent stock-in do not need production integration or a new receiving role.
Do not run old and new production writers together.
After policy/revision writes begin, do not restore the old writers or remove policy history as a rollback.
Keep production writes paused while correcting the new application if deployment checks fail.
Repair existing suspect records only through a separately reviewed change supported by verified evidence.

`check_production_integrity` inspects at most 10,000 items by default.
Use `--date YYYY-MM-DD` and `--branch UUID` to narrow the database query.
The command prints `INCOMPLETE` if more items match than its limit.
Do not interpret a limited inspection as a complete check of all history.
Increase `--limit` only within the supported maximum of 50,000 items, with low concurrency.
The replacement-product check also bounds snapshot keys and current catalogue matches.
Resolve any `CATALOGUE INCOMPLETE` warning through narrower branch/date inspections.

## Verification

Never use the default Django settings for local tests. The default database is shared production.

SQLite regression example:

```bash
python manage.py test bravepos.tests.test_production_requests \
  bravepos.tests.test_production_pos_boundary \
  --settings=bravepos_api.settings_test
```

Run `bravepos.tests.test_production_concurrency` with an explicit isolated PostgreSQL test settings module.
SQLite skips these tests because it does not implement PostgreSQL row locks.
The tests use separate connections, bounded waits, and controlled clocks to verify conflicts and cutoff behavior.

Live verification uses the real Django application, not the approval mockups.
Check desktop and narrow widths in the existing light theme, including conflict and cutoff drafts,
reload confirmation, keyboard entry, blank/zero behavior, exports, and compact submissions.
Report unavailable browser checks as not verified.
