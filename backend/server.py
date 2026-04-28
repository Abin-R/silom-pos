from fastapi import FastAPI, APIRouter, HTTPException
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List, Optional
import uuid
from datetime import datetime, timezone
import httpx
import base64

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

mongo_url = os.environ['MONGO_URL']
client = AsyncIOMotorClient(mongo_url)
db = client[os.environ['DB_NAME']]

app = FastAPI()
api_router = APIRouter(prefix="/api")


# ---------- Models ----------
class Category(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    name_th: Optional[str] = None
    color: str = "#00B14F"
    order: int = 0
    source: Optional[str] = None  # e.g. "Grabfood"
    active: bool = True


class CategoryCreate(BaseModel):
    name: str
    name_th: Optional[str] = None
    source: Optional[str] = None
    active: bool = True


class Product(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    name_th: Optional[str] = None
    price: float
    cost: float = 0
    category_id: str
    image_url: str
    is_favorite: bool = False
    stock: int = 0
    tax_type: str = "V"  # V=VAT, N=None
    product_type: str = "P"  # P=Product, S=Service
    barcode: Optional[str] = None


class ProductCreate(BaseModel):
    name: str
    name_th: Optional[str] = None
    price: float
    cost: float = 0
    category_id: str
    image_url: str = ""
    is_favorite: bool = False
    stock: int = 0
    tax_type: str = "V"
    product_type: str = "P"
    barcode: Optional[str] = None


class ProductUpdate(BaseModel):
    name: Optional[str] = None
    name_th: Optional[str] = None
    price: Optional[float] = None
    cost: Optional[float] = None
    category_id: Optional[str] = None
    image_url: Optional[str] = None
    is_favorite: Optional[bool] = None
    stock: Optional[int] = None
    tax_type: Optional[str] = None
    product_type: Optional[str] = None
    barcode: Optional[str] = None


class StockMovement(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    product_id: str
    product_name: str
    type: str  # in, out, adjust
    qty: int
    note: Optional[str] = None
    document_no: Optional[str] = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class StockMovementCreate(BaseModel):
    product_id: str
    type: str
    qty: int
    note: Optional[str] = None


class Settings(BaseModel):
    id: str = "shop"
    shop_name: str = "The rolling pinn"
    business_type: str = "Restaurant"  # General, Restaurant, Hostel
    tax_id: Optional[str] = None
    pos_id: str = "001"
    branch: str = "Event01"
    pos_number: str = "001"
    open_time: str = "09:00"
    close_time: str = "22:00"
    tax_percent: float = 7.0
    tax_mode: str = "exclusive"  # exclusive, inclusive
    service_charge_enabled: bool = False
    service_charge_percent: float = 10.0
    logo_url: Optional[str] = None
    beam_merchant_id: Optional[str] = None
    beam_api_key: Optional[str] = None
    beam_sandbox: bool = True  # True = playground, False = production


class SettingsUpdate(BaseModel):
    shop_name: Optional[str] = None
    business_type: Optional[str] = None
    tax_id: Optional[str] = None
    pos_id: Optional[str] = None
    branch: Optional[str] = None
    pos_number: Optional[str] = None
    open_time: Optional[str] = None
    close_time: Optional[str] = None
    tax_percent: Optional[float] = None
    tax_mode: Optional[str] = None
    service_charge_enabled: Optional[bool] = None
    service_charge_percent: Optional[float] = None
    logo_url: Optional[str] = None
    beam_merchant_id: Optional[str] = None
    beam_api_key: Optional[str] = None
    beam_sandbox: Optional[bool] = None


class Customer(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    phone: Optional[str] = None
    last_visit: Optional[str] = None
    color: str = "#94A3B8"


class CustomerCreate(BaseModel):
    name: str
    phone: Optional[str] = None


class OrderItem(BaseModel):
    product_id: str
    name: str
    price: float
    qty: int


class OrderCreate(BaseModel):
    items: List[OrderItem]
    subtotal: float
    discount_type: str = "none"
    discount_value: float = 0
    discount_amount: float = 0
    total: float
    payment_method: Optional[str] = None
    paid_amount: float = 0
    change: float = 0
    source: str = "table"
    customer_id: Optional[str] = None
    customer_name: Optional[str] = None
    beam_charge_id: Optional[str] = None  # Beam charge ID for reconciliation


class Order(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    order_number: str
    items: List[OrderItem]
    subtotal: float
    discount_type: str = "none"
    discount_value: float = 0
    discount_amount: float = 0
    total: float
    payment_method: Optional[str] = None
    paid_amount: float = 0
    change: float = 0
    status: str = "completed"
    source: str = "table"
    customer_id: Optional[str] = None
    customer_name: Optional[str] = None
    beam_charge_id: Optional[str] = None  # Beam charge ID for reconciliation
    delivery_provider: Optional[str] = None
    delivery_status: Optional[str] = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    created_time: str = Field(default_factory=lambda: datetime.now(timezone.utc).strftime("%H:%M"))


class ParkedOrder(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    label: str
    items: List[OrderItem]
    subtotal: float
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class ParkedOrderCreate(BaseModel):
    label: str
    items: List[OrderItem]
    subtotal: float


class PinVerify(BaseModel):
    pin: str


class OrderStatusUpdate(BaseModel):
    status: str


# ---------- Shift (Cash Drawer) ----------
class Shift(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    round_number: int
    start_cash: float = 0
    opened_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    opened_by: str = "Admin"
    closed_at: Optional[str] = None
    closed_by: Optional[str] = None
    total_sales_cash: float = 0
    total_paid_in: float = 0
    total_paid_out: float = 0
    expected_in_drawer: float = 0
    actual_in_drawer: Optional[float] = None
    status: str = "open"


class ShiftOpen(BaseModel):
    start_cash: float = 0
    opened_by: str = "Admin"


class ShiftMovement(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    shift_id: str
    type: str
    amount: float
    note: Optional[str] = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class ShiftMovementCreate(BaseModel):
    type: str
    amount: float
    note: Optional[str] = None


class ShiftClose(BaseModel):
    actual_in_drawer: float
    closed_by: str = "Admin"


# ---------- Helpers ----------
def strip_id(doc):
    if doc and "_id" in doc:
        doc.pop("_id", None)
    return doc


async def gen_order_number() -> str:
    """Generate unique order number like PS001XXXXXX."""
    for _ in range(10):
        num = uuid.uuid4().int % 1000000
        order_number = f"PS001{num:06d}"
        existing = await db.orders.find_one({"order_number": order_number})
        if not existing:
            return order_number
    return f"PS001{uuid.uuid4().hex[:6].upper()}"


# ---------- Routes ----------
@api_router.get("/")
async def root():
    return {"message": "POS API running"}


@api_router.post("/auth/verify-pin")
async def verify_pin(body: PinVerify):
    # Simple demo: default PIN 1234 or 0000
    valid_pins = {"1234": "Admin", "0000": "Cashier"}
    if body.pin in valid_pins:
        return {"success": True, "staff_name": valid_pins[body.pin]}
    raise HTTPException(status_code=401, detail="Invalid PIN")


@api_router.get("/categories", response_model=List[Category])
async def list_categories():
    docs = await db.categories.find({}, {"_id": 0}).sort("order", 1).to_list(100)
    return [Category(**d) for d in docs]


@api_router.post("/categories", response_model=Category)
async def create_category(body: CategoryCreate):
    count = await db.categories.count_documents({})
    c = Category(**body.model_dump(), order=count)
    await db.categories.insert_one(c.model_dump())
    return c


@api_router.put("/categories/{cid}", response_model=Category)
async def update_category(cid: str, body: CategoryCreate):
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    await db.categories.update_one({"id": cid}, {"$set": updates})
    doc = await db.categories.find_one({"id": cid}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Category not found")
    return Category(**doc)


@api_router.delete("/categories/{cid}")
async def delete_category(cid: str):
    await db.categories.delete_one({"id": cid})
    return {"success": True}


@api_router.get("/products", response_model=List[Product])
async def list_products(category_id: Optional[str] = None, favorite: Optional[bool] = None):
    q = {}
    if category_id:
        q["category_id"] = category_id
    if favorite is not None:
        q["is_favorite"] = favorite
    docs = await db.products.find(q, {"_id": 0}).to_list(500)
    return [Product(**d) for d in docs]


@api_router.post("/products", response_model=Product)
async def create_product(body: ProductCreate):
    p = Product(**body.model_dump())
    await db.products.insert_one(p.model_dump())
    return p


@api_router.put("/products/{pid}", response_model=Product)
async def update_product(pid: str, body: ProductUpdate):
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    if updates:
        await db.products.update_one({"id": pid}, {"$set": updates})
    doc = await db.products.find_one({"id": pid}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Product not found")
    return Product(**doc)


@api_router.delete("/products/{pid}")
async def delete_product(pid: str):
    await db.products.delete_one({"id": pid})
    return {"success": True}


# ---------- Stock Movements ----------
@api_router.get("/stock-movements", response_model=List[StockMovement])
async def list_stock_movements(product_id: Optional[str] = None):
    q = {}
    if product_id:
        q["product_id"] = product_id
    docs = await db.stock_movements.find(q, {"_id": 0}).sort("created_at", -1).to_list(500)
    return [StockMovement(**d) for d in docs]


@api_router.post("/stock-movements", response_model=StockMovement)
async def create_stock_movement(body: StockMovementCreate):
    prod = await db.products.find_one({"id": body.product_id}, {"_id": 0})
    if not prod:
        raise HTTPException(status_code=404, detail="Product not found")
    # Compute new stock
    delta = body.qty if body.type == "in" else (-body.qty if body.type == "out" else 0)
    new_stock = prod.get("stock", 0) + delta if body.type != "adjust" else body.qty
    await db.products.update_one({"id": body.product_id}, {"$set": {"stock": new_stock}})
    doc_no = f"SM{datetime.now(timezone.utc).strftime('%y%m%d%H%M%S')}"
    mv = StockMovement(
        product_id=body.product_id,
        product_name=prod["name"],
        type=body.type,
        qty=body.qty,
        note=body.note,
        document_no=doc_no,
    )
    await db.stock_movements.insert_one(mv.model_dump())
    return mv

# ---------- Beam constants ----------
BEAM_PLAYGROUND_URL = "https://playground.api.beamcheckout.com"
BEAM_PRODUCTION_URL = "https://api.beamcheckout.com"
BEAM_API_KEY_MASK_PREFIX = "••••"
SATANG_PER_THB = 100
BEAM_POST_TIMEOUT_S = 15.0
BEAM_GET_TIMEOUT_S = 10.0


def _mask_api_key(s: Settings) -> Settings:
    """Mask the Beam API key on outgoing Settings — return only the last 4 chars."""
    if s.beam_api_key and len(s.beam_api_key) > 4:
        s.beam_api_key = BEAM_API_KEY_MASK_PREFIX + s.beam_api_key[-4:]
    return s


def _is_masked_api_key(value: str) -> bool:
    """True if value looks like a masked placeholder previously emitted by _mask_api_key."""
    if not value or not value.startswith(BEAM_API_KEY_MASK_PREFIX):
        return False
    suffix = value[len(BEAM_API_KEY_MASK_PREFIX):]
    return len(suffix) == 4


async def _beam_credentials():
    """Load Beam credentials from settings or raise 400 if missing.

    Returns (base_url, headers) where headers contains the Basic auth header.
    """
    doc = await db.settings.find_one({"id": "shop"}, {"_id": 0})
    settings = Settings(**doc) if doc else Settings()

    if not settings.beam_merchant_id or not settings.beam_api_key:
        raise HTTPException(
            status_code=400,
            detail="Beam credentials not configured. Go to Settings → Payment to add your Merchant ID and API Key.",
        )

    base_url = BEAM_PLAYGROUND_URL if settings.beam_sandbox else BEAM_PRODUCTION_URL
    beam_token = base64.b64encode(
        f"{settings.beam_merchant_id}:{settings.beam_api_key}".encode()
    ).decode()
    headers = {"Authorization": f"Basic {beam_token}"}
    return base_url, headers


# ---------- Settings ----------
@api_router.get("/settings", response_model=Settings)
async def get_settings():
    doc = await db.settings.find_one({"id": "shop"}, {"_id": 0})
    if not doc:
        s = Settings()
        await db.settings.insert_one(s.model_dump())
        return s
    return _mask_api_key(Settings(**doc))


@api_router.put("/settings", response_model=Settings)
async def update_settings(body: SettingsUpdate):
    updates = {k: v for k, v in body.model_dump().items() if v is not None}
    # Don't overwrite beam_api_key if the client sent back the masked placeholder
    # (matches MASK_PREFIX + 4 chars exactly — see _is_masked_api_key)
    if "beam_api_key" in updates and _is_masked_api_key(updates["beam_api_key"]):
        del updates["beam_api_key"]
    await db.settings.update_one({"id": "shop"}, {"$set": updates}, upsert=True)
    doc = await db.settings.find_one({"id": "shop"}, {"_id": 0})
    return _mask_api_key(Settings(**doc))


# ---------- Beam Payment ----------
class BeamChargeRequest(BaseModel):
    amount: float          # THB amount (e.g. 350.00)
    reference_id: str      # order reference / order number
    description: Optional[str] = None


class BeamChargeResponse(BaseModel):
    """Response when creating a Beam charge.

    Beam upstream contract for QR_PROMPT_PAY (see https://docs.beamcheckout.com):
      - id:            charge identifier
      - status:        PENDING | COMPLETED | FAILED | EXPIRED
      - actionRequired: "ENCODED_IMAGE" for QR PromptPay
      - encodedImage.image:    base64-encoded PNG (rendered as data URI on client)
      - encodedImage.qrString: raw QR payload (fallback for client-side rendering)
    """
    charge_id: str
    status: str
    qr_image: Optional[str] = None   # base64 PNG data
    qr_string: Optional[str] = None  # raw QR string for rendering client-side
    amount: float
    currency: str = "THB"


class BeamChargeStatus(BaseModel):
    """Narrower response used for polling — never includes QR data."""
    charge_id: str
    status: str
    amount: float
    currency: str = "THB"


@api_router.post("/beam/charge", response_model=BeamChargeResponse)
async def create_beam_charge(body: BeamChargeRequest):
    """Create a Beam QR PromptPay charge. Returns a QR code image for the customer to scan."""
    base_url, auth_headers = await _beam_credentials()

    payload = {
        "amount": int(round(body.amount * SATANG_PER_THB)),  # Beam uses satang (100 satang = 1 THB)
        "currency": "THB",
        "referenceId": body.reference_id,
        "description": body.description or f"Order {body.reference_id}",
        "paymentMethod": {
            "paymentMethodType": "QR_PROMPT_PAY",
            "qrPromptPay": {}
        }
    }

    try:
        async with httpx.AsyncClient(timeout=BEAM_POST_TIMEOUT_S) as client:
            resp = await client.post(
                f"{base_url}/api/v1/charges",
                json=payload,
                headers={**auth_headers, "Content-Type": "application/json"},
            )
    except httpx.TimeoutException:
        raise HTTPException(status_code=502, detail="Beam API timed out. Please try again.")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Cannot reach Beam API: {str(e)}")

    if resp.status_code == 401:
        raise HTTPException(status_code=401, detail="Beam API key is invalid or expired.")
    if resp.status_code not in (200, 201):
        raise HTTPException(status_code=502, detail=f"Beam API error {resp.status_code}: {resp.text[:300]}")

    data = resp.json()
    # Beam's documented field is "id"; the fallbacks defend against a
    # potential future rename and are intentional.
    charge_id = data.get("id") or data.get("chargeId") or data.get("charge_id") or ""
    if not charge_id:
        raise HTTPException(
            status_code=502,
            detail="Beam response did not include a charge id; cannot poll status.",
        )
    status = data.get("status", "PENDING")

    # Beam returns encodedImage when actionRequired == ENCODED_IMAGE
    qr_image = None
    qr_string = None
    if data.get("actionRequired") == "ENCODED_IMAGE":
        encoded = data.get("encodedImage", {})
        qr_image = encoded.get("image")   # base64 PNG
        qr_string = encoded.get("qrString")
    elif data.get("qrCode"):
        qr_image = data["qrCode"]

    return BeamChargeResponse(
        charge_id=charge_id,
        status=status,
        qr_image=qr_image,
        qr_string=qr_string,
        amount=body.amount,
    )


@api_router.get("/beam/charge/{charge_id}", response_model=BeamChargeStatus)
async def get_beam_charge(charge_id: str):
    """Poll the status of a Beam charge."""
    base_url, auth_headers = await _beam_credentials()

    try:
        async with httpx.AsyncClient(timeout=BEAM_GET_TIMEOUT_S) as client:
            resp = await client.get(
                f"{base_url}/api/v1/charges/{charge_id}",
                headers=auth_headers,
            )
    except httpx.TimeoutException:
        raise HTTPException(status_code=502, detail="Beam API timed out.")
    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=f"Cannot reach Beam API: {str(e)}")

    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="Charge not found")
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"Beam API error {resp.status_code}")

    data = resp.json()
    return BeamChargeStatus(
        charge_id=charge_id,
        status=data.get("status", "PENDING"),
        amount=float(data.get("amount") or 0) / SATANG_PER_THB,
    )


# ---------- Dashboard ----------

@api_router.get("/dashboard")
async def dashboard(period: str = "month"):
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        start = now - timedelta(days=7)
    elif period == "year":
        start = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    else:
        start = now - timedelta(days=30)

    docs = await db.orders.find(
        {"created_at": {"$gte": start.isoformat()}, "status": {"$ne": "cancel"}},
        {"_id": 0},
    ).to_list(10000)

    total_sales = sum(o["total"] for o in docs)
    tx_count = len(docs)
    avg_bill = (total_sales / tx_count) if tx_count else 0
    cost_total = 0.0
    for o in docs:
        for item in o.get("items", []):
            # lookup product cost
            prod = await db.products.find_one({"id": item["product_id"]}, {"_id": 0, "cost": 1})
            if prod:
                cost_total += (prod.get("cost", 0) or 0) * item.get("qty", 1)
    profit = total_sales - cost_total

    # Sales by day (last 7 buckets for simplicity)
    buckets: dict = {}
    for o in docs:
        try:
            d = o["created_at"][:10]
            buckets[d] = buckets.get(d, 0) + o["total"]
        except Exception:
            pass
    timeline = [{"label": k, "value": v} for k, v in sorted(buckets.items())[-7:]]

    # Top products
    prod_totals: dict = {}
    for o in docs:
        for item in o.get("items", []):
            key = item["product_id"]
            prod_totals[key] = prod_totals.get(
                key, {"product_id": key, "name": item["name"], "total": 0, "qty": 0}
            )
            prod_totals[key]["total"] += item["price"] * item["qty"]
            prod_totals[key]["qty"] += item["qty"]
    top_products = sorted(prod_totals.values(), key=lambda x: -x["total"])[:5]

    # Top categories
    cat_totals: dict = {}
    cats = {c["id"]: c for c in await db.categories.find({}, {"_id": 0}).to_list(100)}
    for o in docs:
        for item in o.get("items", []):
            prod = await db.products.find_one({"id": item["product_id"]}, {"_id": 0, "category_id": 1})
            if prod:
                cid = prod.get("category_id", "")
                cname = cats.get(cid, {}).get("name", "Other")
                cat_totals[cname] = cat_totals.get(cname, 0) + item["price"] * item["qty"]
    top_categories = [
        {"name": k, "total": v}
        for k, v in sorted(cat_totals.items(), key=lambda x: -x[1])[:5]
    ]

    return {
        "period": period,
        "total_sales": total_sales,
        "cost": cost_total,
        "profit": profit,
        "gp_percent": (profit / total_sales * 100) if total_sales else 0,
        "tx_count": tx_count,
        "avg_bill": avg_bill,
        "timeline": timeline,
        "top_products": top_products,
        "top_categories": top_categories,
    }


# ---------- Shift endpoints ----------
@api_router.get("/shifts/current", response_model=Optional[Shift])
async def get_current_shift():
    doc = await db.shifts.find_one({"status": "open"}, {"_id": 0}, sort=[("opened_at", -1)])
    return Shift(**doc) if doc else None


@api_router.post("/shifts/open", response_model=Shift)
async def open_shift(body: ShiftOpen):
    existing = await db.shifts.find_one({"status": "open"})
    if existing:
        raise HTTPException(status_code=400, detail="Shift already open")
    count = await db.shifts.count_documents({})
    s = Shift(round_number=count + 1, start_cash=body.start_cash, opened_by=body.opened_by)
    await db.shifts.insert_one(s.model_dump())
    return s


@api_router.post("/shifts/movement", response_model=ShiftMovement)
async def add_movement(body: ShiftMovementCreate):
    shift = await db.shifts.find_one({"status": "open"}, {"_id": 0})
    if not shift:
        raise HTTPException(status_code=400, detail="No open shift")
    mv = ShiftMovement(shift_id=shift["id"], **body.model_dump())
    await db.shift_movements.insert_one(mv.model_dump())
    key = "total_paid_in" if body.type == "paid_in" else "total_paid_out"
    await db.shifts.update_one({"id": shift["id"]}, {"$inc": {key: body.amount}})
    return mv


@api_router.put("/shifts/close", response_model=Shift)
async def close_shift(body: ShiftClose):
    shift = await db.shifts.find_one({"status": "open"}, {"_id": 0})
    if not shift:
        raise HTTPException(status_code=400, detail="No open shift")
    # Calculate total_sales_cash from orders paid with cash methods since shift opened
    cash_orders = await db.orders.find({
        "created_at": {"$gte": shift["opened_at"]},
        "payment_method": {"$in": ["Easy Pay", "Cash"]},
    }, {"_id": 0}).to_list(10000)
    total_cash = sum(o.get("total", 0) for o in cash_orders)
    expected = shift["start_cash"] + total_cash + shift.get("total_paid_in", 0) - shift.get("total_paid_out", 0)
    await db.shifts.update_one({"id": shift["id"]}, {"$set": {
        "status": "closed",
        "closed_at": datetime.now(timezone.utc).isoformat(),
        "closed_by": body.closed_by,
        "actual_in_drawer": body.actual_in_drawer,
        "total_sales_cash": total_cash,
        "expected_in_drawer": expected,
    }})
    doc = await db.shifts.find_one({"id": shift["id"]}, {"_id": 0})
    return Shift(**doc)


@api_router.get("/shifts", response_model=List[Shift])
async def list_shifts():
    docs = await db.shifts.find({}, {"_id": 0}).sort("opened_at", -1).to_list(100)
    return [Shift(**d) for d in docs]


@api_router.get("/customers", response_model=List[Customer])
async def list_customers(q: Optional[str] = None):
    filt = {}
    if q:
        filt = {"$or": [
            {"name": {"$regex": q, "$options": "i"}},
            {"phone": {"$regex": q, "$options": "i"}},
        ]}
    docs = await db.customers.find(filt, {"_id": 0}).to_list(500)
    return [Customer(**d) for d in docs]


@api_router.post("/customers", response_model=Customer)
async def create_customer(body: CustomerCreate):
    c = Customer(name=body.name, phone=body.phone)
    await db.customers.insert_one(c.model_dump())
    return c


@api_router.post("/orders", response_model=Order)
async def create_order(body: OrderCreate):
    order_number = await gen_order_number()
    o = Order(order_number=order_number, **body.model_dump(), status="completed")
    await db.orders.insert_one(o.model_dump())
    return o


@api_router.get("/orders", response_model=List[Order])
async def list_orders(source: Optional[str] = None, status: Optional[str] = None):
    q = {}
    if source and source != "all":
        q["source"] = source
    if status:
        q["status"] = status
    docs = await db.orders.find(q, {"_id": 0}).sort("created_at", -1).to_list(500)
    return [Order(**d) for d in docs]


@api_router.put("/orders/{order_id}/status", response_model=Order)
async def update_order_status(order_id: str, body: OrderStatusUpdate):
    await db.orders.update_one({"id": order_id}, {"$set": {"status": body.status}})
    doc = await db.orders.find_one({"id": order_id}, {"_id": 0})
    if not doc:
        raise HTTPException(status_code=404, detail="Order not found")
    return Order(**doc)


@api_router.get("/parked-orders", response_model=List[ParkedOrder])
async def list_parked():
    docs = await db.parked_orders.find({}, {"_id": 0}).sort("created_at", -1).to_list(500)
    return [ParkedOrder(**d) for d in docs]


@api_router.post("/parked-orders", response_model=ParkedOrder)
async def park_order(body: ParkedOrderCreate):
    p = ParkedOrder(**body.model_dump())
    await db.parked_orders.insert_one(p.model_dump())
    return p


@api_router.delete("/parked-orders/{pid}")
async def delete_parked(pid: str):
    await db.parked_orders.delete_one({"id": pid})
    return {"success": True}


@api_router.post("/seed")
async def seed_data():
    """Idempotent seed: wipes and reseeds demo bakery data."""
    await db.categories.delete_many({})
    await db.products.delete_many({})
    await db.customers.delete_many({})
    await db.orders.delete_many({})
    await db.parked_orders.delete_many({})
    await db.stock_movements.delete_many({})

    # Categories (expanded to match screenshots)
    cats = [
        {"name": "Favorite", "name_th": "รายการโปรด", "color": "#00B14F", "order": 0, "source": None},
        {"name": "Valentine's Collection", "name_th": "วาเลนไทน์", "color": "#EC4899", "order": 1, "source": "Grabfood"},
        {"name": "Hot Promotion!", "name_th": "โปรโมชั่น", "color": "#F59E0B", "order": 2, "source": "Grabfood"},
        {"name": "Christmas Collection", "name_th": "คริสต์มาส", "color": "#EF4444", "order": 3, "source": "Grabfood"},
        {"name": "Cake Slices", "name_th": "เค้กชิ้น", "color": "#94A3B8", "order": 4, "source": "Grabfood"},
        {"name": "Choco Gems", "name_th": "ช็อกโกเจม", "color": "#00B14F", "order": 5, "source": "Grabfood"},
        {"name": "Small Cookies", "name_th": "คุกกี้เล็ก", "color": "#00B14F", "order": 6, "source": None},
        {"name": "Cookie Cake", "name_th": "คุกกี้เค้ก", "color": "#00B14F", "order": 7, "source": "Grabfood"},
        {"name": "Dream Cake box", "name_th": "ดรีมเค้กบ็อกซ์", "color": "#8B5CF6", "order": 8, "source": "Grabfood"},
        {"name": "Mini Birthday Cake", "name_th": "มินิเค้กวันเกิด", "color": "#F59E0B", "order": 9, "source": "Grabfood"},
        {"name": "Brownie Bites", "name_th": "บราวนี่", "color": "#7C2D12", "order": 10, "source": None},
        {"name": "Say something sweet", "name_th": "คำหวาน", "color": "#EC4899", "order": 11, "source": None},
        {"name": "Dubai Chocolate", "name_th": "ดูไบช็อกโกแลต", "color": "#92400E", "order": 12, "source": "Grabfood"},
        {"name": "MEGA Hot Deals", "name_th": "โปรโมชั่นเด็ด", "color": "#DC2626", "order": 13, "source": "Grabfood"},
    ]
    cat_objs = [Category(**c) for c in cats]
    await db.categories.insert_many([c.model_dump() for c in cat_objs])
    cat_map = {c.name: c.id for c in cat_objs}

    IMG = {
        "Choco Gems": "https://images.pexels.com/photos/9419469/pexels-photo-9419469.jpeg?auto=compress&cs=tinysrgb&dpr=2&h=650&w=940",
        "Mousse": "https://images.unsplash.com/photo-1713274785893-8879f807aff6?crop=entropy&cs=srgb&fm=jpg&ixid=M3w3NTY2OTV8MHwxfHNlYXJjaHwxfHxtb3Vzc2UlMjBjYWtlfGVufDB8fHx8MTc3Njg0ODM1Mnww&ixlib=rb-4.1.0&q=85",
        "Soft Cookies": "https://images.pexels.com/photos/36500580/pexels-photo-36500580.jpeg?auto=compress&cs=tinysrgb&dpr=2&h=650&w=940",
        "Dubai Chocolate": "https://images.pexels.com/photos/9279001/pexels-photo-9279001.jpeg?auto=compress&cs=tinysrgb&dpr=2&h=650&w=940",
        "Cookie Cake": "https://images.unsplash.com/photo-1694588915262-30d22a36b379?crop=entropy&cs=srgb&fm=jpg&ixid=M3w3NTY2Njd8MHwxfHNlYXJjaHwxfHxjb29raWUlMjBjYWtlfGVufDB8fHx8MTc3Njg0ODM1Nnww&ixlib=rb-4.1.0&q=85",
        "Brownie": "https://images.pexels.com/photos/45202/brownie-dessert-cake-sweet-45202.jpeg?auto=compress&cs=tinysrgb&dpr=2&h=650&w=940",
        "Valentine": "https://images.unsplash.com/photo-1511381939415-e44015466834?w=600",
    }

    products_data = [
        # Favorite / Choco Gems
        {"name": "Chocogems pop Baby edition", "name_th": "ช็อกโกเจมป๊อปเบบี้", "price": 350, "cost": 180, "category": "Choco Gems", "image": IMG["Choco Gems"], "favorite": True, "stock": 24},
        {"name": "Choco Gems Pop", "name_th": "ช็อกโกเจมป๊อป", "price": 299, "cost": 150, "category": "Choco Gems", "image": IMG["Choco Gems"], "favorite": True, "stock": 32},
        {"name": "Mayongchid Choco Gems Pop", "name_th": "มะยงชิดช็อกโกเจม", "price": 350, "cost": 180, "category": "Choco Gems", "image": IMG["Choco Gems"], "favorite": True, "stock": 18},
        {"name": "Summer Edition Choco Gems Pop (BOX)", "name_th": "ช็อกโกเจมซัมเมอร์", "price": 350, "cost": 180, "category": "Choco Gems", "image": IMG["Choco Gems"], "stock": 12},

        # Dubai Chocolate
        {"name": "Dubai Matcha Strawberry Mochi กล่อง 4 ชิ้น", "name_th": "ดูไบมัทฉะสตรอเบอร์รี่", "price": 399, "cost": 199, "category": "Dubai Chocolate", "image": IMG["Dubai Chocolate"], "favorite": True, "stock": 15},
        {"name": "Dubai Matcha Strawberry Mochi กล่อง 8 ชิ้น", "name_th": "ดูไบมัทฉะสตรอเบอร์รี่ 8", "price": 699, "cost": 350, "category": "Dubai Chocolate", "image": IMG["Dubai Chocolate"], "stock": 8},
        {"name": "Dubai Chewy Cookies", "name_th": "ดูไบชิววี่", "price": 299, "cost": 149, "category": "Dubai Chocolate", "image": IMG["Dubai Chocolate"], "stock": 22},
        {"name": "Dubai Classic Bar", "name_th": "ดูไบคลาสสิก", "price": 450, "cost": 225, "category": "Dubai Chocolate", "image": IMG["Dubai Chocolate"], "stock": 10},

        # Small Cookies (with negative stocks like the screenshot!)
        {"name": "Mama OG Dark Chocolate Walnut Cookie", "name_th": "คุกกี้ดาร์กช็อกโกแลต วอลนัท", "price": 95, "cost": 40, "category": "Small Cookies", "image": IMG["Soft Cookies"], "stock": 0},
        {"name": "The Marching Ladies Cookie", "name_th": "มาร์ชิ่งเลดี้ส์", "price": 95, "cost": 40, "category": "Small Cookies", "image": IMG["Soft Cookies"], "stock": -59},
        {"name": "Sexy Back Cookie", "name_th": "เซ็กซี่แบ็กคุกกี้", "price": 95, "cost": 40, "category": "Small Cookies", "image": IMG["Soft Cookies"], "favorite": True, "stock": -32},
        {"name": "CORNFLAKE MARSHMALLOW", "name_th": "คอร์นเฟลกมาร์ชเมลโลว์", "price": 95, "cost": 40, "category": "Small Cookies", "image": IMG["Soft Cookies"], "stock": 0},
        {"name": "Double Trouble Dark Chocolate Cookie", "name_th": "ดับเบิ้ลทรับเบิ้ล", "price": 95, "cost": 40, "category": "Small Cookies", "image": IMG["Soft Cookies"], "stock": 0},

        # Cookie Cake
        {"name": "Pink Birthday Cookie Cake (1lb)", "name_th": "พิงค์ เบิร์ธเดย์", "price": 590, "cost": 280, "category": "Cookie Cake", "image": IMG["Cookie Cake"], "favorite": True, "stock": 6},
        {"name": "Breakfast Confetti Birthday Sized Cake", "name_th": "เบรกฟาสต์คอนเฟ็ตตี้", "price": 590, "cost": 280, "category": "Cookie Cake", "image": IMG["Cookie Cake"], "stock": 4},
        {"name": "Mini Strawberry Shortcake", "name_th": "มินิสตรอเบอร์รี่", "price": 690, "cost": 320, "category": "Cookie Cake", "image": IMG["Cookie Cake"], "stock": 3},
        {"name": "Chocolate Fudge Cookie Cake", "name_th": "ช็อกโกแลตฟัดจ์", "price": 690, "cost": 320, "category": "Cookie Cake", "image": IMG["Cookie Cake"], "stock": 5},

        # Cake Slices
        {"name": "Red Velvet Cookie Cake Slice", "name_th": "เรดเวลเว็ทชิ้น", "price": 160, "cost": 70, "category": "Cake Slices", "image": IMG["Cookie Cake"], "stock": 12},
        {"name": "Chocolate Pudding Slice", "name_th": "ช็อกโกแลตพุดดิ้ง", "price": 160, "cost": 70, "category": "Cake Slices", "image": IMG["Cookie Cake"], "stock": 10},

        # Christmas
        {"name": "Crystal Velvet Tanghulu Cookie", "name_th": "คริสตัลเวลเว็ท", "price": 160, "cost": 70, "category": "Christmas Collection", "image": IMG["Soft Cookies"], "stock": 18},
        {"name": "Crystal Blueberry Tanghulu Cookie", "name_th": "คริสตัลบลูเบอร์รี่", "price": 160, "cost": 70, "category": "Christmas Collection", "image": IMG["Soft Cookies"], "stock": 16},
        {"name": "Snowflake Xmas Tree Cake (mini)", "name_th": "สโนว์เฟลก มินิ", "price": 650, "cost": 300, "category": "Christmas Collection", "image": IMG["Cookie Cake"], "stock": 4},
        {"name": "Snowflake Xmas Tree Cake (S)", "name_th": "สโนว์เฟลก เอส", "price": 1650, "cost": 800, "category": "Christmas Collection", "image": IMG["Cookie Cake"], "stock": 2},

        # Mousse / Valentine
        {"name": "Raspberry Mousse", "name_th": "ราสเบอร์รี่ มูส", "price": 95, "cost": 45, "category": "Cake Slices", "image": IMG["Mousse"], "stock": 14},
        {"name": "Strawberry Love Cake", "name_th": "เค้กความรัก", "price": 590, "cost": 280, "category": "Valentine's Collection", "image": IMG["Valentine"], "stock": 6},

        # Hot Promotion
        {"name": "Box of 9pcs Bae Brownie", "name_th": "แบบราวนี่ 9 ชิ้น", "price": 380, "cost": 180, "category": "Hot Promotion!", "image": IMG["Brownie"], "stock": 10},
        {"name": "Firecracker Candle (เทียนพลุ)", "name_th": "เทียนพลุ", "price": 100, "cost": 30, "category": "Hot Promotion!", "image": IMG["Soft Cookies"], "stock": 40},

        # Brownie Bites
        {"name": "Classic Brownie Bite", "name_th": "บราวนี่คลาสสิก", "price": 45, "cost": 18, "category": "Brownie Bites", "image": IMG["Brownie"], "stock": 50},
        {"name": "Salted Caramel Brownie", "name_th": "ซอลเทดคาราเมล", "price": 55, "cost": 22, "category": "Brownie Bites", "image": IMG["Brownie"], "stock": 36},

        # Mini Birthday Cake (from screenshots)
        {"name": "Ballerina (Birthday Cake) เค้กวันเกิด", "name_th": "บัลเลริน่า", "price": 650, "cost": 300, "category": "Mini Birthday Cake", "image": IMG["Cookie Cake"], "stock": 5},
        {"name": "Mini Strawberry Shortcake (Mini)", "name_th": "มินิสตรอเบอร์รี่ (มินิ)", "price": 690, "cost": 320, "category": "Mini Birthday Cake", "image": IMG["Cookie Cake"], "stock": 4},
        {"name": "Mini Snowflake Cake", "name_th": "มินิสโนว์เฟลก", "price": 650, "cost": 300, "category": "Mini Birthday Cake", "image": IMG["Cookie Cake"], "stock": 3},
        {"name": "Mini Cherry Amour - 0.5 LB (Birthday)", "name_th": "มินิเชอร์รี่อามัวร์", "price": 690, "cost": 320, "category": "Mini Birthday Cake", "image": IMG["Cookie Cake"], "stock": 2},
        {"name": "mini white glitter queen", "name_th": "มินิไวท์กลิตเตอร์ควีน", "price": 650, "cost": 300, "category": "Mini Birthday Cake", "image": IMG["Cookie Cake"], "stock": 6},
        {"name": "Mini Glitter Baby Blue", "name_th": "มินิกลิตเตอร์เบบี้บลู", "price": 650, "cost": 300, "category": "Mini Birthday Cake", "image": IMG["Cookie Cake"], "stock": 4},
        {"name": "Mini Black Forest Cake", "name_th": "มินิแบล็คฟอเรสต์", "price": 690, "cost": 320, "category": "Mini Birthday Cake", "image": IMG["Cookie Cake"], "stock": 3},
        {"name": "Mini Glitter Pink", "name_th": "มินิกลิตเตอร์พิงค์", "price": 650, "cost": 300, "category": "Mini Birthday Cake", "image": IMG["Cookie Cake"], "stock": 5},

        # Christmas Collection - more
        {"name": "Xmas Cookies Gift Box ซื้อ 2 แถมคุกกี้ใหญ่", "name_th": "เซ็ตคุกกี้ Xmas", "price": 420, "cost": 200, "category": "Christmas Collection", "image": IMG["Soft Cookies"], "stock": 8},
        {"name": "Cereal Cookie Crunch", "name_th": "ซีเรียลคุกกี้ครันช์", "price": 180, "cost": 70, "category": "Christmas Collection", "image": IMG["Soft Cookies"], "stock": 12},
        {"name": "Cornflake Crunch Chocolate Cake Box", "name_th": "คอร์นเฟลกเค้กกรอบ", "price": 390, "cost": 180, "category": "Christmas Collection", "image": IMG["Cookie Cake"], "stock": 6},
        {"name": "Cornflake Crunch Chocolate Cake (slice)", "name_th": "คอร์นเฟลกเค้กชิ้น", "price": 250, "cost": 120, "category": "Christmas Collection", "image": IMG["Cookie Cake"], "stock": 10},

        # Hot Promotion (BOM / Bundle)
        {"name": "Buy 2 Get 1 Free! - Dubai Collection", "name_th": "ซื้อ 2 แถม 1 - ดูไบ", "price": 1100, "cost": 0, "category": "Hot Promotion!", "image": IMG["Dubai Chocolate"], "stock": 0, "product_type": "BOM"},
    ]

    products = []
    for p in products_data:
        products.append(Product(
            name=p["name"],
            name_th=p.get("name_th"),
            price=p["price"],
            cost=p.get("cost", 0),
            category_id=cat_map[p["category"]],
            image_url=p["image"],
            is_favorite=p.get("favorite", False),
            stock=p.get("stock", 0),
            tax_type="V",
            product_type=p.get("product_type", "P"),
        ))
    await db.products.insert_many([p.model_dump() for p in products])

    # Customers
    customers_data = [
        {"name": "bb bb", "phone": None, "color": "#EF4444"},
        {"name": "Sunisa Chaiprom", "phone": "020128575", "color": "#94A3B8"},
        {"name": "fisa ml", "phone": None, "last_visit": "2026-03-02", "color": "#10B981"},
        {"name": "CEVA AIR OCEAN (THAILAND) CO., LTD.", "phone": None, "color": "#94A3B8"},
        {"name": "CHAGEE (THAILAND) COMPANY LIMITED", "phone": None, "color": "#94A3B8"},
        {"name": "อาภาทิพ เหรียญเจริญ", "phone": "028320700", "color": "#94A3B8"},
        {"name": "j k", "phone": None, "color": "#F59E0B"},
        {"name": "เ อ", "phone": None, "color": "#10B981"},
        {"name": "cc gg", "phone": "02-2620140", "color": "#334155"},
        {"name": "Louis Vuitton (Thailand) S.A (Head Office)", "phone": None, "color": "#7C3AED"},
        {"name": "DUCKKING บริษัท ดั๊กคิง จำกัด", "phone": "0385881178", "color": "#F59E0B"},
    ]
    customers = [Customer(**c) for c in customers_data]
    await db.customers.insert_many([c.model_dump() for c in customers])

    # Historical orders (distributed over last 30 days for dashboard)
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    import random
    random.seed(42)
    order_count = 0
    for days_ago in range(30):
        day = now - timedelta(days=days_ago)
        # 3-8 orders per day, fewer on weekends for realism
        n = random.randint(3, 8)
        for _ in range(n):
            # random items
            sample = random.sample(products, random.randint(1, 3))
            items = [OrderItem(
                product_id=p.id, name=p.name, price=p.price, qty=random.randint(1, 3)
            ) for p in sample]
            total = sum(i.price * i.qty for i in items)
            o = Order(
                order_number=f"PS001{1000 + order_count:06d}",
                items=items,
                subtotal=total,
                total=total,
                paid_amount=total,
                status="completed",
                source=random.choice(["table", "delivery", "table", "kiosk"]),
                payment_method=random.choice(["Easy Pay", "PromptPay", "QR Kbank", "Credit"]),
                delivery_provider="Grab" if random.random() < 0.3 else None,
                delivery_status="DELIVERED" if random.random() < 0.5 else None,
                created_time=day.strftime("%H:%M"),
                created_at=(day - timedelta(hours=random.randint(9, 21))).isoformat(),
            )
            await db.orders.insert_one(o.model_dump())
            order_count += 1

    # Active delivery orders (like the Order Hub screenshot)
    sample_items = [OrderItem(product_id=products[0].id, name=products[0].name, price=products[0].price, qty=1)]
    for live in [
        {"num": "PS001989001", "total": 290.0, "status": "completed", "delivery_status": "DELIVERING", "time": "14:34"},
        {"num": "PS001643001", "total": 630.0, "status": "completed", "delivery_status": "DELIVERING", "time": "14:16"},
    ]:
        o = Order(
            order_number=live["num"],
            items=sample_items,
            subtotal=live["total"],
            total=live["total"],
            paid_amount=live["total"],
            status="completed",
            source="delivery",
            payment_method="PromptPay",
            delivery_provider="Grab",
            delivery_status=live["delivery_status"],
            created_time=live["time"],
            created_at=now.isoformat(),
        )
        await db.orders.insert_one(o.model_dump())

    # Some stock movements (to match the -59 / -32 numbers)
    marching = next(p for p in products if "Marching Ladies" in p.name)
    sexy = next(p for p in products if "Sexy Back" in p.name)
    await db.stock_movements.insert_many([
        StockMovement(
            product_id=marching.id, product_name=marching.name,
            type="out", qty=59, note="Sold on Grabfood",
            document_no="SM260422143001",
        ).model_dump(),
        StockMovement(
            product_id=sexy.id, product_name=sexy.name,
            type="out", qty=32, note="Sold on Grabfood",
            document_no="SM260422143002",
        ).model_dump(),
    ])

    # Default settings
    await db.settings.delete_many({})
    await db.settings.insert_one(Settings().model_dump())

    return {
        "categories": len(cat_objs),
        "products": len(products),
        "customers": len(customers),
        "orders": order_count + 2,
    }


app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


@app.on_event("startup")
async def auto_seed():
    count = await db.categories.count_documents({})
    if count == 0:
        logger.info("Empty database — seeding demo data")
        await seed_data()


@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()
