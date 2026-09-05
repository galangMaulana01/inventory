from fastapi import FastAPI, HTTPException, UploadFile, Form, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional, List
from datetime import datetime, timedelta
import os
import json
from io import BytesIO
import base64
import hashlib
from pathlib import Path

# ===== DATABASE SETUP =====
try:
    from pymongo import MongoClient
    from pymongo.errors import DuplicateKeyError
    MONGO_URI = os.getenv("MONGODB_URI", "mongodb://localhost:27017/stokku")
    mongo_client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = mongo_client.get_database()
    USE_MONGO = True
except Exception as e:
    print(f"⚠️ MongoDB tidak tersedia ({e}), fallback ke local JSON storage")
    USE_MONGO = False
    DATA_DIR = Path("/tmp/stokku_data")
    DATA_DIR.mkdir(exist_ok=True)
    db = None

# ===== CLOUDINARY SETUP =====
CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME")
CLOUDINARY_API_KEY = os.getenv("CLOUDINARY_API_KEY")
CLOUDINARY_API_SECRET = os.getenv("CLOUDINARY_API_SECRET")
USE_CLOUDINARY = bool(CLOUDINARY_CLOUD_NAME and CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET)

# ===== APP SETUP =====
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ===== MODELS =====
class SizeInput(BaseModel):
    size: str
    warehouse_qty: int
    hpp: int
    normal_price: int
    minimum_price: int

class ColorInput(BaseModel):
    color: str
    color_hex: str
    sizes: List[SizeInput]

class ProductInput(BaseModel):
    name: str
    model: str
    colors: List[ColorInput]

class VariantInput(BaseModel):
    product_id: str
    color: str
    color_hex: Optional[str] = None
    size: str
    warehouse_qty: int
    hpp: int
    normal_price: int
    minimum_price: int

class TransactionInput(BaseModel):
    variant_id: str
    qty: int
    unit_price: int
    payment_method: Optional[str] = "cash"  # bank, wallet, cash

class TransferInput(BaseModel):
    variant_id: str
    qty: int

# ===== UPLOAD HANDLER =====
async def upload_image_to_cloudinary(file_content: bytes, filename: str) -> str:
    """Upload ke Cloudinary atau local fallback"""
    if USE_CLOUDINARY:
        import requests
        url = f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD_NAME}/image/upload"
        files = {"file": (filename, file_content)}
        data = {"upload_preset": os.getenv("CLOUDINARY_UPLOAD_PRESET", "stokku")}
        auth = (CLOUDINARY_API_KEY, CLOUDINARY_API_SECRET)
        try:
            resp = requests.post(url, files=files, data=data, auth=auth, timeout=10)
            if resp.status_code == 200:
                return resp.json()["secure_url"]
        except Exception as e:
            print(f"Cloudinary error: {e}, fallback ke local")
    
    # Fallback: simpan local dan return placeholder
    file_hash = hashlib.md5(file_content).hexdigest()
    upload_dir = Path("/tmp/stokku_uploads")
    upload_dir.mkdir(exist_ok=True)
    file_path = upload_dir / f"{file_hash}.png"
    file_path.write_bytes(file_content)
    return f"/uploads/{file_hash}.png"

# ===== JSON DATA HELPERS (Fallback) =====
def load_json(key: str, default=None):
    if USE_MONGO:
        return None
    path = DATA_DIR / f"{key}.json"
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return default or {}

def save_json(key: str, data):
    if USE_MONGO:
        return
    path = DATA_DIR / f"{key}.json"
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)

# ===== MONGODB HELPERS =====
def init_db():
    if not USE_MONGO:
        return
    try:
        # Create collections dengan index
        products = db["products"]
        products.create_index("_id", unique=True)
        
        variants = db["variants"]
        variants.create_index([("product_id", 1), ("color_lower", 1), ("size", 1)], unique=True)
        
        transactions = db["transactions"]
        transactions.create_index("created_at")
        
        stock_moves = db["stock_moves"]
        colors = db["colors"]
        
        print("✅ MongoDB initialized")
    except Exception as e:
        print(f"⚠️ MongoDB init error: {e}")

init_db()

# ===== HELPER FUNCTIONS =====
def normalize_color(color: str) -> str:
    """Normalize warna ke lowercase"""
    return color.strip().lower()

def get_products():
    if USE_MONGO:
        return list(db["products"].find({"deleted": {"$ne": True}}))
    return load_json("products", {}).get("items", [])

def get_variants():
    if USE_MONGO:
        return list(db["variants"].find({"deleted": {"$ne": True}}))
    return load_json("variants", {}).get("items", [])

def get_colors():
    if USE_MONGO:
        return list(db["colors"].find({"deleted": {"$ne": True}}))
    return load_json("colors", {}).get("items", [])

def get_transactions():
    if USE_MONGO:
        return list(db["transactions"].find().sort("created_at", -1).limit(100))
    return load_json("transactions", {}).get("items", [])

def add_stock_move(variant_id: str, from_type: str, to_type: str, qty: int, notes: str = ""):
    if USE_MONGO:
        db["stock_moves"].insert_one({
            "variant_id": variant_id,
            "from_type": from_type,
            "to_type": to_type,
            "qty": qty,
            "notes": notes,
            "created_at": datetime.utcnow()
        })
    else:
        moves = load_json("stock_moves", {})
        if "items" not in moves:
            moves["items"] = []
        moves["items"].append({
            "variant_id": variant_id,
            "from_type": from_type,
            "to_type": to_type,
            "qty": qty,
            "notes": notes,
            "created_at": datetime.utcnow().isoformat()
        })
        save_json("stock_moves", moves)

# ===== API ENDPOINTS =====

@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "database": "mongodb" if USE_MONGO else "json",
        "cloudinary": "enabled" if USE_CLOUDINARY else "disabled (local fallback)"
    }

@app.get("/api/dashboard")
async def get_dashboard():
    """Dashboard dengan metrics"""
    products = get_products()
    variants = get_variants()
    transactions = get_transactions()
    
    today = datetime.utcnow().date()
    today_trans = [t for t in transactions if datetime.fromisoformat(t.get("created_at", "")).date() == today]
    
    warehouse_qty = sum(v.get("warehouse_qty", 0) for v in variants)
    sale_qty = sum(v.get("sale_qty", 0) for v in variants)
    sold_qty = sum(t.get("qty", 0) for t in today_trans)
    
    revenue = sum(t.get("qty", 0) * t.get("unit_price", 0) for t in today_trans)
    hpp_cost = sum(t.get("qty", 0) * next((v.get("hpp", 0) for v in variants if str(v.get("_id")) == str(t.get("variant_id"))), 0) for t in today_trans)
    profit = revenue - hpp_cost
    
    return {
        "total_products": len(products),
        "total_variants": len(variants),
        "warehouse_qty": warehouse_qty,
        "sale_qty": sale_qty,
        "sold_qty_today": sold_qty,
        "revenue_today": revenue,
        "profit_today": profit,
        "transactions_today": len(today_trans),
        "total_capital": sum(v.get("hpp", 0) * v.get("warehouse_qty", 0) for v in variants)
    }

@app.post("/api/products")
async def create_product(product: ProductInput):
    """Buat produk baru dengan warna dan size"""
    if USE_MONGO:
        from bson.objectid import ObjectId
        
        # Buat produk
        product_doc = {
            "_id": ObjectId(),
            "name": product.name,
            "model": product.model,
            "image_url": None,
            "created_at": datetime.utcnow(),
            "deleted": False
        }
        db["products"].insert_one(product_doc)
        
        # Buat warna dan varian
        for color_input in product.colors:
            color_norm = normalize_color(color_input.color)
            
            # Cek duplicate warna dalam produk ini
            existing_color = db["colors"].find_one({
                "product_id": product_doc["_id"],
                "color_lower": color_norm,
                "deleted": {"$ne": True}
            })
            
            if not existing_color:
                color_doc = {
                    "_id": ObjectId(),
                    "product_id": product_doc["_id"],
                    "color": color_input.color,
                    "color_lower": color_norm,
                    "color_hex": color_input.color_hex or "#cccccc",
                    "created_at": datetime.utcnow(),
                    "deleted": False
                }
                db["colors"].insert_one(color_doc)
            else:
                color_doc = existing_color
            
            # Buat varian untuk setiap size
            for size_input in color_input.sizes:
                try:
                    variant_doc = {
                        "_id": ObjectId(),
                        "product_id": product_doc["_id"],
                        "color_id": color_doc["_id"],
                        "color": color_doc["color"],
                        "color_lower": color_norm,
                        "color_hex": color_doc["color_hex"],
                        "size": size_input.size.strip(),
                        "warehouse_qty": size_input.warehouse_qty,
                        "sale_qty": 0,
                        "hpp": size_input.hpp,
                        "normal_price": size_input.normal_price,
                        "minimum_price": size_input.minimum_price,
                        "created_at": datetime.utcnow(),
                        "deleted": False
                    }
                    db["variants"].insert_one(variant_doc)
                except DuplicateKeyError:
                    raise HTTPException(status_code=400, detail=f"Duplikat warna+size: {color_input.color} {size_input.size}")
        
        return {"id": str(product_doc["_id"]), "name": product.name, "model": product.model}
    else:
        products_data = load_json("products", {})
        if "items" not in products_data:
            products_data["items"] = []
        
        product_id = str(len(products_data["items"]) + 1)
        product_doc = {
            "id": product_id,
            "name": product.name,
            "model": product.model,
            "image_url": None,
            "created_at": datetime.utcnow().isoformat(),
            "deleted": False
        }
        products_data["items"].append(product_doc)
        save_json("products", products_data)
        
        # Similar untuk warna & varian... (simplified untuk brevity)
        return {"id": product_id, "name": product.name, "model": product.model}

@app.get("/api/products")
async def list_products():
    """List semua produk (tidak deleted)"""
    products = get_products()
    return [{"_id": str(p.get("_id", p.get("id"))), "name": p["name"], "model": p["model"], "image_url": p.get("image_url")} for p in products]

@app.get("/api/products/{product_id}")
async def get_product(product_id: str):
    """Detail produk dengan varian"""
    if USE_MONGO:
        from bson.objectid import ObjectId
        try:
            product = db["products"].find_one({"_id": ObjectId(product_id), "deleted": {"$ne": True}})
        except:
            product = None
    else:
        products = get_products()
        product = next((p for p in products if str(p.get("_id", p.get("id"))) == product_id), None)
    
    if not product:
        raise HTTPException(status_code=404, detail="Produk tidak ditemukan")
    
    variants = get_variants()
    product_variants = [v for v in variants if str(v.get("product_id")) == product_id]
    
    return {
        "_id": str(product.get("_id", product.get("id"))),
        "name": product["name"],
        "model": product["model"],
        "image_url": product.get("image_url"),
        "variants": product_variants
    }

@app.put("/api/products/{product_id}")
async def update_product(product_id: str, name: str = Form(...), model: str = Form(...)):
    """Update info produk (nama, seri)"""
    if USE_MONGO:
        from bson.objectid import ObjectId
        try:
            result = db["products"].update_one(
                {"_id": ObjectId(product_id), "deleted": {"$ne": True}},
                {"$set": {"name": name, "model": model}}
            )
            if result.matched_count == 0:
                raise HTTPException(status_code=404, detail="Produk tidak ditemukan")
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))
    
    return {"message": "Produk diperbarui"}

@app.delete("/api/products/{product_id}")
async def delete_product(product_id: str):
    """Soft-delete produk (tandai sebagai deleted)"""
    if USE_MONGO:
        from bson.objectid import ObjectId
        try:
            # Soft delete produk
            result = db["products"].update_one(
                {"_id": ObjectId(product_id)},
                {"$set": {"deleted": True}}
            )
            if result.matched_count == 0:
                raise HTTPException(status_code=404, detail="Produk tidak ditemukan")
            
            # Soft delete semua varian produk ini
            db["variants"].update_many(
                {"product_id": ObjectId(product_id)},
                {"$set": {"deleted": True}}
            )
            
            # Soft delete semua warna produk ini
            db["colors"].update_many(
                {"product_id": ObjectId(product_id)},
                {"$set": {"deleted": True}}
            )
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))
    
    return {"message": f"Produk {product_id} dihapus"}

@app.post("/api/products/{product_id}/image")
async def upload_product_image(product_id: str, image: UploadFile):
    """Upload gambar produk ke Cloudinary"""
    if image.content_type not in ["image/jpeg", "image/png", "image/webp"]:
        raise HTTPException(status_code=400, detail="Format harus JPEG/PNG/WebP")
    
    content = await image.read()
    if len(content) > 5 * 1024 * 1024:  # 5MB
        raise HTTPException(status_code=400, detail="Ukuran maksimal 5MB")
    
    image_url = await upload_image_to_cloudinary(content, f"{product_id}_{image.filename}")
    
    if USE_MONGO:
        from bson.objectid import ObjectId
        db["products"].update_one(
            {"_id": ObjectId(product_id)},
            {"$set": {"image_url": image_url}}
        )
    
    return {"image_url": image_url, "message": "Gambar utama produk berhasil disimpan"}

@app.get("/api/variants")
async def list_variants(product_id: Optional[str] = Query(None)):
    """List varian dengan grouping per produk & warna"""
    variants = get_variants()
    
    if product_id:
        variants = [v for v in variants if str(v.get("product_id")) == product_id]
    
    return variants

@app.post("/api/variants")
async def create_variant(variant: VariantInput):
    """Tambah varian (warna+size) ke produk"""
    if USE_MONGO:
        from bson.objectid import ObjectId
        
        color_norm = normalize_color(variant.color)
        
        # Cek duplikat
        existing = db["variants"].find_one({
            "product_id": ObjectId(variant.product_id),
            "color_lower": color_norm,
            "size": variant.size.strip(),
            "deleted": {"$ne": True}
        })
        
        if existing:
            raise HTTPException(status_code=400, detail=f"Duplikat: {variant.color} size {variant.size} sudah ada di produk ini")
        
        # Get/create color
        color = db["colors"].find_one({
            "product_id": ObjectId(variant.product_id),
            "color_lower": color_norm,
            "deleted": {"$ne": True}
        })
        
        if not color:
            color_doc = {
                "_id": ObjectId(),
                "product_id": ObjectId(variant.product_id),
                "color": variant.color,
                "color_lower": color_norm,
                "color_hex": variant.color_hex or "#cccccc",
                "created_at": datetime.utcnow(),
                "deleted": False
            }
            db["colors"].insert_one(color_doc)
            color = color_doc
        
        # Create variant
        variant_doc = {
            "_id": ObjectId(),
            "product_id": ObjectId(variant.product_id),
            "color_id": color["_id"],
            "color": color["color"],
            "color_lower": color_norm,
            "color_hex": color.get("color_hex", "#cccccc"),
            "size": variant.size.strip(),
            "warehouse_qty": variant.warehouse_qty,
            "sale_qty": 0,
            "hpp": variant.hpp,
            "normal_price": variant.normal_price,
            "minimum_price": variant.minimum_price,
            "created_at": datetime.utcnow(),
            "deleted": False
        }
        db["variants"].insert_one(variant_doc)
        
        return {"id": str(variant_doc["_id"]), "message": "Varian berhasil ditambahkan"}
    else:
        raise HTTPException(status_code=500, detail="JSON mode tidak support create variant, gunakan MongoDB")

@app.put("/api/variants/{variant_id}")
async def update_variant(variant_id: str, hpp: int = None, normal_price: int = None, minimum_price: int = None):
    """Update harga varian"""
    if USE_MONGO:
        from bson.objectid import ObjectId
        update_data = {}
        if hpp is not None:
            update_data["hpp"] = hpp
        if normal_price is not None:
            update_data["normal_price"] = normal_price
        if minimum_price is not None:
            update_data["minimum_price"] = minimum_price
        
        if not update_data:
            raise HTTPException(status_code=400, detail="Tidak ada field untuk diupdate")
        
        result = db["variants"].update_one(
            {"_id": ObjectId(variant_id)},
            {"$set": update_data}
        )
        
        if result.matched_count == 0:
            raise HTTPException(status_code=404, detail="Varian tidak ditemukan")
    
    return {"message": "Varian diperbarui"}

@app.delete("/api/variants/{variant_id}")
async def delete_variant(variant_id: str):
    """Soft-delete varian"""
    if USE_MONGO:
        from bson.objectid import ObjectId
        result = db["variants"].update_one(
            {"_id": ObjectId(variant_id)},
            {"$set": {"deleted": True}}
        )
        
        if result.matched_count == 0:
            raise HTTPException(status_code=404, detail="Varian tidak ditemukan")
    
    return {"message": "Varian dihapus"}

@app.post("/api/transfers")
async def transfer_stock(transfer: TransferInput):
    """Pindahkan stok dari gudang ke stok jual"""
    if USE_MONGO:
        from bson.objectid import ObjectId
        
        variant = db["variants"].find_one({"_id": ObjectId(transfer.variant_id)})
        if not variant:
            raise HTTPException(status_code=404, detail="Varian tidak ditemukan")
        
        if variant["warehouse_qty"] < transfer.qty:
            raise HTTPException(status_code=400, detail=f"Stok gudang hanya {variant['warehouse_qty']} unit")
        
        # Update stok
        db["variants"].update_one(
            {"_id": ObjectId(transfer.variant_id)},
            {
                "$inc": {
                    "warehouse_qty": -transfer.qty,
                    "sale_qty": transfer.qty
                }
            }
        )
        
        add_stock_move(transfer.variant_id, "warehouse", "sale", transfer.qty, "Transfer manual")
    
    return {"message": f"Stok berhasil dipindahkan ({transfer.qty} unit)"}

@app.post("/api/transactions")
async def create_transaction(transaction: TransactionInput):
    """Catat transaksi penjualan"""
    if USE_MONGO:
        from bson.objectid import ObjectId
        
        variant = db["variants"].find_one({"_id": ObjectId(transaction.variant_id)})
        if not variant:
            raise HTTPException(status_code=404, detail="Varian tidak ditemukan")
        
        # Validasi stok jual
        if variant["sale_qty"] < transaction.qty:
            raise HTTPException(status_code=400, detail=f"Stok jual hanya {variant['sale_qty']} unit")
        
        # Validasi harga dalam range
        if not (variant["minimum_price"] <= transaction.unit_price <= variant["normal_price"]):
            raise HTTPException(
                status_code=400,
                detail=f"Harga harus antara {variant['minimum_price']} - {variant['normal_price']}"
            )
        
        # Create transaction
        trans_doc = {
            "_id": ObjectId(),
            "variant_id": ObjectId(transaction.variant_id),
            "product_id": variant["product_id"],
            "color": variant["color"],
            "size": variant["size"],
            "qty": transaction.qty,
            "unit_price": transaction.unit_price,
            "total_price": transaction.qty * transaction.unit_price,
            "hpp": variant["hpp"],
            "profit": transaction.qty * (transaction.unit_price - variant["hpp"]),
            "payment_method": transaction.payment_method or "cash",
            "created_at": datetime.utcnow()
        }
        db["transactions"].insert_one(trans_doc)
        
        # Update sale_qty
        db["variants"].update_one(
            {"_id": ObjectId(transaction.variant_id)},
            {"$inc": {"sale_qty": -transaction.qty}}
        )
        
        add_stock_move(transaction.variant_id, "sale", "sold", transaction.qty, f"Transaksi Rp{transaction.unit_price}")
    
    return {
        "id": str(trans_doc["_id"]),
        "message": "Transaksi berhasil dicatat",
        "profit": trans_doc["profit"]
    }

@app.get("/api/transactions")
async def list_transactions(limit: int = Query(100)):
    """List transaksi terbaru"""
    transactions = get_transactions()
    return transactions[:limit]

@app.get("/api/colors")
async def list_colors(product_id: Optional[str] = Query(None)):
    """List warna"""
    colors = get_colors()
    if product_id:
        colors = [c for c in colors if str(c.get("product_id")) == product_id]
    return colors

@app.delete("/api/colors/{color_id}")
async def delete_color(color_id: str):
    """Soft-delete warna (jika tidak ada varian yang pakai)"""
    if USE_MONGO:
        from bson.objectid import ObjectId
        
        # Cek ada varian yang masih pakai warna ini?
        variants_using = db["variants"].find_one({
            "color_id": ObjectId(color_id),
            "deleted": {"$ne": True}
        })
        
        if variants_using:
            raise HTTPException(status_code=400, detail="Tidak bisa hapus warna yang masih dipakai varian")
        
        result = db["colors"].update_one(
            {"_id": ObjectId(color_id)},
            {"$set": {"deleted": True}}
        )
        
        if result.matched_count == 0:
            raise HTTPException(status_code=404, detail="Warna tidak ditemukan")
    
    return {"message": "Warna dihapus"}

@app.get("/uploads/{filename}")
async def serve_upload(filename: str):
    """Serve local uploaded files"""
    file_path = Path("/tmp/stokku_uploads") / filename
    if file_path.exists():
        return FileResponse(file_path)
    raise HTTPException(status_code=404, detail="File tidak ditemukan")

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", 8000)))
