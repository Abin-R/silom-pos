# BakePOS — Bakery POS App (v1)

## Overview
Tablet-first Point-of-Sale app cloned from a Thai bakery POS (FoodStory/Ocha style), built with Expo Router + FastAPI + MongoDB. Landscape-optimized, green accent (#00B14F), mocked payments.

## Features Delivered (v1)
- **Staff PIN Login**: 4-digit keypad auth (demo PINs: `1234` Admin, `0000` Cashier)
- **Main POS Screen (landscape 3-pane)**:
  - Left category rail: Favorite, Choco Gems, Mousse Cake, Soft Cookies, Dubai Chocolate, Cookie Cake
  - Center product grid (4 cols) with images + THB prices + live search
  - Right cart sidebar: Tables chip, customer chip, Subtotal, Discount row, THB total, Pay button, item list with qty controls, Clear cart
- **Top Toolbar**: Search, Order Hub (with badge), Drawer, Discount, Save & Retrieve, Customer, Admin staff chip, Logout
- **Discount Modal**: Amount/% toggle, quick 5/10/15/20% chips, numeric keypad, Done
- **Payment Modal**: 6 mocked methods (Easy Pay, Credit, PromptPay, QR Kbank, EDC, Custom), numeric keypad, quick amounts (1000/500/100/50/20), Net Total tap-to-fill, Change calculation, Confirm Payment
- **Payment Success Modal**: Order number, method, total, received, change due, "Done · New Order"
- **Order Hub (Kanban)**: Tabs All/Table/Delivery/KIOSK/Other; columns New Order / Preparing / Completed / Cancel; Grab delivery cards with DELIVERING/DELIVERED badges; tap card to cycle status
- **Customer Management**: Search, avatar list with phone + last visit, add new customer (inline form)
- **Save & Retrieve**: Park current order, retrieve later (restores cart)
- **Cash Drawer**: Slide-in panel with daily quick stats (Sales, Orders, Avg Ticket — UI stub for v2)

## Tech Stack
- **Frontend**: Expo SDK 54, Expo Router, React Native 0.81, @expo/vector-icons, landscape orientation
- **Backend**: FastAPI, MongoDB (motor), Pydantic v2
- **Seed Data**: 6 categories, 25 bakery products (Pink Birthday Cookie Cake, Chocogems Pop, Dubai Matcha Strawberry Mochi, etc.), 7 customers, 5 demo Grab delivery orders

## Key API Endpoints (all prefixed `/api`)
- `POST /auth/verify-pin`
- `GET /categories`
- `GET /products?category_id=&favorite=`
- `GET /customers?q=` · `POST /customers`
- `POST /orders` · `GET /orders?source=&status=` · `PUT /orders/{id}/status`
- `GET /parked-orders` · `POST /parked-orders` · `DELETE /parked-orders/{id}`
- `POST /seed` (idempotent)

## Mocked Integrations
- **ALL 6 PAYMENT METHODS ARE MOCKED** — Confirm Payment creates an Order record with the selected method and shows success modal. No real payment processor integration.

## Out of Scope (deferred to v2+)
- Real payment gateway integration (Stripe/Omise/PromptPay)
- Receipt printing
- Full sales reports / analytics
- Staff management (multiple users, roles)
- Inventory tracking
- Thai language UI toggle
- Table management (seating plan)

## Smart Business Enhancement
Ground-up analytics-ready schema: every order stores source, payment method, discount type/amount, customer link, and timestamps — enabling future dashboards, top-customer lists, and payment-method performance reports without a migration.
