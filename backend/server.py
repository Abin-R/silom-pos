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


class Product(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    name: str
    name_th: Optional[str] = None
    price: float
    category_id: str
    image_url: str
    is_favorite: bool = False


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


# ---------- Helpers ----------
def strip_id(doc):
    if doc and "_id" in doc:
        doc.pop("_id", None)
    return doc


async def gen_order_number() -> str:
    """Generate unique order number like GF-XXX."""
    for _ in range(10):
        num = uuid.uuid4().int % 1000
        order_number = f"GF-{num:03d}"
        existing = await db.orders.find_one({"order_number": order_number})
        if not existing:
            return order_number
    return f"GF-{uuid.uuid4().hex[:4].upper()}"


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


@api_router.get("/products", response_model=List[Product])
async def list_products(category_id: Optional[str] = None, favorite: Optional[bool] = None):
    q = {}
    if category_id:
        q["category_id"] = category_id
    if favorite is not None:
        q["is_favorite"] = favorite
    docs = await db.products.find(q, {"_id": 0}).to_list(500)
    return [Product(**d) for d in docs]


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

    # Categories
    cats = [
        {"name": "Favorite", "name_th": "รายการโปรด", "color": "#00B14F", "order": 0},
        {"name": "Choco Gems", "name_th": "ช็อกโกเจม", "color": "#00B14F", "order": 1},
        {"name": "Mousse Cake", "name_th": "มูสเค้ก", "color": "#00B14F", "order": 2},
        {"name": "Soft Cookies", "name_th": "ซอฟต์คุกกี้", "color": "#00B14F", "order": 3},
        {"name": "Dubai Chocolate", "name_th": "ดูไบช็อกโกแลต", "color": "#00B14F", "order": 4},
        {"name": "Cookie Cake", "name_th": "คุกกี้เค้ก", "color": "#00B14F", "order": 5},
    ]
    cat_objs = [Category(**c) for c in cats]
    await db.categories.insert_many([c.model_dump() for c in cat_objs])
    cat_map = {c.name: c.id for c in cat_objs}

    # Images by category
    IMG = {
        "Choco Gems": "https://images.pexels.com/photos/9419469/pexels-photo-9419469.jpeg?auto=compress&cs=tinysrgb&dpr=2&h=650&w=940",
        "Mousse Cake": "https://images.unsplash.com/photo-1713274785893-8879f807aff6?crop=entropy&cs=srgb&fm=jpg&ixid=M3w3NTY2OTV8MHwxfHNlYXJjaHwxfHxtb3Vzc2UlMjBjYWtlfGVufDB8fHx8MTc3Njg0ODM1Mnww&ixlib=rb-4.1.0&q=85",
        "Soft Cookies": "https://images.pexels.com/photos/36500580/pexels-photo-36500580.jpeg?auto=compress&cs=tinysrgb&dpr=2&h=650&w=940",
        "Dubai Chocolate": "https://images.pexels.com/photos/9279001/pexels-photo-9279001.jpeg?auto=compress&cs=tinysrgb&dpr=2&h=650&w=940",
        "Cookie Cake": "https://images.unsplash.com/photo-1694588915262-30d22a36b379?crop=entropy&cs=srgb&fm=jpg&ixid=M3w3NTY2Njd8MHwxfHNlYXJjaHwxfHxjb29raWUlMjBjYWtlfGVufDB8fHx8MTc3Njg0ODM1Nnww&ixlib=rb-4.1.0&q=85",
    }

    # Products (from screenshots + extras)
    products_data = [
        # Choco Gems
        {"name": "Chocogems pop Baby edition", "name_th": "ช็อกโกเจมป๊อปเบบี้", "price": 350, "category": "Choco Gems", "image": IMG["Choco Gems"], "favorite": True},
        {"name": "Choco Gems Pop", "name_th": "ช็อกโกเจมป๊อป", "price": 299, "category": "Choco Gems", "image": IMG["Choco Gems"]},
        {"name": "Choco Gems Pop Ice Cream", "name_th": "ช็อกโกเจมไอศกรีม", "price": 350, "category": "Choco Gems", "image": IMG["Choco Gems"]},
        {"name": "Mini Chocogems", "name_th": "มินิช็อกโกเจม", "price": 199, "category": "Choco Gems", "image": IMG["Choco Gems"]},

        # Mousse Cake Collection
        {"name": "Pink Birthday Cookie Cake (1lb)", "name_th": "พิงค์ เบิร์ธเดย์", "price": 590, "category": "Mousse Cake", "image": IMG["Mousse Cake"], "favorite": True},
        {"name": "Breakfast Confetti Birthday Sized Cake", "name_th": "เบรกฟาสต์คอนเฟ็ตตี้", "price": 590, "category": "Mousse Cake", "image": IMG["Mousse Cake"]},
        {"name": "Red Velvet Cookie Cake", "name_th": "เรดเวลเว็ท", "price": 160, "category": "Mousse Cake", "image": IMG["Mousse Cake"]},
        {"name": "Chocolate Pudding Birthday", "name_th": "ช็อกโกแลตพุดดิ้ง", "price": 1600, "category": "Mousse Cake", "image": IMG["Mousse Cake"]},

        # Soft Cookies
        {"name": "Biscoff Mochi Cookie", "name_th": "บิสคอฟโมจิ", "price": 160, "category": "Soft Cookies", "image": IMG["Soft Cookies"], "favorite": True},
        {"name": "Sexy Back Cookie", "name_th": "เซ็กซี่แบ็กคุกกี้", "price": 95, "category": "Soft Cookies", "image": IMG["Soft Cookies"], "favorite": True},
        {"name": "Cookie Dough Cookie", "name_th": "คุกกี้โด", "price": 160, "category": "Soft Cookies", "image": IMG["Soft Cookies"]},
        {"name": "Double Trouble Dark Chocolate", "name_th": "ดับเบิ้ลทรับเบิ้ล", "price": 95, "category": "Soft Cookies", "image": IMG["Soft Cookies"]},
        {"name": "The Marching Ladies Cookie", "name_th": "มาร์ชิ่งเลดี้ส์", "price": 95, "category": "Soft Cookies", "image": IMG["Soft Cookies"]},
        {"name": "Breakfast Confetti Cookie", "name_th": "เบรกฟาสต์คอนเฟ็ตตี้", "price": 160, "category": "Soft Cookies", "image": IMG["Soft Cookies"]},
        {"name": "Firecracker Candle (เทียนพลุ)", "name_th": "เทียนพลุ", "price": 100, "category": "Soft Cookies", "image": IMG["Soft Cookies"]},
        {"name": "Box of 9pcs Bae Brownie", "name_th": "แบบราวนี่ 9 ชิ้น", "price": 380, "category": "Soft Cookies", "image": IMG["Soft Cookies"]},
        {"name": "CORNFLAKE MARSHMALLOW", "name_th": "คอร์นเฟลกมาร์ชเมลโลว์", "price": 95, "category": "Soft Cookies", "image": IMG["Soft Cookies"]},
        {"name": "Pink Birthday Cookies", "name_th": "พิงค์เบิร์ธเดย์คุกกี้", "price": 160, "category": "Soft Cookies", "image": IMG["Soft Cookies"]},

        # Dubai Chocolate
        {"name": "Dubai Matcha Strawberry Mochi", "name_th": "ดูไบมัทฉะสตรอเบอร์รี่", "price": 399, "category": "Dubai Chocolate", "image": IMG["Dubai Chocolate"], "favorite": True},
        {"name": "Box of 9pcs Red Velvet Cream Cheese", "name_th": "เรดเวลเว็ทครีมชีส", "price": 380, "category": "Dubai Chocolate", "image": IMG["Dubai Chocolate"]},
        {"name": "Dubai Chewy Cookies", "name_th": "ดูไบชิววี่", "price": 299, "category": "Dubai Chocolate", "image": IMG["Dubai Chocolate"]},
        {"name": "Dubai Classic Bar", "name_th": "ดูไบคลาสสิก", "price": 450, "category": "Dubai Chocolate", "image": IMG["Dubai Chocolate"]},

        # Cookie Cake
        {"name": "Mini Strawberry Shortcake", "name_th": "มินิสตรอเบอร์รี่", "price": 690, "category": "Cookie Cake", "image": IMG["Cookie Cake"]},
        {"name": "Birthday Cookie Cake (1lb)", "name_th": "เบิร์ธเดย์คุกกี้เค้ก", "price": 590, "category": "Cookie Cake", "image": IMG["Cookie Cake"]},
        {"name": "Chocolate Fudge Cookie Cake", "name_th": "ช็อกโกแลตฟัดจ์", "price": 690, "category": "Cookie Cake", "image": IMG["Cookie Cake"]},
    ]

    products = []
    for p in products_data:
        products.append(Product(
            name=p["name"],
            name_th=p.get("name_th"),
            price=p["price"],
            category_id=cat_map[p["category"]],
            image_url=p["image"],
            is_favorite=p.get("favorite", False),
        ))
    await db.products.insert_many([p.model_dump() for p in products])

    # Customers
    customers_data = [
        {"name": "bb bb", "phone": None, "color": "#EF4444"},
        {"name": "Sunisa Chaiprom", "phone": "020128575", "color": "#94A3B8"},
        {"name": "fisa ml", "phone": None, "last_visit": "2026-03-02", "color": "#10B981"},
        {"name": "CEVA AIR OCEAN (THAILAND) CO., LTD.", "phone": None, "color": "#94A3B8"},
        {"name": "CHAGEE (THAILAND) COMPANY LIMITED", "phone": None, "color": "#94A3B8"},
        {"name": "DUCKKING บริษัท ดั๊กคิง จำกัด", "phone": "0385881178", "color": "#F59E0B"},
        {"name": "fv dff", "phone": "122522", "color": "#10B981"},
    ]
    customers = [Customer(**c) for c in customers_data]
    await db.customers.insert_many([c.model_dump() for c in customers])

    # Seed some delivered orders (Grab) for Order Hub demo
    now = datetime.now(timezone.utc)
    sample_items = [OrderItem(product_id=products[0].id, name=products[0].name, price=products[0].price, qty=1)]
    demo_orders = [
        {"order_number": "GF-989", "total": 290.0, "status": "completed", "source": "delivery", "delivery_provider": "Grab", "delivery_status": "DELIVERING", "time": "14:34"},
        {"order_number": "GF-643", "total": 630.0, "status": "completed", "source": "delivery", "delivery_provider": "Grab", "delivery_status": "DELIVERING", "time": "14:16"},
        {"order_number": "GF-383", "total": 590.0, "status": "completed", "source": "delivery", "delivery_provider": "Grab", "delivery_status": "DELIVERED", "time": "14:21"},
        {"order_number": "GF-034", "total": 350.0, "status": "completed", "source": "delivery", "delivery_provider": "Grab", "delivery_status": "DELIVERED", "time": "12:32"},
        {"order_number": "GF-247", "total": 450.0, "status": "completed", "source": "delivery", "delivery_provider": "Grab", "delivery_status": "DELIVERED", "time": "12:08"},
    ]
    for d in demo_orders:
        o = Order(
            order_number=d["order_number"],
            items=sample_items,
            subtotal=d["total"],
            total=d["total"],
            paid_amount=d["total"],
            status=d["status"],
            source=d["source"],
            payment_method="PromptPay",
            delivery_provider=d.get("delivery_provider"),
            delivery_status=d.get("delivery_status"),
            created_time=d["time"],
            created_at=now.isoformat(),
        )
        await db.orders.insert_one(o.model_dump())

    return {
        "categories": len(cat_objs),
        "products": len(products),
        "customers": len(customers),
        "orders": len(demo_orders),
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
