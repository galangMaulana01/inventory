from fastapi import FastAPI, HTTPException, UploadFile, Form, Query
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional, List, Any
from datetime import datetime, timezone
from pathlib import Path
import os
import json
import hashlib
import time
import secrets

try:
    from pymongo import MongoClient
    from pymongo.errors import DuplicateKeyError
    from bson import ObjectId
except Exception:
    MongoClient = None
    DuplicateKeyError = Exception
    ObjectId = None

# ============================================================
# CONFIG
# ============================================================
MONGO_URI = os.getenv("MONGODB_URI")
CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME")
CLOUDINARY_API_KEY = os.getenv("CLOUDINARY_API_KEY")
CLOUDINARY_API_SECRET = os.getenv("CLOUDINARY_API_SECRET")
USE_CLOUDINARY = all([CLOUDINARY_CLOUD_NAME, CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET])

# Login v1 — kredensial statis di server (env var kalau ada, fallback default).
# Belum menyentuh database sama sekali; ganti nanti kalau sudah ada sistem user.
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "stokku123")
ACTIVE_TOKENS: set = set()

DATA_DIR = Path(os.getenv("STOKKU_DATA_DIR", "/tmp/stokku_data"))
UPLOAD_DIR = Path(os.getenv("STOKKU_UPLOAD_DIR", "/tmp/stokku_uploads"))

mongo_client = None
db = None
USE_MONGO = False

if MONGO_URI and MongoClient:
    try:
        mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
        db = mongo_client.get_default_database()
        mongo_client.admin.command("ping")
        USE_MONGO = True
    except Exception as exc:
        print(f"MongoDB unavailable: {exc}")
        USE_MONGO = False
        db = None

if not USE_MONGO:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Stokku Inventory API", version="2.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================
# MODELS
# ============================================================
class SizeInput(BaseModel):
    size: str = Field(min_length=1)
    warehouse_qty: int = Field(ge=0)
    hpp: int = Field(ge=0)
    normal_price: int = Field(ge=0)
    minimum_price: int = Field(ge=0)

class ColorInput(BaseModel):
    color: str = Field(min_length=1)
    color_hex: Optional[str] = "#cccccc"
    sizes: List[SizeInput] = Field(min_length=1)

class ProductInput(BaseModel):
    name: str = Field(min_length=1)
    model: str = Field(min_length=1)
    colors: List[ColorInput] = Field(min_length=1)

class VariantInput(BaseModel):
    product_id: str
    color: str = Field(min_length=1)
    color_hex: Optional[str] = None
    size: str = Field(min_length=1)
    warehouse_qty: int = Field(ge=0)
    hpp: int = Field(ge=0)
    normal_price: int = Field(ge=0)
    minimum_price: int = Field(ge=0)

class VariantUpdate(BaseModel):
    product_id: Optional[str] = None
    color: Optional[str] = None
    color_hex: Optional[str] = None
    size: Optional[str] = None
    warehouse_qty: Optional[int] = Field(default=None, ge=0)
    hpp: Optional[int] = Field(default=None, ge=0)
    normal_price: Optional[int] = Field(default=None, ge=0)
    minimum_price: Optional[int] = Field(default=None, ge=0)

class TransactionInput(BaseModel):
    variant_id: str
    qty: int = Field(gt=0)
    unit_price: int = Field(gt=0)
    payment_method: Optional[str] = "cash"

class BatchItem(BaseModel):
    variant_id: str
    qty: int = Field(gt=0)
    unit_price: int = Field(gt=0)

class BatchTransactionInput(BaseModel):
    items: List[BatchItem] = Field(min_length=1)
    payment_method: Optional[str] = "cash"

class TransferInput(BaseModel):
    variant_id: str
    qty: int = Field(gt=0)

class LoginInput(BaseModel):
    username: str = Field(min_length=1)
    password: str = Field(min_length=1)

class LogoutInput(BaseModel):
    token: Optional[str] = None

# ============================================================
# SERIALIZATION / HELPERS
# ============================================================
def normalize_color(value: str) -> str:
    return str(value or "").strip().lower()

def now_utc() -> datetime:
    return datetime.now(timezone.utc)

def parse_datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        return datetime.fromisoformat(text)
    except Exception:
        return None

def json_safe(value: Any) -> Any:
    """Recursively remove Mongo ObjectId/datetime so FastAPI can serialize safely."""
    if ObjectId is not None and isinstance(value, ObjectId):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    return value

def oid(value: str):
    if ObjectId is None or not ObjectId.is_valid(str(value)):
        raise HTTPException(status_code=400, detail="ID tidak valid")
    return ObjectId(str(value))

def load_json(name: str, default):
    path = DATA_DIR / f"{name}.json"
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default

def save_json(name: str, data):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    path = DATA_DIR / f"{name}.json"
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=json_safe)

def get_products():
    if USE_MONGO:
        return list(db.products.find({"deleted": {"$ne": True}}))
    return load_json("products", {"items": []}).get("items", [])

def get_variants():
    if USE_MONGO:
        return list(db.variants.find({"deleted": {"$ne": True}}))
    return load_json("variants", {"items": []}).get("items", [])

def get_colors():
    if USE_MONGO:
        return list(db.colors.find({"deleted": {"$ne": True}}))
    return load_json("colors", {"items": []}).get("items", [])

def get_transactions():
    if USE_MONGO:
        return list(db.transactions.find().sort("created_at", -1).limit(200))
    return load_json("transactions", {"items": []}).get("items", [])

def product_map():
    return {str(p.get("_id", p.get("id"))): p for p in get_products()}

def enrich_variants(variants):
    products = product_map()
    colors = {str(c.get("_id", c.get("id"))): c for c in get_colors()}
    result = []
    for v in variants:
        item = dict(v)
        product_id = str(v.get("product_id", ""))
        color_id = str(v.get("color_id", ""))
        p = products.get(product_id, {})
        c = colors.get(color_id, {})
        item["id"] = str(v.get("_id", v.get("id", "")))
        item["product_id"] = product_id
        item["color_id"] = color_id
        item["product_name"] = v.get("product_name") or p.get("name", "")
        item["model"] = v.get("model") or p.get("model", "")
        item["image_url"] = v.get("image_url") or p.get("image_url")
        item["color"] = v.get("color") or c.get("color", "")
        item["color_hex"] = v.get("color_hex") or c.get("color_hex") or "#cccccc"
        item["size"] = str(v.get("size", ""))
        item["warehouse_qty"] = int(v.get("warehouse_qty", 0) or 0)
        item["sale_qty"] = int(v.get("sale_qty", 0) or 0)
        item["hpp"] = int(v.get("hpp", 0) or 0)
        item["normal_price"] = int(v.get("normal_price", 0) or 0)
        item["minimum_price"] = int(v.get("minimum_price", 0) or 0)
        result.append(item)
    return json_safe(result)

def find_variant(variant_id: str):
    if USE_MONGO:
        return db.variants.find_one({"_id": oid(variant_id), "deleted": {"$ne": True}})
    return next((v for v in get_variants() if str(v.get("_id", v.get("id"))) == str(variant_id)), None)

def invoice_no():
    return f"INV-{now_utc().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2).upper()}"

def validate_price(variant, unit_price):
    minimum = int(variant.get("minimum_price", 0))
    normal = int(variant.get("normal_price", 0))
    if minimum > normal:
        raise HTTPException(status_code=500, detail="Konfigurasi harga varian tidak valid")
    if not minimum <= unit_price <= normal:
        raise HTTPException(status_code=400, detail=f"Harga harus antara {minimum} - {normal}")

def validate_payment_method(payment_method: str) -> str:
    method = str(payment_method or "cash").strip().lower()
    if method not in {"cash", "bank", "wallet"}:
        raise HTTPException(status_code=400, detail="Metode pembayaran harus cash, bank, atau wallet")
    return method

def add_stock_move(variant_id, from_type, to_type, qty, notes=""):
    doc = {
        "variant_id": oid(variant_id) if USE_MONGO else str(variant_id),
        "from_type": from_type,
        "to_type": to_type,
        "qty": int(qty),
        "notes": notes,
        "created_at": now_utc(),
    }
    if USE_MONGO:
        db.stock_moves.insert_one(doc)
    else:
        data = load_json("stock_moves", {"items": []})
        data["items"].append(json_safe(doc))
        save_json("stock_moves", data)

# ============================================================
# DB INDEXES
# ============================================================
if USE_MONGO:
    try:
        # create_index is idempotent when the name/spec match what's already
        # there, so no need to drop existing indexes on every cold start.
        db.variants.create_index(
            [("product_id", 1), ("color_lower", 1), ("size", 1)],
            unique=True,
            name="variant_product_color_size_unique",
        )
        db.transactions.create_index([("created_at", -1)], name="transactions_created_at")
        # Unique index on colors for active (non-deleted) colors only
        db.colors.create_index(
            [("product_id", 1), ("color_lower", 1)],
            unique=True,
            partialFilterExpression={"deleted": {"$ne": True}},
            name="color_product_lower_unique_active"
        )
        db.stock_moves.create_index([("created_at", -1)], name="stock_moves_created_at")
    except Exception as exc:
        print(f"Index warning: {exc}")

# ============================================================
# AUTH (v1 — kredensial statis di server, belum ke database)
# ============================================================
@app.post("/api/login")
async def login(payload: LoginInput):
    if payload.username.strip() != ADMIN_USERNAME or payload.password != ADMIN_PASSWORD:
        raise HTTPException(status_code=401, detail="Username atau sandi salah")
    token = secrets.token_hex(24)
    ACTIVE_TOKENS.add(token)
    return {"token": token, "username": ADMIN_USERNAME, "message": "Login berhasil"}

@app.post("/api/logout")
async def logout(payload: LogoutInput):
    if payload.token:
        ACTIVE_TOKENS.discard(payload.token)
    return {"message": "Berhasil keluar"}

# ============================================================
# HEALTH / DASHBOARD
# ============================================================
@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "database": "mongodb" if USE_MONGO else "json-fallback",
        "cloudinary": "enabled" if USE_CLOUDINARY else "disabled",
    }

@app.get("/api/dashboard")
async def dashboard():
    products = get_products()
    variants = get_variants()
    transactions = get_transactions()
    today = now_utc().date()

    today_trans = []
    for t in transactions:
        dt = parse_datetime(t.get("created_at"))
        if dt and dt.date() == today:
            today_trans.append(t)

    warehouse_qty = sum(int(v.get("warehouse_qty", 0) or 0) for v in variants)
    sale_qty = sum(int(v.get("sale_qty", 0) or 0) for v in variants)
    sold_qty = sum(int(t.get("qty", 0) or 0) for t in today_trans)
    revenue = sum(int(t.get("qty", 0) or 0) * int(t.get("unit_price", 0) or 0) for t in today_trans)
    capital = sum(int(v.get("hpp", 0) or 0) * int(v.get("warehouse_qty", 0) or 0) for v in variants)

    hpp_by_variant = {str(v.get("_id", v.get("id"))): int(v.get("hpp", 0) or 0) for v in variants}
    profit = sum(
        int(t.get("qty", 0) or 0)
        * (int(t.get("unit_price", 0) or 0) - hpp_by_variant.get(str(t.get("variant_id")), int(t.get("hpp", 0) or 0)))
        for t in today_trans
    )

    enriched_transactions = []
    products_by_id = product_map()
    for t in transactions:
        item = dict(t)
        item["id"] = str(t.get("_id", t.get("id", "")))
        item["variant_id"] = str(t.get("variant_id", ""))
        p = products_by_id.get(str(t.get("product_id")), {})
        item["product_name"] = t.get("product_name") or p.get("name", "")
        item["model"] = t.get("model") or p.get("model", "")
        item["image_url"] = t.get("image_url") or p.get("image_url")
        item["total"] = int(t.get("total_price", t.get("total", 0)) or 0)
        enriched_transactions.append(item)

    recent = enriched_transactions[:5]
    product_sales = {}
    for t in today_trans:
        pid = str(t.get("product_id", ""))
        entry = product_sales.setdefault(pid, {"product_id": pid, "sold_qty": 0, "revenue": 0})
        entry["sold_qty"] += int(t.get("qty", 0) or 0)
        entry["revenue"] += int(t.get("qty", 0) or 0) * int(t.get("unit_price", 0) or 0)

    top = []
    for entry in sorted(product_sales.values(), key=lambda x: (-x["sold_qty"], -x["revenue"])):
        p = products_by_id.get(entry["product_id"], {})
        top.append({
            **entry,
            "product_name": p.get("name", ""),
            "model": p.get("model", ""),
            "image_url": p.get("image_url"),
        })

    return json_safe({
        "total_products": len(products),
        "total_variants": len(variants),
        "warehouse_qty": warehouse_qty,
        "sale_qty": sale_qty,
        "sold_qty_today": sold_qty,
        "revenue_today": revenue,
        "profit_today": profit,
        "transactions_today": len(today_trans),
        "transaction_count_today": len(today_trans),
        "total_capital": capital,
        "capital": capital,
        "recent_transactions": recent,
        "top_products": top,
    })

# ============================================================
# PRODUCTS
# ============================================================
@app.get("/api/products")
async def list_products():
    return json_safe([
        {
            "_id": str(p.get("_id", p.get("id"))),
            "id": str(p.get("_id", p.get("id"))),
            "name": p.get("name", ""),
            "model": p.get("model", ""),
            "image_url": p.get("image_url"),
        }
        for p in get_products()
    ])

@app.post("/api/products")
async def create_product(product: ProductInput):
    for color in product.colors:
        if any(s.minimum_price > s.normal_price for s in color.sizes):
            raise HTTPException(status_code=400, detail=f"Harga minimum warna {color.color} melebihi harga normal")
        if len({normalize_color(s.size) for s in color.sizes}) != len(color.sizes):
            raise HTTPException(status_code=400, detail=f"Duplikat size pada warna {color.color}")
    if len({normalize_color(c.color) for c in product.colors}) != len(product.colors):
        raise HTTPException(status_code=400, detail="Duplikat warna dalam produk")

    created = now_utc()

    if USE_MONGO:
        product_doc = {
            "_id": ObjectId(),
            "name": product.name.strip(),
            "model": product.model.strip(),
            "image_url": None,
            "created_at": created,
            "deleted": False,
        }
        db.products.insert_one(product_doc)
        try:
            for color in product.colors:
                color_doc = {
                    "_id": ObjectId(),
                    "product_id": product_doc["_id"],
                    "color": color.color.strip(),
                    "color_lower": normalize_color(color.color),
                    "color_hex": color.color_hex or "#cccccc",
                    "created_at": created,
                    "deleted": False,
                }
                db.colors.insert_one(color_doc)
                for size in color.sizes:
                    db.variants.insert_one({
                        "_id": ObjectId(),
                        "product_id": product_doc["_id"],
                        "color_id": color_doc["_id"],
                        "color": color.color.strip(),
                        "color_lower": normalize_color(color.color),
                        "color_hex": color.color_hex or "#cccccc",
                        "size": size.size.strip(),
                        "warehouse_qty": size.warehouse_qty,
                        "sale_qty": 0,
                        "hpp": size.hpp,
                        "normal_price": size.normal_price,
                        "minimum_price": size.minimum_price,
                        "created_at": created,
                        "deleted": False,
                    })
        except DuplicateKeyError:
            db.products.delete_one({"_id": product_doc["_id"]})
            db.colors.delete_many({"product_id": product_doc["_id"]})
            db.variants.delete_many({"product_id": product_doc["_id"]})
            raise HTTPException(status_code=400, detail="Kombinasi warna + size duplikat")
        return {"id": str(product_doc["_id"]), "message": "Produk berhasil dibuat"}

    products = load_json("products", {"items": []})
    variants = load_json("variants", {"items": []})
    colors = load_json("colors", {"items": []})
    product_id = secrets.token_hex(12)
    pdoc = {"id": product_id, "name": product.name.strip(), "model": product.model.strip(), "image_url": None, "created_at": created.isoformat(), "deleted": False}
    products["items"].append(pdoc)
    for color in product.colors:
        color_id = secrets.token_hex(12)
        cdoc = {"id": color_id, "product_id": product_id, "color": color.color.strip(), "color_lower": normalize_color(color.color), "color_hex": color.color_hex or "#cccccc", "created_at": created.isoformat(), "deleted": False}
        colors["items"].append(cdoc)
        for size in color.sizes:
            variants["items"].append({"id": secrets.token_hex(12), "product_id": product_id, "color_id": color_id, "color": color.color.strip(), "color_lower": normalize_color(color.color), "color_hex": color.color_hex or "#cccccc", "size": size.size.strip(), "warehouse_qty": size.warehouse_qty, "sale_qty": 0, "hpp": size.hpp, "normal_price": size.normal_price, "minimum_price": size.minimum_price, "created_at": created.isoformat(), "deleted": False})
    save_json("products", products)
    save_json("colors", colors)
    save_json("variants", variants)
    return {"id": product_id, "message": "Produk berhasil dibuat"}

@app.get("/api/products/{product_id}")
async def get_product(product_id: str):
    products = product_map()
    product = products.get(product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Produk tidak ditemukan")
    variants = [v for v in get_variants() if str(v.get("product_id")) == product_id]
    return json_safe({
        "id": product_id,
        "_id": product_id,
        "name": product.get("name", ""),
        "model": product.get("model", ""),
        "image_url": product.get("image_url"),
        "variants": enrich_variants(variants),
    })

@app.put("/api/products/{product_id}")
async def update_product(product_id: str, name: str = Form(...), model: str = Form(...)):
    if not str(name).strip() or not str(model).strip():
        raise HTTPException(status_code=400, detail="Nama dan seri wajib diisi")
    if USE_MONGO:
        result = db.products.update_one({"_id": oid(product_id), "deleted": {"$ne": True}}, {"$set": {"name": name.strip(), "model": model.strip()}})
        if result.matched_count == 0:
            raise HTTPException(status_code=404, detail="Produk tidak ditemukan")
    else:
        data = load_json("products", {"items": []})
        found = next((p for p in data["items"] if str(p.get("id")) == product_id and not p.get("deleted")), None)
        if not found:
            raise HTTPException(status_code=404, detail="Produk tidak ditemukan")
        found.update({"name": name.strip(), "model": model.strip()})
        save_json("products", data)
    return {"message": "Produk diperbarui"}

@app.delete("/api/products/{product_id}")
async def delete_product(product_id: str):
    if USE_MONGO:
        result = db.products.update_one({"_id": oid(product_id)}, {"$set": {"deleted": True}})
        if result.matched_count == 0:
            raise HTTPException(status_code=404, detail="Produk tidak ditemukan")
        db.variants.update_many({"product_id": oid(product_id)}, {"$set": {"deleted": True}})
        db.colors.update_many({"product_id": oid(product_id)}, {"$set": {"deleted": True}})
    else:
        products = load_json("products", {"items": []})
        found = next((p for p in products["items"] if str(p.get("id")) == product_id), None)
        if not found:
            raise HTTPException(status_code=404, detail="Produk tidak ditemukan")
        found["deleted"] = True
        for name in ("variants", "colors"):
            data = load_json(name, {"items": []})
            for item in data["items"]:
                if str(item.get("product_id")) == product_id:
                    item["deleted"] = True
            save_json(name, data)
        save_json("products", products)
    return {"message": "Produk dihapus"}

# ============================================================
# IMAGE
# ============================================================
async def upload_image_to_cloudinary(content: bytes, filename: str) -> str:
    if not USE_CLOUDINARY:
        digest = hashlib.sha256(content).hexdigest()[:32]
        path = UPLOAD_DIR / f"{digest}.bin"
        path.write_bytes(content)
        return f"/uploads/{path.name}"

    import requests
    import hashlib as _hashlib
    timestamp = int(time.time())
    signature_base = f"timestamp={timestamp}{CLOUDINARY_API_SECRET}"
    signature = _hashlib.sha1(signature_base.encode("utf-8")).hexdigest()
    url = f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD_NAME}/image/upload"
    files = {"file": (filename, content)}
    data = {"api_key": CLOUDINARY_API_KEY, "timestamp": timestamp, "signature": signature}
    response = requests.post(url, files=files, data=data, timeout=30)
    if response.status_code != 200:
        raise HTTPException(status_code=502, detail="Upload gambar ke Cloudinary gagal")
    return response.json()["secure_url"]

@app.post("/api/products/{product_id}/image")
async def upload_product_image(product_id: str, image: UploadFile):
    if image.content_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise HTTPException(status_code=400, detail="Format harus JPEG/PNG/WebP")
    content = await image.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Ukuran maksimal 5MB")

    if not find_product(product_id):
        raise HTTPException(status_code=404, detail="Produk tidak ditemukan")

    url = await upload_image_to_cloudinary(content, image.filename or "product.jpg")
    if USE_MONGO:
        db.products.update_one({"_id": oid(product_id)}, {"$set": {"image_url": url}})
    else:
        data = load_json("products", {"items": []})
        found = next((p for p in data["items"] if str(p.get("id")) == product_id), None)
        if found:
            found["image_url"] = url
            save_json("products", data)
    return {"image_url": url, "message": "Gambar utama produk berhasil disimpan"}

def find_product(product_id: str):
    if USE_MONGO:
        return db.products.find_one({"_id": oid(product_id), "deleted": {"$ne": True}})
    return next((p for p in get_products() if str(p.get("id")) == product_id), None)

@app.get("/uploads/{filename}")
async def serve_upload(filename: str):
    safe_name = Path(filename).name
    path = UPLOAD_DIR / safe_name
    if path.exists():
        return FileResponse(path)
    raise HTTPException(status_code=404, detail="File tidak ditemukan")

# ============================================================
# VARIANTS / STOCK
# ============================================================
@app.get("/api/variants")
async def list_variants(product_id: Optional[str] = Query(None)):
    variants = get_variants()
    if product_id:
        variants = [v for v in variants if str(v.get("product_id")) == product_id]
    return enrich_variants(variants)

@app.post("/api/variants")
async def create_variant(variant: VariantInput):
    if variant.minimum_price > variant.normal_price:
        raise HTTPException(status_code=400, detail="Harga minimum tidak boleh melebihi harga normal")
    product = find_product(variant.product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Produk tidak ditemukan")

    color_norm = normalize_color(variant.color)
    size = variant.size.strip()

    if USE_MONGO:
        product_oid = oid(variant.product_id)
        # Compare size case-insensitively as well, so "40" and "40 " / "m" and "M" cannot create duplicates.
        existing_candidates = db.variants.find({
            "product_id": product_oid,
            "color_lower": color_norm,
            "deleted": {"$ne": True},
        })
        if any(normalize_color(v.get("size")) == normalize_color(size) for v in existing_candidates):
            raise HTTPException(status_code=400, detail=f"Duplikat: warna {variant.color} dengan size {size} sudah ada")
        color = db.colors.find_one({"product_id": product_oid, "color_lower": color_norm, "deleted": {"$ne": True}})
        if not color:
            # Simple approach: find any existing doc (including deleted), restore it
            # Otherwise create new. No upsert, no index conflicts.
            existing = db.colors.find_one({"product_id": product_oid, "color_lower": color_norm})
            if existing:
                # Restore if deleted, update color/hex if needed
                db.colors.update_one(
                    {"_id": existing["_id"]},
                    {"$set": {"deleted": False, "color": variant.color.strip(), "color_hex": variant.color_hex or "#cccccc"}}
                )
                color = db.colors.find_one({"_id": existing["_id"]})
            else:
                # Create new color
                color_doc = {
                    "_id": ObjectId(),
                    "product_id": product_oid,
                    "color": variant.color.strip(),
                    "color_lower": color_norm,
                    "color_hex": variant.color_hex or "#cccccc",
                    "created_at": now_utc(),
                    "deleted": False
                }
                try:
                    db.colors.insert_one(color_doc)
                    color = color_doc
                except DuplicateKeyError:
                    # Race: another request created it
                    color = db.colors.find_one({"product_id": product_oid, "color_lower": color_norm, "deleted": {"$ne": True}})
                    if not color:
                        raise HTTPException(status_code=500, detail="Gagal membuat/menemukan warna")
        doc = {
            "_id": ObjectId(),
            "product_id": product_oid,
            "color_id": color["_id"],
            "color": color["color"],
            "color_lower": color_norm,
            "color_hex": color.get("color_hex") or "#cccccc",
            "size": size,
            "warehouse_qty": variant.warehouse_qty,
            "sale_qty": 0,
            "hpp": variant.hpp,
            "normal_price": variant.normal_price,
            "minimum_price": variant.minimum_price,
            "created_at": now_utc(),
            "deleted": False,
        }
        try:
            db.variants.insert_one(doc)
        except DuplicateKeyError:
            raise HTTPException(status_code=400, detail="Kombinasi warna + size sudah ada")
        return {"id": str(doc["_id"]), "message": "Varian berhasil ditambahkan"}
    data = load_json("variants", {"items": []})
    for v in data["items"]:
        if str(v.get("product_id")) == variant.product_id and normalize_color(v.get("color")) == color_norm and str(v.get("size")).strip().lower() == size.lower() and not v.get("deleted"):
            raise HTTPException(status_code=400, detail="Kombinasi warna + size sudah ada")
    color_data = load_json("colors", {"items": []})
    color = next((c for c in color_data["items"] if str(c.get("product_id")) == variant.product_id and normalize_color(c.get("color")) == color_norm and not c.get("deleted")), None)
    if not color:
        color = {"id": secrets.token_hex(12), "product_id": variant.product_id, "color": variant.color.strip(), "color_lower": color_norm, "color_hex": variant.color_hex or "#cccccc", "created_at": now_utc().isoformat(), "deleted": False}
        color_data["items"].append(color)
        save_json("colors", color_data)
    doc = {"id": secrets.token_hex(12), "product_id": variant.product_id, "color_id": color["id"], "color": color["color"], "color_lower": color_norm, "color_hex": color["color_hex"], "size": size, "warehouse_qty": variant.warehouse_qty, "sale_qty": 0, "hpp": variant.hpp, "normal_price": variant.normal_price, "minimum_price": variant.minimum_price, "created_at": now_utc().isoformat(), "deleted": False}
    data["items"].append(doc)
    save_json("variants", data)
    return {"id": doc["id"], "message": "Varian berhasil ditambahkan"}

@app.put("/api/variants/{variant_id}")
async def update_variant(variant_id: str, variant: VariantUpdate):
    existing = find_variant(variant_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Varian tidak ditemukan")

    update = {}
    if variant.product_id is not None:
        if not find_product(variant.product_id):
            raise HTTPException(status_code=404, detail="Produk tujuan tidak ditemukan")
        update["product_id"] = oid(variant.product_id) if USE_MONGO else variant.product_id
    if variant.color is not None:
        update["color"] = variant.color.strip()
        update["color_lower"] = normalize_color(variant.color)
    if variant.color_hex is not None:
        update["color_hex"] = variant.color_hex
    if variant.size is not None:
        update["size"] = variant.size.strip()
    for field in ("warehouse_qty", "hpp", "normal_price", "minimum_price"):
        value = getattr(variant, field)
        if value is not None:
            update[field] = value

    final_min = int(update.get("minimum_price", existing.get("minimum_price", 0)))
    final_normal = int(update.get("normal_price", existing.get("normal_price", 0)))
    if final_min > final_normal:
        raise HTTPException(status_code=400, detail="Harga minimum tidak boleh melebihi harga normal")

    if USE_MONGO:
        target_product_id = update.get("product_id", existing.get("product_id"))
        target_color = normalize_color(str(update.get("color", existing.get("color", ""))))
        target_size = normalize_color(str(update.get("size", existing.get("size", ""))))
        duplicate_candidates = db.variants.find({
            "product_id": target_product_id,
            "color_lower": target_color,
            "deleted": {"$ne": True},
        })
        for candidate in duplicate_candidates:
            if str(candidate.get("_id")) != str(existing.get("_id")) and normalize_color(candidate.get("size")) == target_size:
                raise HTTPException(status_code=400, detail="Kombinasi warna + size sudah ada")
        if "product_id" in update:
            # Existing color must be recreated/looked up if product changes.
            color_name = str(update.get("color", existing.get("color", "")))
            color_norm = normalize_color(color_name)
            color = db.colors.find_one({"product_id": update["product_id"], "color_lower": color_norm, "deleted": {"$ne": True}})
            if not color:
                color = {"_id": ObjectId(), "product_id": update["product_id"], "color": color_name, "color_lower": color_norm, "color_hex": update.get("color_hex", existing.get("color_hex", "#cccccc")), "created_at": now_utc(), "deleted": False}
                db.colors.insert_one(color)
            update["color_id"] = color["_id"]
        elif "color" in update:
            color_norm = normalize_color(update["color"])
            color = db.colors.find_one({"product_id": existing["product_id"], "color_lower": color_norm, "deleted": {"$ne": True}})
            if color:
                update["color_id"] = color["_id"]
        try:
            db.variants.update_one({"_id": oid(variant_id)}, {"$set": update})
        except DuplicateKeyError:
            raise HTTPException(status_code=400, detail="Kombinasi warna + size sudah ada")
    else:
        data = load_json("variants", {"items": []})
        found = next((v for v in data["items"] if str(v.get("id")) == variant_id and not v.get("deleted")), None)
        if not found:
            raise HTTPException(status_code=404, detail="Varian tidak ditemukan")
        found.update(update)
        save_json("variants", data)
    return {"message": "Varian diperbarui"}

@app.delete("/api/variants/{variant_id}")
async def delete_variant(variant_id: str):
    existing = find_variant(variant_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Varian tidak ditemukan")
    if USE_MONGO:
        db.variants.update_one({"_id": oid(variant_id)}, {"$set": {"deleted": True}})
    else:
        data = load_json("variants", {"items": []})
        found = next(v for v in data["items"] if str(v.get("id")) == variant_id)
        found["deleted"] = True
        save_json("variants", data)
    return {"message": "Varian dihapus"}

# ============================================================
# TRANSFER
# ============================================================
@app.post("/api/transfers")
async def transfer_stock(transfer: TransferInput):
    variant = find_variant(transfer.variant_id)
    if not variant:
        raise HTTPException(status_code=404, detail="Varian tidak ditemukan")

    if USE_MONGO:
        result = db.variants.update_one(
            {"_id": oid(transfer.variant_id), "deleted": {"$ne": True}, "warehouse_qty": {"$gte": transfer.qty}},
            {"$inc": {"warehouse_qty": -transfer.qty, "sale_qty": transfer.qty}},
        )
        if result.modified_count != 1:
            raise HTTPException(status_code=400, detail="Stok gudang tidak mencukupi")
    else:
        data = load_json("variants", {"items": []})
        found = next(v for v in data["items"] if str(v.get("id")) == transfer.variant_id and not v.get("deleted"))
        if int(found.get("warehouse_qty", 0)) < transfer.qty:
            raise HTTPException(status_code=400, detail=f"Stok gudang hanya {found.get('warehouse_qty', 0)} unit")
        found["warehouse_qty"] = int(found.get("warehouse_qty", 0)) - transfer.qty
        found["sale_qty"] = int(found.get("sale_qty", 0)) + transfer.qty
        save_json("variants", data)

    add_stock_move(transfer.variant_id, "warehouse", "sale", transfer.qty, "Transfer manual")
    return {"message": f"Stok berhasil dipindahkan ({transfer.qty} unit)"}

# ============================================================
# TRANSACTIONS
# ============================================================
def create_one_transaction(item: BatchItem, payment_method: str):
    payment_method = validate_payment_method(payment_method)
    variant = find_variant(item.variant_id)
    if not variant:
        raise HTTPException(status_code=404, detail=f"Varian {item.variant_id} tidak ditemukan")
    if int(variant.get("sale_qty", 0)) < item.qty:
        raise HTTPException(status_code=400, detail=f"Stok jual hanya {variant.get('sale_qty', 0)} unit")
    validate_price(variant, item.unit_price)

    products = product_map()
    product = products.get(str(variant.get("product_id")), {})
    trans_id = ObjectId() if USE_MONGO else secrets.token_hex(12)
    created = now_utc()
    doc = {
        "_id": trans_id if USE_MONGO else None,
        "id": str(trans_id),
        "invoice_no": invoice_no(),
        "variant_id": oid(item.variant_id) if USE_MONGO else item.variant_id,
        "product_id": variant.get("product_id"),
        "color": variant.get("color", ""),
        "size": variant.get("size", ""),
        "qty": item.qty,
        "unit_price": item.unit_price,
        "total_price": item.qty * item.unit_price,
        "total": item.qty * item.unit_price,
        "hpp": int(variant.get("hpp", 0)),
        "profit": item.qty * (item.unit_price - int(variant.get("hpp", 0))),
        "payment_method": payment_method or "cash",
        "product_name": product.get("name", ""),
        "model": product.get("model", ""),
        "image_url": product.get("image_url"),
        "created_at": created,
    }
    if USE_MONGO:
        result = db.variants.update_one(
            {"_id": oid(item.variant_id), "deleted": {"$ne": True}, "sale_qty": {"$gte": item.qty}},
            {"$inc": {"sale_qty": -item.qty}},
        )
        if result.modified_count != 1:
            raise HTTPException(status_code=400, detail="Stok jual berubah atau tidak mencukupi")
        db.transactions.insert_one(doc)
    else:
        data = load_json("variants", {"items": []})
        found = next(v for v in data["items"] if str(v.get("id")) == item.variant_id and not v.get("deleted"))
        found["sale_qty"] = int(found.get("sale_qty", 0)) - item.qty
        save_json("variants", data)
        doc["created_at"] = created.isoformat()
        doc.pop("_id", None)
        save_json("transactions", {"items": load_json("transactions", {"items": []}).get("items", []) + [json_safe(doc)]})
    add_stock_move(item.variant_id, "sale", "sold", item.qty, f"Transaksi {doc['invoice_no']}")
    return json_safe(doc)

@app.post("/api/transactions")
async def create_transaction(transaction: TransactionInput):
    transaction.payment_method = validate_payment_method(transaction.payment_method)
    item = BatchItem(variant_id=transaction.variant_id, qty=transaction.qty, unit_price=transaction.unit_price)
    doc = create_one_transaction(item, transaction.payment_method or "cash")
    return {"id": doc["id"], "invoice_no": doc["invoice_no"], "message": "Transaksi berhasil dicatat", "profit": doc["profit"]}

@app.post("/api/transactions/batch")
async def create_batch_transaction(batch: BatchTransactionInput):
    batch.payment_method = validate_payment_method(batch.payment_method)
    # Validate the whole cart first. This prevents a partial cart from being silently accepted.
    if len(batch.items) > 100:
        raise HTTPException(status_code=400, detail="Maksimal 100 item per transaksi")
    seen = set()
    for item in batch.items:
        key = (item.variant_id, item.unit_price)
        if key in seen:
            raise HTTPException(status_code=400, detail="Item duplikat di keranjang")
        seen.add(key)
        variant = find_variant(item.variant_id)
        if not variant:
            raise HTTPException(status_code=404, detail=f"Varian {item.variant_id} tidak ditemukan")
        if int(variant.get("sale_qty", 0)) < item.qty:
            raise HTTPException(status_code=400, detail=f"Stok jual {variant.get('color','')} {variant.get('size','')} tidak mencukupi")
        validate_price(variant, item.unit_price)

    if USE_MONGO:
        # Mongo transactions may not be available on every deployment/topology.
        # Sequential atomic decrements still prevent overselling.
        docs = [create_one_transaction(item, batch.payment_method or "cash") for item in batch.items]
    else:
        docs = [create_one_transaction(item, batch.payment_method or "cash") for item in batch.items]

    invoice = docs[0]["invoice_no"] if docs else invoice_no()
    return {
        "invoice_no": invoice,
        "message": f"{len(docs)} item transaksi berhasil dicatat",
        "items": docs,
        "total": sum(int(d["total"]) for d in docs),
        "profit": sum(int(d["profit"]) for d in docs),
    }

@app.get("/api/transactions")
async def list_transactions(limit: int = Query(100, ge=1, le=200)):
    transactions = get_transactions()
    products = product_map()
    output = []
    for t in transactions[:limit]:
        item = dict(t)
        item["id"] = str(t.get("_id", t.get("id", "")))
        item["variant_id"] = str(t.get("variant_id", ""))
        p = products.get(str(t.get("product_id")), {})
        item["product_name"] = t.get("product_name") or p.get("name", "")
        item["model"] = t.get("model") or p.get("model", "")
        item["image_url"] = t.get("image_url") or p.get("image_url")
        item["total"] = int(t.get("total", t.get("total_price", 0)) or 0)
        output.append(item)
    return json_safe(output)

# ============================================================
# COLORS
# ============================================================
@app.get("/api/colors")
async def list_colors(product_id: Optional[str] = Query(None)):
    colors = get_colors()
    if product_id:
        colors = [c for c in colors if str(c.get("product_id")) == product_id]
    return json_safe([
        {
            **c,
            "id": str(c.get("_id", c.get("id", ""))),
            "product_id": str(c.get("product_id", "")),
        }
        for c in colors
    ])

@app.delete("/api/colors/{color_id}")
async def delete_color(color_id: str):
    if USE_MONGO:
        color = db.colors.find_one({"_id": oid(color_id), "deleted": {"$ne": True}})
        if not color:
            raise HTTPException(status_code=404, detail="Warna tidak ditemukan")
        if db.variants.find_one({"color_id": oid(color_id), "deleted": {"$ne": True}}):
            raise HTTPException(status_code=400, detail="Tidak bisa hapus warna yang masih dipakai varian")
        db.colors.update_one({"_id": oid(color_id)}, {"$set": {"deleted": True}})
    else:
        data = load_json("colors", {"items": []})
        found = next((c for c in data["items"] if str(c.get("id")) == color_id and not c.get("deleted")), None)
        if not found:
            raise HTTPException(status_code=404, detail="Warna tidak ditemukan")
        if any(str(v.get("color_id")) == color_id and not v.get("deleted") for v in get_variants()):
            raise HTTPException(status_code=400, detail="Tidak bisa hapus warna yang masih dipakai varian")
        found["deleted"] = True
        save_json("colors", data)
    return {"message": "Warna dihapus"}

# ============================================================
# ENTRYPOINT
# ============================================================
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
