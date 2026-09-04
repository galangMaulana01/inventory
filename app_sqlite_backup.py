from __future__ import annotations

import sqlite3
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

try:
    import cloudinary
    import cloudinary.uploader
except ImportError:  # Local tests may run before the deployment dependency is installed.
    cloudinary = None
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOADS_DIR = BASE_DIR / "uploads"
DATABASE_PATH = DATA_DIR / "inventory.db"

DATA_DIR.mkdir(exist_ok=True)
UPLOADS_DIR.mkdir(exist_ok=True)

def now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def get_db() -> sqlite3.Connection:
    db = sqlite3.connect(DATABASE_PATH)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def init_db() -> None:
    with get_db() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                model TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS colors (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                color_hex TEXT,
                image_path TEXT,
                image_public_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(product_id, name)
            );

            CREATE TABLE IF NOT EXISTS variants (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
                color_id INTEGER REFERENCES colors(id) ON DELETE CASCADE,
                color TEXT NOT NULL,
                size TEXT NOT NULL,
                image_path TEXT,
                warehouse_qty INTEGER NOT NULL DEFAULT 0 CHECK (warehouse_qty >= 0),
                sale_qty INTEGER NOT NULL DEFAULT 0 CHECK (sale_qty >= 0),
                hpp INTEGER NOT NULL DEFAULT 0 CHECK (hpp >= 0),
                normal_price INTEGER NOT NULL DEFAULT 0 CHECK (normal_price >= 0),
                minimum_price INTEGER NOT NULL DEFAULT 0 CHECK (minimum_price >= 0),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(product_id, color, size)
            );

            CREATE TABLE IF NOT EXISTS stock_moves (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                variant_id INTEGER NOT NULL REFERENCES variants(id) ON DELETE CASCADE,
                move_type TEXT NOT NULL,
                qty INTEGER NOT NULL,
                note TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_no TEXT NOT NULL UNIQUE,
                variant_id INTEGER NOT NULL REFERENCES variants(id),
                qty INTEGER NOT NULL CHECK (qty > 0),
                unit_price INTEGER NOT NULL CHECK (unit_price >= 0),
                total INTEGER NOT NULL CHECK (total >= 0),
                hpp_snapshot INTEGER NOT NULL CHECK (hpp_snapshot >= 0),
                created_at TEXT NOT NULL
            );
            """
        )
        variant_columns = {column["name"] for column in db.execute("PRAGMA table_info(variants)").fetchall()}
        if "image_path" not in variant_columns:
            db.execute("ALTER TABLE variants ADD COLUMN image_path TEXT")
        if "color_id" not in variant_columns:
            db.execute("ALTER TABLE variants ADD COLUMN color_id INTEGER REFERENCES colors(id)")
        product_columns = {column["name"] for column in db.execute("PRAGMA table_info(products)").fetchall()}
        if "image_path" not in product_columns:
            db.execute("ALTER TABLE products ADD COLUMN image_path TEXT")
        color_columns = {column["name"] for column in db.execute("PRAGMA table_info(colors)").fetchall()}
        if "color_hex" not in color_columns:
            db.execute("ALTER TABLE colors ADD COLUMN color_hex TEXT")
        for row in db.execute("SELECT id, product_id, color, image_path, created_at, updated_at FROM variants WHERE color_id IS NULL").fetchall():
            color = db.execute(
                "SELECT id FROM colors WHERE product_id = ? AND name = ?",
                (row["product_id"], row["color"]),
            ).fetchone()
            if not color:
                cursor = db.execute(
                    "INSERT INTO colors (product_id, name, image_path, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                    (row["product_id"], row["color"], row["image_path"], row["created_at"], row["updated_at"]),
                )
                color_id = cursor.lastrowid
            else:
                color_id = color["id"]
                if row["image_path"]:
                    db.execute("UPDATE colors SET image_path = COALESCE(image_path, ?) WHERE id = ?", (row["image_path"], color_id))
            db.execute("UPDATE variants SET color_id = ? WHERE id = ?", (color_id, row["id"]))


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="Inventori Lokal V1", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/uploads", StaticFiles(directory=UPLOADS_DIR), name="uploads")


def variant_dict(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "product_id": row["product_id"],
        "product_name": row["product_name"],
        "model": row["model"],
        "color_id": row["color_id"],
        "image_url": image_url(row["product_image_path"]),
        "color_hex": row["color_hex"],
        "color": row["color"],
        "size": row["size"],
        "warehouse_qty": row["warehouse_qty"],
        "sale_qty": row["sale_qty"],
        "hpp": row["hpp"],
        "normal_price": row["normal_price"],
        "minimum_price": row["minimum_price"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def get_variant(db: sqlite3.Connection, variant_id: int) -> sqlite3.Row:
    row = db.execute(
        """
        SELECT v.*, p.name AS product_name, p.model, p.image_path AS product_image_path, c.color_hex
        FROM variants v
        JOIN products p ON p.id = v.product_id
        JOIN colors c ON c.id = v.color_id
        WHERE v.id = ?
        """,
        (variant_id,),
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Varian tidak ditemukan.")
    return row


def image_url(path: str | None) -> str | None:
    if not path:
        return None
    return path if path.startswith("http") else f"/uploads/{path}"


def ensure_color(db: sqlite3.Connection, product_id: int, name: str, color_hex: str | None = None) -> int:
    name = name.strip()
    color_hex = color_hex.strip() if color_hex else None
    row = db.execute(
        "SELECT id FROM colors WHERE product_id = ? AND name = ? COLLATE NOCASE",
        (product_id, name),
    ).fetchone()
    if row:
        if color_hex:
            db.execute("UPDATE colors SET color_hex = ?, updated_at = ? WHERE id = ?", (color_hex, now(), row["id"]))
        return row["id"]
    return db.execute(
        "INSERT INTO colors (product_id, name, color_hex, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (product_id, name, color_hex, now(), now()),
    ).lastrowid


def ensure_variant_unique(db: sqlite3.Connection, product_id: int, color_id: int, size: str, exclude_variant_id: int | None = None) -> None:
    query = "SELECT id FROM variants WHERE product_id = ? AND color_id = ? AND size = ? COLLATE NOCASE"
    params: list[object] = [product_id, color_id, size.strip()]
    if exclude_variant_id is not None:
        query += " AND id != ?"
        params.append(exclude_variant_id)
    if db.execute(query, params).fetchone():
        raise HTTPException(status_code=400, detail="Warna dan size ini sudah ada pada produk tersebut.")

def has_cloudinary_config() -> bool:
    return all(__import__("os").environ.get(key) for key in ("CLOUDINARY_CLOUD_NAME", "CLOUDINARY_API_KEY", "CLOUDINARY_API_SECRET"))


async def save_color_image(image: UploadFile) -> tuple[str, str | None]:
    if image.content_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise HTTPException(status_code=400, detail="Gambar harus JPG, PNG, atau WEBP.")
    data = await image.read()
    if len(data) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Ukuran gambar maksimal 5 MB.")
    if has_cloudinary_config():
        if cloudinary is None:
            raise HTTPException(status_code=500, detail="Paket Cloudinary belum terpasang. Jalankan pip install -r requirements.txt.")
        cloudinary.config(
            cloud_name=__import__("os").environ["CLOUDINARY_CLOUD_NAME"],
            api_key=__import__("os").environ["CLOUDINARY_API_KEY"],
            api_secret=__import__("os").environ["CLOUDINARY_API_SECRET"],
            secure=True,
        )
        result = cloudinary.uploader.upload(data, folder="stokku/products", resource_type="image")
        return result["secure_url"], result["public_id"]
    suffix = Path(image.filename or "foto.jpg").suffix.lower() or ".jpg"
    filename = f"{uuid.uuid4().hex}{suffix}"
    (UPLOADS_DIR / filename).write_bytes(data)
    return filename, None


class VariantCreate(BaseModel):
    product_id: int
    color: str = Field(min_length=1, max_length=60)
    color_hex: str | None = Field(default=None, max_length=20)
    size: str = Field(min_length=1, max_length=60)
    warehouse_qty: int = Field(ge=0)
    hpp: int = Field(ge=0)
    normal_price: int = Field(ge=0)
    minimum_price: int = Field(ge=0)


class SizeCreate(BaseModel):
    size: str = Field(min_length=1, max_length=60)
    warehouse_qty: int = Field(ge=0)
    hpp: int = Field(ge=0)
    normal_price: int = Field(ge=0)
    minimum_price: int = Field(ge=0)


class ColorCreate(BaseModel):
    color: str = Field(min_length=1, max_length=60)
    color_hex: str | None = Field(default=None, max_length=20)
    sizes: list[SizeCreate] = Field(min_length=1)


class ProductCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=100)
    colors: list[ColorCreate] = Field(min_length=1)


class ProductUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=100)


class VariantUpdate(BaseModel):
    color: str = Field(min_length=1, max_length=60)
    color_hex: str | None = Field(default=None, max_length=20)
    size: str = Field(min_length=1, max_length=60)
    warehouse_qty: int = Field(ge=0)
    hpp: int = Field(ge=0)
    normal_price: int = Field(ge=0)
    minimum_price: int = Field(ge=0)


class TransferCreate(BaseModel):
    variant_id: int
    qty: int = Field(gt=0)


class TransactionCreate(BaseModel):
    variant_id: int
    qty: int = Field(gt=0)
    unit_price: int = Field(gt=0)


class TransactionLineCreate(BaseModel):
    variant_id: int
    qty: int = Field(gt=0)
    unit_price: int = Field(gt=0)


class TransactionBatchCreate(BaseModel):
    items: list[TransactionLineCreate] = Field(min_length=1)


@app.get("/api/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/dashboard")
def dashboard() -> dict:
    with get_db() as db:
        stock = db.execute(
            """
            SELECT
                COALESCE(SUM(warehouse_qty), 0) AS warehouse_qty,
                COALESCE(SUM(sale_qty), 0) AS sale_qty,
                COALESCE(SUM((warehouse_qty + sale_qty) * hpp), 0) AS capital
            FROM variants
            """
        ).fetchone()
        today = datetime.now().astimezone().date().isoformat()
        sales = db.execute(
            """
            SELECT
                COALESCE(SUM(total), 0) AS revenue,
                COALESCE(SUM(qty), 0) AS sold_qty,
                COALESCE(SUM(total - (qty * hpp_snapshot)), 0) AS profit,
                COUNT(*) AS transaction_count
            FROM transactions
            WHERE substr(created_at, 1, 10) = ?
            """,
            (today,),
        ).fetchone()
        recent = db.execute(
            """
            SELECT t.invoice_no, t.qty, t.unit_price, t.total, t.created_at,
                   p.name AS product_name, p.model, v.color, v.size, p.image_path
            FROM transactions t
            JOIN variants v ON v.id = t.variant_id
            JOIN products p ON p.id = v.product_id
            JOIN colors c ON c.id = v.color_id
            ORDER BY t.id DESC LIMIT 5
            """
        ).fetchall()
        top_products = db.execute(
            """
            SELECT p.name AS product_name, p.model, MIN(v.color) AS color, MIN(v.size) AS size,
                   MIN(p.image_path) AS image_path, SUM(t.qty) AS sold_qty, SUM(t.total) AS revenue
            FROM transactions t
            JOIN variants v ON v.id = t.variant_id
            JOIN products p ON p.id = v.product_id
            JOIN colors c ON c.id = v.color_id
            GROUP BY p.id
            ORDER BY sold_qty DESC, revenue DESC
            LIMIT 5
            """
        ).fetchall()

    def sales_item(row: sqlite3.Row) -> dict:
        item = dict(row)
        item["image_url"] = image_url(item.pop("image_path"))
        return item

    return {
        "warehouse_qty": stock["warehouse_qty"],
        "sale_qty": stock["sale_qty"],
        "capital": stock["capital"],
        "revenue_today": sales["revenue"],
        "sold_qty_today": sales["sold_qty"],
        "profit_today": sales["profit"],
        "transaction_count_today": sales["transaction_count"],
        "recent_transactions": [sales_item(row) for row in recent],
        "top_products": [sales_item(row) for row in top_products],
    }


@app.get("/api/products")
def products() -> list[dict]:
    with get_db() as db:
        rows = db.execute("SELECT * FROM products ORDER BY name COLLATE NOCASE, model COLLATE NOCASE").fetchall()
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "model": row["model"],
            "image_url": image_url(row["image_path"]),
        }
        for row in rows
    ]


@app.post("/api/products")
def create_product(payload: ProductCreate) -> dict:
    seen_colors: set[str] = set()
    for color in payload.colors:
        key = color.color.strip().casefold()
        if key in seen_colors:
            raise HTTPException(status_code=400, detail="Nama warna tidak boleh duplikat.")
        seen_colors.add(key)
        seen_sizes: set[str] = set()
        for size in color.sizes:
            if size.minimum_price > size.normal_price:
                raise HTTPException(status_code=400, detail="Harga minimum tidak boleh melebihi harga normal.")
            size_key = size.size.strip().casefold()
            if size_key in seen_sizes:
                raise HTTPException(status_code=400, detail=f"Size pada warna {color.color} tidak boleh duplikat.")
            seen_sizes.add(size_key)
    with get_db() as db:
        cursor = db.execute(
            "INSERT INTO products (name, model, image_path, created_at) VALUES (?, ?, ?, ?)",
            (payload.name.strip(), payload.model.strip(), None, now()),
        )
        product_id = cursor.lastrowid
        colors = []
        for color in payload.colors:
            color_id = ensure_color(db, product_id, color.color, color.color_hex)
            colors.append({"id": color_id, "name": color.color.strip(), "color_hex": color.color_hex})
            for size in color.sizes:
                cursor = db.execute(
                    """
                    INSERT INTO variants
                    (product_id, color_id, color, size, warehouse_qty, hpp, normal_price, minimum_price, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (product_id, color_id, color.color.strip(), size.size.strip(), size.warehouse_qty, size.hpp, size.normal_price, size.minimum_price, now(), now()),
                )
                if size.warehouse_qty:
                    db.execute(
                        "INSERT INTO stock_moves (variant_id, move_type, qty, note, created_at) VALUES (?, ?, ?, ?, ?)",
                        (cursor.lastrowid, "stok_awal", size.warehouse_qty, "Produk baru dibuat", now()),
                    )
    return {"id": product_id, "colors": colors, "message": "Produk beserta varian warnanya berhasil dibuat."}


@app.patch("/api/products/{product_id}")
def update_product(product_id: int, payload: ProductUpdate) -> dict:
    name = payload.name.strip()
    model = payload.model.strip()
    if not name or not model:
        raise HTTPException(status_code=400, detail="Merek dan seri wajib diisi.")
    with get_db() as db:
        product = db.execute("SELECT id FROM products WHERE id = ?", (product_id,)).fetchone()
        if not product:
            raise HTTPException(status_code=404, detail="Produk tidak ditemukan.")
        db.execute("UPDATE products SET name = ?, model = ? WHERE id = ?", (name, model, product_id))
    return {"message": "Produk berhasil diperbarui."}


@app.delete("/api/products/{product_id}")
def delete_product(product_id: int) -> dict:
    with get_db() as db:
        if not db.execute("SELECT id FROM products WHERE id = ?", (product_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Produk tidak ditemukan.")
        if db.execute("SELECT 1 FROM transactions t JOIN variants v ON v.id=t.variant_id WHERE v.product_id=? LIMIT 1", (product_id,)).fetchone():
            raise HTTPException(status_code=400, detail="Produk tidak bisa dihapus karena sudah memiliki riwayat transaksi. Gunakan arsip di versi berikutnya.")
        db.execute("DELETE FROM products WHERE id = ?", (product_id,))
    return {"message": "Produk berhasil dihapus."}

@app.delete("/api/variants/{variant_id}")
def delete_variant(variant_id: int) -> dict:
    with get_db() as db:
        variant = get_variant(db, variant_id)
        if variant["sale_qty"] > 0:
            raise HTTPException(status_code=400, detail="Varian tidak bisa dihapus selama masih ada stok jual.")
        if db.execute("SELECT 1 FROM transactions WHERE variant_id=? LIMIT 1", (variant_id,)).fetchone():
            raise HTTPException(status_code=400, detail="Varian tidak bisa dihapus karena sudah memiliki riwayat transaksi.")
        db.execute("DELETE FROM variants WHERE id=?", (variant_id,))
        db.execute("DELETE FROM colors WHERE id=? AND NOT EXISTS (SELECT 1 FROM variants WHERE color_id=?)", (variant["color_id"], variant["color_id"]))
    return {"message": "Varian berhasil dihapus."}

@app.delete("/api/colors/{color_id}")
def delete_color(color_id: int) -> dict:
    with get_db() as db:
        color = db.execute("SELECT id FROM colors WHERE id=?", (color_id,)).fetchone()
        if not color: raise HTTPException(status_code=404, detail="Warna tidak ditemukan.")
        if db.execute("SELECT 1 FROM variants WHERE color_id=? LIMIT 1", (color_id,)).fetchone():
            raise HTTPException(status_code=400, detail="Warna masih memiliki size/varian. Hapus variannya terlebih dahulu.")
        db.execute("DELETE FROM colors WHERE id=?", (color_id,))
    return {"message": "Warna berhasil dihapus."}

@app.post("/api/products/{product_id}/image")
async def upload_product_image(product_id: int, image: UploadFile = File(...)) -> dict:
    new_image_path, _ = await save_color_image(image)
    with get_db() as db:
        product = db.execute("SELECT id FROM products WHERE id = ?", (product_id,)).fetchone()
        if not product:
            raise HTTPException(status_code=404, detail="Produk tidak ditemukan.")
        db.execute("UPDATE products SET image_path = ? WHERE id = ?", (new_image_path, product_id))
    return {"image_url": image_url(new_image_path), "message": "Gambar utama produk berhasil disimpan."}


@app.get("/api/variants")
def variants(location: Literal["warehouse", "sale", "all"] = "all") -> list[dict]:
    where = ""
    if location == "warehouse":
        where = "WHERE v.warehouse_qty > 0"
    elif location == "sale":
        where = "WHERE v.sale_qty > 0"
    with get_db() as db:
        rows = db.execute(
            f"""
            SELECT v.*, p.name AS product_name, p.model, p.image_path AS product_image_path, c.color_hex
            FROM variants v
            JOIN products p ON p.id = v.product_id
            JOIN colors c ON c.id = v.color_id
            {where}
            ORDER BY p.name COLLATE NOCASE, p.model COLLATE NOCASE, v.color COLLATE NOCASE, v.size COLLATE NOCASE
            """
        ).fetchall()
    return [variant_dict(row) for row in rows]


@app.post("/api/variants")
def create_variant(payload: VariantCreate) -> dict:
    if payload.minimum_price > payload.normal_price:
        raise HTTPException(status_code=400, detail="Harga minimum tidak boleh melebihi harga normal.")
    with get_db() as db:
        product = db.execute("SELECT id FROM products WHERE id = ?", (payload.product_id,)).fetchone()
        if not product:
            raise HTTPException(status_code=404, detail="Produk tidak ditemukan.")
        try:
            color_id = ensure_color(db, payload.product_id, payload.color, payload.color_hex)
            ensure_variant_unique(db, payload.product_id, color_id, payload.size)
            canonical_color = db.execute("SELECT name FROM colors WHERE id = ?", (color_id,)).fetchone()["name"]
            cursor = db.execute(
                """
                INSERT INTO variants
                (product_id, color_id, color, size, warehouse_qty, hpp, normal_price, minimum_price, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    payload.product_id,
                    color_id,
                    canonical_color,
                    payload.size.strip(),
                    payload.warehouse_qty,
                    payload.hpp,
                    payload.normal_price,
                    payload.minimum_price,
                    now(),
                    now(),
                ),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=400, detail="Warna dan size ini sudah ada pada produk tersebut.")
        variant_id = cursor.lastrowid
        if payload.warehouse_qty:
            db.execute(
                "INSERT INTO stock_moves (variant_id, move_type, qty, note, created_at) VALUES (?, ?, ?, ?, ?)",
                (variant_id, "stok_awal", payload.warehouse_qty, "Varian baru dibuat", now()),
            )
    return {"id": variant_id, "color_id": color_id, "message": "Varian berhasil ditambahkan ke Gudang."}


@app.patch("/api/variants/{variant_id}")
def update_variant(variant_id: int, payload: VariantUpdate) -> dict:
    if payload.minimum_price > payload.normal_price:
        raise HTTPException(status_code=400, detail="Harga minimum tidak boleh melebihi harga normal.")
    with get_db() as db:
        current = get_variant(db, variant_id)
        difference = payload.warehouse_qty - current["warehouse_qty"]
        try:
            color_id = ensure_color(db, current["product_id"], payload.color, payload.color_hex)
            ensure_variant_unique(db, current["product_id"], color_id, payload.size, variant_id)
            canonical_color = db.execute("SELECT name FROM colors WHERE id = ?", (color_id,)).fetchone()["name"]
            db.execute(
                """
                UPDATE variants
                SET color_id = ?, color = ?, size = ?, warehouse_qty = ?, hpp = ?, normal_price = ?, minimum_price = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    color_id,
                    canonical_color,
                    payload.size.strip(),
                    payload.warehouse_qty,
                    payload.hpp,
                    payload.normal_price,
                    payload.minimum_price,
                    now(),
                    variant_id,
                ),
            )
        except sqlite3.IntegrityError:
            raise HTTPException(status_code=400, detail="Warna dan size ini sudah dipakai oleh varian lain.")
        if difference:
            db.execute(
                "INSERT INTO stock_moves (variant_id, move_type, qty, note, created_at) VALUES (?, ?, ?, ?, ?)",
                (variant_id, "update_stok_gudang", difference, "Update unit", now()),
            )
    return {"message": "Unit berhasil diperbarui."}


@app.post("/api/transfers")
def transfer_stock(payload: TransferCreate) -> dict:
    with get_db() as db:
        variant = get_variant(db, payload.variant_id)
        if variant["warehouse_qty"] < payload.qty:
            raise HTTPException(status_code=400, detail="Stok Gudang tidak mencukupi.")
        db.execute(
            "UPDATE variants SET warehouse_qty = warehouse_qty - ?, sale_qty = sale_qty + ?, updated_at = ? WHERE id = ?",
            (payload.qty, payload.qty, now(), payload.variant_id),
        )
        db.execute(
            "INSERT INTO stock_moves (variant_id, move_type, qty, note, created_at) VALUES (?, ?, ?, ?, ?)",
            (payload.variant_id, "pindah_ke_jual", payload.qty, "Gudang ke Stok Jual", now()),
        )
    return {"message": "Barang berhasil dipindahkan ke Stok Jual."}


@app.post("/api/transactions")
def create_transaction(payload: TransactionCreate) -> dict:
    with get_db() as db:
        variant = get_variant(db, payload.variant_id)
        if variant["sale_qty"] < payload.qty:
            raise HTTPException(status_code=400, detail="Stok Jual tidak mencukupi.")
        if not variant["minimum_price"] <= payload.unit_price <= variant["normal_price"]:
            raise HTTPException(
                status_code=400,
                detail="Harga jual harus berada di antara harga minimum dan harga normal.",
            )
        invoice_no = f"TRX-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4].upper()}"
        total = payload.qty * payload.unit_price
        db.execute(
            "UPDATE variants SET sale_qty = sale_qty - ?, updated_at = ? WHERE id = ?",
            (payload.qty, now(), payload.variant_id),
        )
        db.execute(
            """
            INSERT INTO transactions (invoice_no, variant_id, qty, unit_price, total, hpp_snapshot, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (invoice_no, payload.variant_id, payload.qty, payload.unit_price, total, variant["hpp"], now()),
        )
        db.execute(
            "INSERT INTO stock_moves (variant_id, move_type, qty, note, created_at) VALUES (?, ?, ?, ?, ?)",
            (payload.variant_id, "penjualan", -payload.qty, invoice_no, now()),
        )
    return {"invoice_no": invoice_no, "message": "Transaksi berhasil disimpan."}


@app.post("/api/transactions/batch")
def create_transaction_batch(payload: TransactionBatchCreate) -> dict:
    requested_qty: dict[int, int] = {}
    for item in payload.items:
        requested_qty[item.variant_id] = requested_qty.get(item.variant_id, 0) + item.qty

    with get_db() as db:
        variants = {variant_id: get_variant(db, variant_id) for variant_id in requested_qty}
        for variant_id, qty in requested_qty.items():
            variant = variants[variant_id]
            if variant["sale_qty"] < qty:
                raise HTTPException(status_code=400, detail=f"Stok Jual {variant['product_name']} tidak mencukupi.")
        for item in payload.items:
            variant = variants[item.variant_id]
            if not variant["minimum_price"] <= item.unit_price <= variant["normal_price"]:
                raise HTTPException(status_code=400, detail=f"Harga {variant['product_name']} harus berada di rentang yang diizinkan.")

        batch_no = f"TRX-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4].upper()}"
        for index, item in enumerate(payload.items, start=1):
            variant = variants[item.variant_id]
            invoice_no = f"{batch_no}-{index:02d}"
            total = item.qty * item.unit_price
            db.execute(
                "UPDATE variants SET sale_qty = sale_qty - ?, updated_at = ? WHERE id = ?",
                (item.qty, now(), item.variant_id),
            )
            db.execute(
                """
                INSERT INTO transactions (invoice_no, variant_id, qty, unit_price, total, hpp_snapshot, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (invoice_no, item.variant_id, item.qty, item.unit_price, total, variant["hpp"], now()),
            )
            db.execute(
                "INSERT INTO stock_moves (variant_id, move_type, qty, note, created_at) VALUES (?, ?, ?, ?, ?)",
                (item.variant_id, "penjualan", -item.qty, invoice_no, now()),
            )
    return {"invoice_no": batch_no, "item_count": len(payload.items), "message": "Transaksi keranjang berhasil disimpan."}


@app.get("/api/transactions")
def transaction_history() -> list[dict]:
    with get_db() as db:
        rows = db.execute(
            """
            SELECT t.*, p.name AS product_name, p.model, v.color, v.size, p.image_path
            FROM transactions t
            JOIN variants v ON v.id = t.variant_id
            JOIN products p ON p.id = v.product_id
            JOIN colors c ON c.id = v.color_id
            ORDER BY t.id DESC
            """
        ).fetchall()
    history = []
    for row in rows:
        item = dict(row)
        item["image_url"] = image_url(item.pop("image_path"))
        history.append(item)
    return history
