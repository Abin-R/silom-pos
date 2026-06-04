# SilomPOS Dashboard — Page Catalog

Reference for replicating the third-party `dashboard.silompos.com` UI as an
in-house backoffice backed by `backend_django/`. Built from screenshots
captured 2026-06-03.

## Cross-cutting

- **Multi-branch**: every reporting page has a branch dropdown. The
  rolling pinn account has 22 branches (Central Chidlom, Central Park,
  Central World, Chonburi, Event 01, Event002-1/2/3, Event06–10, Future
  Park Rangsit, Icon Siam, Ladprao, Mega bangna, Samyan, Terminal Pattaya,
  The mall korat, พารากอน, เอ็มควอเทีย). Backend ACL — some branches show
  a "Can't access this information" empty state.
- **Date range picker**: single day or range, used by every report.
- **Export button** on every report page (XLSX/CSV).
- **Header**: language toggle (TH/EN), profile menu.
- **Sidebar nav groups**: Reports (Sales · Inventory · Tax · Staff · Order
  Device) and Management (Product · Inventory · Inventory Transfer · Shop
  and Branch · Promotion · Self Service).
- **Sales sub-menu**: Sales by Date, Sales by Bill Detail, Sales by
  Product, Sales by Product (PLU), Non Selling Products, Sales by Category,
  Payment, Drawer, Void bill, Sales by Add-on.
- **Inventory sub-menu**: Inventory Summary, PLU Inventory Report, Serial
  No. Inventory Report, Low Stock, Out of Stock, Stock In by Product,
  Stock Out by Product, Unadjusted Stock, Export Products, Add-on Category
  Sales By Date.

---

## `/dashboard`

Single-page overview, branch + date filtered.

**Top tiles**: Sales · Profit · Discount (THB totals).

**Payment donut**: Cash / Credit card / Custom Pay.

**Bill total card**: Total bills · Average per bill · Cancel · Voidbill
Total.

**Tax breakdown column**: Sub Total · Discount Amount · Total (incl-Tax) ·
Total (non-Tax) · Tax Amount 7% · Service Charge · Grand Total.

**Inventory tiles**: Inventory Quantity (items) · Total Cost Value (THB) ·
Inventory Value (THB).

**Delivery Channels card**: per-channel revenue + order count, includes
Grab integration banner.

**Table Usage total**: Items avg/bill · Customer avg/table · Bills per
table per day · Time per table.

**Sales by date** histogram (24-hour x-axis).

**Best sellers** donut + top-7 product table (Qty, Sales).

**Best sellers category** donut + top-N category table.

---

## `/report/transaction`

Per-bill transaction list with expandable detail.

**Filters**: branch, date range.

**List columns**: Date · Receipt No. · Sub Total · Discount · Promotion
Discount · Grand Total · Total (incl-Tax) · Total (non-Tax) · Sub-total
(ex-Tax) · Tax Amount · Add-on Total · Service Charge · Rounding Adj. ·
Shipping Fee · Status.

**Row expansion** shows:
- Items table: Receipt No., Barcode, Product Name, Quantity, Price/Unit,
  Item discount, Sub Total, Total
- Discount / Promotion Discount / Service Charge totals
- Payment block (Cash / Credit card / Custom Pay) + Change
- "Print Simplified Tax Invoice" action

---

## `/report/daily`

Sales report by Date — one row per day in the range.

**Columns**: Date · Sub Total · Discount · Tax Amount · Service Charge ·
Profit · Grand Total. Negative discounts shown in red.

**Drill-down**: clicking a day → `/report/daily/{token}/{date}` shows
per-bill rows for that day (Created at, Pay at, Receipt No., Sub Total,
Discount, Tax Amount, Service Charge, Rounding Adj., Grand Total).

---

## `/report/sell` — Sales report by Bill Detail

One row per line item across all bills in the range. ~2500 rows for the
sample month.

**Columns**: Date · Receipt No. · Barcode · Product Name · Quantity ·
Price/Unit · Add-on Total · Sub Total · Discount · Total.

---

## `/report/sku` — Sales report by Product

Aggregated per product over the date range.

**Columns**: # · Barcode · Product Name · Category · Quantity · Balance ·
Sales · Cost · Profit.

---

## `/inventory/sku` — Inventory Summary

Current on-hand by product.

**Sort By**: Name · Barcode · Category · OnhandQty.

**Columns**: # · Barcode · Product Name · Unit · Category · Balance
(numeric, or `non-stock` for add-ons / non-tracked items).

~180 rows.

---

## `/product` — Product catalog

**Header**: branch selector + Submit. Buttons: `Add Product`,
`More Manage Product` (dropdown: Quick Add Multiple Products, Quick Edit
Multiple Products).

**Filters**: All Product (type) · Category search.

**View toggle**: list ☰ / grid ▦.

**Sort By**: Name · Newest · Category · Product price.

**Grid card**: image, name, category, VAT badge, price, channel/tag
badges (BOM, P, Grab-eligible green, etc.). Clicking a card →
`/product/productdetail/{id}`.

~181 products.

---

## `/product/productdetail/{id}` — Product detail

Edit a single product. Two-column layout.

**Left column**: Back · branch context · product thumbnail · delete (🗑) ·
section tabs: **Information** (active) · Add-on Category · Other packaging
sizes · Sales Channels · Other Prices.

**Right column — Information tab fields**: Product image upload · Product
Name · Product Description (0/5000, More Translation link) · Product price
· Cost · Barcode · Category · Save button.

---

## `/product/newAddproduct` — Add Product

Single-product add form, same tab structure as detail but tabs are:
Information · Add-on Category · Other packaging sizes · **Price** · Other
Prices.

**Information fields** (required marked `*`): Image · Product Name* ·
Product price* · Cost · Barcode · Category* · Unit* · Product Description.

---

## `/product/addproductmultiple` — Quick Add Multiple Products

Bulk add up to 10 rows.

**Top**: `Product quantity` input + `+ Add items` button. Note: max 10.

**Row columns**: No. · Image · Barcode · Name* · Description · Category* ·
Unit* · Product price · Product Type* (default `General Prod`).

`Save` button top-right.

---

## `/product/editproductmultiple` — Quick Edit Multiple Products

Same grid layout, populated with existing products. ~180 items, paginated
10/page. Sort + Category filter at top. Inline edit of any cell.

---

## Backoffice mapping notes

Build order suggestion (lightest → heaviest):

1. **Read-only reports first** (`/dashboard`, `/report/*`,
   `/inventory/sku`) — they only need `GET` endpoints from
   `backend_django` with branch + date filters. Shared widgets: branch
   selector, date range picker, export-button hook.
2. **Product catalog read** (`/product` list + detail) — `GET` only.
3. **Product write** (`/product/newAddproduct`,
   `/product/addproductmultiple`, `/product/editproductmultiple`) — needs
   `POST`/`PATCH` endpoints + image upload.
4. **Per-page actions** like "Print Simplified Tax Invoice" — defer until
   reporting is solid.

Endpoint shape to verify in `backend_django/`:
- `GET /api/branches/` for the branch selector.
- `GET /api/reports/{dashboard,transactions,daily,sell,sku}?branch=&from=&to=`
- `GET /api/inventory/summary?branch=`
- `GET /api/products/?branch=&page=&sort=&category=`
- `GET /api/products/{id}/`
- `POST /api/products/` and `POST /api/products/bulk/`

If any of these are missing, add them in `backend_django/` rather than
spinning up a new service.
