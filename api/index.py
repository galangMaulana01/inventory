import os
import asyncio
import random
import re
import uuid
import hashlib
import json
import traceback
from datetime import datetime, timezone, date, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from auth import get_admin_user
import httpx
import requests
import uvicorn
from authlib.integrations.starlette_client import OAuth
from dotenv import load_dotenv
from fastapi import FastAPI, Request, Response, Depends, HTTPException, BackgroundTasks, Query, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import or_, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload
from starlette.config import Config
from starlette.middleware.sessions import SessionMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
from auth import (
    hash_password,
    verify_password,
    create_access_token,
    get_current_user
)
from bottele import send_telegram_message, handle_telegram_update_webhook
from database import engine, SessionLocal
import models
from models import Base, User, Bet, Deposit, Withdraw, WithdrawStatus, ChatMessage
models.Base.metadata.create_all(bind=engine)
from passlib.context import CryptContext
from typing import Optional
import shutil

# ================= INIT =================

load_dotenv()

app = FastAPI()

@app.middleware("http")
async def log_requests(request: Request, call_next):
    origin = request.headers.get("origin")
    method = request.method
    url = request.url.path
    print(f"DEBUG: Request Masuk -> {method} {url} | Origin: {origin}")
    
    response = await call_next(request)
    
    print(f"DEBUG: Response Status -> {response.status_code}")
    return response


pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

# 1. Pastikan domain baru ada di sini
origins = [
    "https://bola433.my.id",
    "https://www.bola433.my.id",
    "http://localhost:8000",
]

# 2. Tambahkan ProxyHeaders (Paling atas agar skema HTTPS kedeteksi)
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

# 3. CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True, # WAJIB True untuk cookies
    allow_methods=["*"],
    allow_headers=["*"],
)

# 4. Session Middleware
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SESSION_SECRET", "pastiin-ini-rahasia-banget"),
    https_only=True, # Set True karena lo pakai HTTPS
    same_site="lax"  # Pakai 'lax' untuk domain yang sama agar lebih stabil
)


@app.middleware("http")
async def fix_proto_https(request: Request, call_next):
    x_forwarded_proto = request.headers.get("x-forwarded-proto")
    if x_forwarded_proto == "https":
        request.scope["scheme"] = "https"
    return await call_next(request)
    



# ================= GOOGLE LOGIN =================

config = Config(".env")

oauth = OAuth()

oauth.register(
    name="google",
    client_id=config("GOOGLE_CLIENT_ID"),
    client_secret=config("GOOGLE_CLIENT_SECRET"),
    server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
    client_kwargs={
        "scope": "openid email profile"
    }
)

BASE_DIR = Path(__file__).resolve().parent

app.mount(
    "/static",
    StaticFiles(directory=str(BASE_DIR / "static")),
    name="static"
)

Base.metadata.create_all(bind=engine)

transactions = {}

STARPAGO_URL = "https://id.api.starpago.com/api/v2/payment/order/create"
APP_ID = "2efd66cb53fb2a0b86fdd1a998cc18d8"
APP_SECRET = "755f59601a48930216448350b79eee04"
ODDS_API_KEY = "90d84a3b7c5f22ce46b4d9bdaa0b466c"
LUNEXA_CONFIG = {
    "agent_code": "jumpapegas880",
    "agent_token": "4c7995b7856a5b0377149d48a47fd4b1",
    "base_url": "https://svc-v1.lunexa.to/api/v2"
}

# ================= ODDS CACHE =================

ODDS_CACHE = {}

# ================= LIVE STREAM CACHE (WeStream) =================
WESTREAM_BASE = "https://westream.su"
WESTREAM_MATCHES_ENDPOINT = "/matches/football"

LIVE_CACHE: list = []
LIVE_CACHE_LOCK = asyncio.Lock()

LEAGUES = [
	"soccer_uefa_champs_league",
	"soccer_uefa_europa_conference_league",
	"soccer_uefa_europa_league",
    "soccer_epl",
    "soccer_spain_la_liga",
    "soccer_italy_serie_a",
    "soccer_germany_bundesliga",
    "soccer_france_ligue_one",
]
# ================= DATABASE =================

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

PAYMENT_METHODS = {
    "DANA": {"payMethod": "ID_DANA", "channelCode": "DANA"},
    "QRIS": {"payMethod": "ID_QRIS", "channelCode": "QRIS"},

    "BRI": {"payMethod": "ID_VA", "channelCode": "BRI"},
    "MANDIRI": {"payMethod": "ID_VA", "channelCode": "MANDIRI"},
}

# ================= HELPER =================

def parse_extra(ext: dict):
    if not ext or not isinstance(ext, dict):
        return ""

    keys = sorted(ext.keys())

    pairs = []
    for k in keys:
        v = str(ext[k]).strip()
        pairs.append(f"{k}={v}")

    return "&".join(pairs)


def generate_sign(params: dict, app_secret: str):
    keys = [k for k in params.keys() if k != "sign" and params[k] is not None]
    keys.sort()

    parts = []

    for key in keys:
        value = params[key]

        if isinstance(value, dict):
            val_str = parse_extra(value)
        else:
            val_str = str(value).strip()

        parts.append(f"{key}={val_str}")

    sign_str = "&".join(parts)

    if app_secret:
        sign_str += f"&key={app_secret}"

    print("DEBUG SIGN STRING:", sign_str)

    sign = hashlib.sha256(sign_str.encode("utf-8")).hexdigest()

    return sign, sign_str
# ================= PAGES =================

@app.get("/")  
def root():  
    return FileResponse(str(BASE_DIR / "views" / "home.html")) 
    
@app.get("/game")  
def mahjong():  
    return FileResponse(str(BASE_DIR / "views" / "game.html"))
    
@app.get("/login")  
def login():  
    return FileResponse(str(BASE_DIR / "views" / "login.html"))
    
@app.get("/history")  
def history_page():  
    return FileResponse(str(BASE_DIR / "views" / "history.html")) 
    
@app.get("/historydepo")  
def history_page_depo():  
    return FileResponse(str(BASE_DIR / "views" / "historytransaksi.html"))
  
@app.get("/setting")  
def setting():  
    return FileResponse(str(BASE_DIR / "views" / "setting.html"))  
      
@app.get("/username")  
def username():  
    return FileResponse(str(BASE_DIR / "views" / "username.html"))  
      
@app.get("/password")  
def password():  
    return FileResponse(str(BASE_DIR / "views" / "password.html")) 
    
@app.get("/live")
def live_page():
    return FileResponse(str(BASE_DIR / "views" / "live.html"))
      
@app.get("/sportbook")  
def sportbook():  
    return FileResponse(str(BASE_DIR / "views" / "sportbook.html"))  
    
@app.get("/contact")  
def contact():  
    return FileResponse(str(BASE_DIR / "views" / "contact.html"))  
      
@app.get("/account")  
def account():  
    return FileResponse(str(BASE_DIR / "views" / "account.html"))
    
@app.get("/info")  
def info():  
    return FileResponse(str(BASE_DIR / "views" / "info.html"))
    
@app.get("/referral")  
def referal():  
    return FileResponse(str(BASE_DIR / "views" / "referral.html"))
      
@app.get("/deposit")  
def wallet():  
    return FileResponse(str(BASE_DIR / "views" / "deposit.html"))

@app.get("/panel/")
def login_panel_page():
    file_path = BASE_DIR / "views" / "panel" / "login.html"
    return FileResponse(str(file_path))
    
@app.get("/panel/dashboard")
def dashboardpanel(
    admin: User = Depends(get_admin_user)
):
    file_path = BASE_DIR / "views" / "panel" / "dashboard.html"
    return FileResponse(str(file_path))
    
@app.get("/panel/user")
def userpanel(
    admin: User = Depends(get_admin_user)
):
    file_path = BASE_DIR / "views" / "panel" / "user.html"
    return FileResponse(str(file_path))
    
@app.get("/panel/deposit")
def depositpanel(
    admin: User = Depends(get_admin_user)
):
    file_path = BASE_DIR / "views" / "panel" / "deposit.html"
    return FileResponse(str(file_path))
    
@app.get("/panel/withdraw")
def withdrawpanel(
    admin: User = Depends(get_admin_user)
):
    file_path = BASE_DIR / "views" / "panel" / "withdraw.html"
    return FileResponse(str(file_path))
    
@app.get("/panel/leaderboard")
def leaderboardpanel(
    admin: User = Depends(get_admin_user)
):
    file_path = BASE_DIR / "views" / "panel" / "leaderboard.html"
    return FileResponse(str(file_path))
    
@app.get("/panel/bet")
def leaderboardpanel(
    admin: User = Depends(get_admin_user)
):
    file_path = BASE_DIR / "views" / "panel" / "bet.html"
    return FileResponse(str(file_path))
    
@app.get("/panel/forum")
def forumpanel(
    admin: User = Depends(get_admin_user)
):
    file_path = BASE_DIR / "views" / "panel" / "forum.html"
    return FileResponse(str(file_path))
# ================= HELPER =================
def is_email(value: str) -> bool:
    """Check if the value is a valid email"""
    return re.match(r"[^@]+@[^@]+\.[^@]+", value) is not None

def is_phone(value: str) -> bool:
    return re.match(r'^\+?\d{6,15}$', value) is not None

# ================= LUNEXA LOGIKA =================

async def create_lunexa_user(username: str):
    """
    Mendaftarkan username ke Lunexa API
    """
    async with httpx.AsyncClient() as client:
        payload = {
            "agent_code": LUNEXA_CONFIG["agent_code"],
            "agent_token": LUNEXA_CONFIG["agent_token"],
            "user_code": username,
            "deposit_amount": 0  # Default 0 saat daftar
        }
        try:
            response = await client.post(f"{LUNEXA_CONFIG['base_url']}/user_create", json=payload)
            return response.json()
        except Exception as e:
            print(f"LUNEXA CREATE USER ERROR for {username}: {e}")
            return none
            
async def lunexa_deposit(username: str, amount: int):
    """Kirim saldo dari web ke dompet game Lunexa"""
    async with httpx.AsyncClient() as client:
        payload = {
            "agent_code": LUNEXA_CONFIG["agent_code"],
            "agent_token": LUNEXA_CONFIG["agent_token"],
            "user_code": username,
            "amount": amount
        }
        try:
            response = await client.post(f"{LUNEXA_CONFIG['base_url']}/user_deposit", json=payload)
            return response.json()
        except Exception as e:
            print(f"LUNEXA DEPO ERROR: {e}")
            return none

async def lunexa_withdraw_all(username: str):
    async with httpx.AsyncClient() as client:
        
        payload = {
            "agent_code": LUNEXA_CONFIG["agent_code"],
            "agent_token": LUNEXA_CONFIG["agent_token"],
            "user_code": username
        }
        
        try:
            url = f"{LUNEXA_CONFIG['base_url']}/user_withdraw"
            response = await client.post(url, json=payload, timeout=10.0)
            res_data = response.json()
            
            print(f"DEBUG LUNEXA WD: {res_data}") 
            return res_data
        except Exception as e:
            print(f"LUNEXA WD ERROR: {e}")
            return none

            
async def process_withdraw_update(wid: int, action: str, db: Session):
    
    withdraw = db.query(Withdraw).filter(Withdraw.id == wid).first()
    if not withdraw:
        return f"❌ ID {wid} tidak ditemukan"

    user = db.query(User).filter(User.id == withdraw.user_id).with_for_update().first()
    if not user:
        return f"❌ User tidak ditemukan"

    if action == "approve":
        if withdraw.status != WithdrawStatus.pending:
            return f"⚠️ WD {wid} sudah diproses sebelumnya"

        try:
            res = await lunexa_withdraw_all(user.username)
            
            if res and res.get("status") == 1:
                game_wd_amount = Decimal(str(res.get("withdraw_amount", 0)))
                
                user.balance += game_wd_amount
                print(f"DEBUG: Berhasil tarik {game_wd_amount} dari Lunexa untuk user {user.username}")
            else:
                return f"⚠️ Gagal tarik saldo dari Lunexa. Pastikan user sudah keluar dari game!"
        
        except Exception as e:
            print(f"ERROR LUNEXA SYNC: {e}")
            return f"❌ Error koneksi ke server game saat sinkronisasi saldo."

        withdraw.status = WithdrawStatus.approved
        msg = f"✅ WD ID {wid} DISETUJUI. Saldo game sudah ditarik ke Web."

    elif action == "paid":
        if withdraw.status != WithdrawStatus.approved:
            return f"⚠️ WD {wid} harus berstatus APPROVED dulu"
        
        withdraw.status = WithdrawStatus.paid
        msg = f"💸 WD ID {wid} SUDAH DIBAYAR"

    elif action == "reject":
        if withdraw.status not in [WithdrawStatus.pending, WithdrawStatus.approved]:
            return f"⚠️ WD {wid} tidak bisa ditolak"

        # Kembalikan saldo ke user jika ditolak
        withdraw.status = WithdrawStatus.rejected
        user.balance += withdraw.amount
        msg = f"❌ WD ID {wid} DITOLAK & SALDO DIKEMBALIKAN"

    withdraw.updated_at = datetime.utcnow()
    db.commit() # Simpan semua perubahan saldo

    return msg
    
@app.get("/api/user/sync-balance")
async def sync_balance(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    user = db.query(User).filter(User.id == current_user.id).with_for_update().first()

    try:
        res = await lunexa_withdraw_all(user.username)
        
        if res and res.get("status") == 1:
            game_amount = Decimal(str(res.get("withdraw_amount", 0)))
            
            if game_amount > 0:
                user.balance += game_amount
                user.is_playing = False
                db.commit()

                return {
                    "status": "success",
                    "msg": f"Saldo sebesar {game_amount} berhasil ditarik dari game.",
                    "new_balance": float(user.balance)
                }
            else:
                return {
                    "status": "success",
                    "msg": "Saldo di game kosong.",
                    "new_balance": float(user.balance)
                }
        
        return {"status": "error", "msg": res.get("msg", "Gagal kontak server game.")}

    except Exception as e:
        db.rollback()
        print(f"SYNC ERROR: {e}")
        return {"status": "error", "msg": "Terjadi kesalahan sistem saat sinkronisasi."}
        
from pydantic import BaseModel

class GameListRequest(BaseModel):
    provider_code: str

@app.post("/api/games/list")
async def get_game_list(data: GameListRequest):
    print(f"DEBUG: Request list game untuk provider: {data.provider_code}")
    
    async with httpx.AsyncClient() as client:
        payload = {
            "agent_code": LUNEXA_CONFIG["agent_code"],
            "agent_token": LUNEXA_CONFIG["agent_token"],
            "provider_code": data.provider_code,
            "lang": "en"
        }
        try:
            response = await client.post(
                "https://svc-v1.lunexa.to/api/v2/game_list", 
                json=payload
            )
            res_json = response.json()
            print(f"DEBUG: Lunexa Response: {res_json.get('msg')}")
            return res_json
        except Exception as e:
            print(f"ERROR: {str(e)}")
            raise HTTPException(status_code=500, detail="Gagal kontak API Lunexa")
            
import httpx
from fastapi import Query, Depends
from fastapi.responses import RedirectResponse

@app.get("/api/game/launch")
async def launch_game(
    game_code: str = Query(...),
    provider_code: str = Query("PRAGMATIC"),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    try:
        cleanup = await lunexa_withdraw_all(current_user.username)
        if cleanup and cleanup.get("status") == 1:
            added_back = Decimal(str(cleanup.get("withdraw_amount", 0)))
            if added_back > 0:
                user_clean = db.query(User).filter(User.id == current_user.id).with_for_update().first()
                user_clean.balance += added_back
                db.commit()
                print(f"CLEANUP: Balikin {added_back} ke DB sebelum main lagi.")
    except:
        pass 
    
    user = db.query(User).filter(User.id == current_user.id).with_for_update().first()
    amount_to_send = int(user.balance) # Kita ambil angka bulat saja

    if amount_to_send > 0:
        depo_res = await lunexa_deposit(user.username, amount_to_send)
        if depo_res and depo_res.get("status") == 1:
            user.balance -= Decimal(str(amount_to_send))
            user.is_playing = True # SET INI JADI TRUE BRO!
            db.commit()
            print(f"SEAMLESS: Berhasil kirim {amount_to_send} ke game untuk {user.username}")
        else:
            return {"status": 0, "msg": "Gagal sinkron saldo ke server game."}

    payload = {
        "agent_code": LUNEXA_CONFIG["agent_code"],
        "agent_token": LUNEXA_CONFIG["agent_token"],
        "user_code": user.username,
        "game_type": "slot",
        "provider_code": provider_code,
        "game_code": game_code,
        "lang": "en"
    }

    async with httpx.AsyncClient() as client:
        try:
            url_api = f"{LUNEXA_CONFIG['base_url']}/game_launch"
            res = await client.post(url_api, json=payload, timeout=5.0)
            data = res.json()

            if data.get("status") == 1:
                return RedirectResponse(url=data.get("launch_url"))
            
            user.balance += Decimal(str(amount_to_send))
            db.commit()
            return {"status": 0, "msg": "Gagal memuat game."}
        except:
            user.balance += Decimal(str(amount_to_send))
            db.commit()
            return {"status": 0, "msg": "Error koneksi."}

# ================= REGISTER =================
@app.post("/register")
async def register(req: Request, db: Session = Depends(get_db)):
    try:
        body = await req.json()
        username = body.get("username")
        password = body.get("password")
        contact = body.get("contact")
        
        ref = req.query_params.get("ref")

        if not username or not password or not contact:
            raise HTTPException(status_code=400, detail="Isi setidaknya email atau nomor HP")
            
        email = None
        phone = None
        if is_email(contact):
            email = contact
        elif is_phone(contact):
            phone = contact
        else:
            raise HTTPException(status_code=400, detail="Contact harus email atau nomor HP yang valid")

        if db.query(User).filter(User.username == username).first():
            raise HTTPException(status_code=400, detail="Username sudah digunakan")
        if email and db.query(User).filter(User.email == email).first():
            raise HTTPException(status_code=400, detail="Email sudah digunakan")
        if phone and db.query(User).filter(User.phone == phone).first():
            raise HTTPException(status_code=400, detail="Nomor HP sudah digunakan")

        referrer_id = None
        if ref:
            ref_user = db.query(User).filter(User.ref_code == ref).first()
            if ref_user:
                referrer_id = ref_user.id

        new_ref_code = uuid.uuid4().hex[:8].upper()

        user = User(
            username=username,
            email=email,
            phone=phone,
            password_hash=hash_password(password),
            balance=Decimal("0.00"),
            ref_code=new_ref_code,
            referred_by=referrer_id,
            ref_earnings=Decimal("0.00")
        )

        db.add(user)
        try:
            db.commit()
            db.refresh(user)
            
            await create_lunexa_user(username)

        except IntegrityError:
            db.rollback()
            raise HTTPException(status_code=400, detail="Username / Contact sudah terdaftar")

        return {"msg": "Akun berhasil dibuat"}

    except HTTPException as he:
        raise he
    except Exception as e:
        print("REGISTER ERROR:", e)
        raise HTTPException(status_code=500, detail="Register error")
# ================= REFERRAL ENDPOINT =================

@app.get("/referralbe")
def get_referral_data(db: Session = Depends(get_db), current_user: User = Depends(get_current_user)):
    user = db.query(User).filter(User.id == current_user.id).first()
    
    if not user.ref_code:
        user.ref_code = uuid.uuid4().hex[:8].upper()
        db.commit()
        db.refresh(user)

    referred_users = db.query(User).filter(User.referred_by == user.id).all()
    
    user_list = []
        
    for u in referred_users:
        masked_phone = "Tidak ada"

        if u.phone:
            masked_phone = u.phone[:5] + "****"

        user_list.append({
            "username": u.username,
            "phone": masked_phone
        })

    total_referrals = len(user_list)
    base_url = "https://bola433.my.id/login?ref=" 
    
    return {
        "ref_code": user.ref_code,
        "ref_link": f"{base_url}{user.ref_code}",
        "total_referrals": total_referrals,
        "earnings": float(user.ref_earnings or 0),
        "joined_members": user_list 
    }



# ================= GOOGLE LOGIN =================

@app.get("/auth/google")
async def auth_google(request: Request):
    redirect_uri = "https://bola433.my.id/auth/google/callback"
    return await oauth.google.authorize_redirect(request, redirect_uri)


@app.get("/auth/google/callback")
async def auth_google_callback(request: Request, db: Session = Depends(get_db)):
    try:
        token = await oauth.google.authorize_access_token(request)

        user_info = token.get("userinfo")

        if not user_info:
            user_info = await oauth.google.parse_id_token(request, token)

        email = user_info["email"].lower()

        user = db.query(User).filter(User.email == email).first()

        if not user:

            username_base = email.split("@")[0]
            username = username_base
            counter = 1

            while db.query(User).filter(User.username == username).first():
                username = f"{username_base}{counter}"
                counter += 1

            user = User(
                username=username,
                email=email,
                password_hash="google_login",
                provider="google",
                balance=Decimal("0.00"),
                ref_code=uuid.uuid4().hex[:8].upper()
            )

            db.add(user)
            db.commit()
            db.refresh(user)

            await create_lunexa_user(username)

        jwt_token = create_access_token({"sub": str(user.id)})
        
        response = RedirectResponse(url="/sportbook")

        response.set_cookie(
            key="token",
            value=jwt_token,
            httponly=False,  
            secure=True,     # Wajib True kalau pake samesite="none"
            samesite="lax",  # Gunakan "lax" biar cookie ikut saat redirect dari Google ke bola433
            max_age=86400,   # Berlaku 24 jam
            path="/"         # Wajib ada biar kebaca di semua path /api/
        )

        return response

    except Exception as e:
        print("GOOGLE LOGIN ERROR:", e)
        return RedirectResponse("/login?error=google_login_failed")
        
# ================= LOGIN =================
@app.post("/login")
async def login(req: Request, db: Session = Depends(get_db)):

    try:
        body = await req.json()

        username = body.get("username")
        password = body.get("password")

        if not username or not password:
            raise HTTPException(
                status_code=400,
                detail="Username dan password wajib diisi"
            )

        user = db.query(User).filter(
           or_(
                User.username == username,
                User.email == username,
                User.phone == username
            )
        ).first()


        if not user:
            raise HTTPException(
                status_code=400,
                detail="User tidak ditemukan"
            )

        if not verify_password(password, user.password_hash):
            raise HTTPException(
                status_code=400,
                detail="Password salah"
            )

        token = create_access_token(
            {"sub": str(user.id)}
        )

        return {
            "access_token": token,
            "token_type": "bearer"
        }

    except Exception as e:
        print("LOGIN ERROR:", e)
        raise e

@app.post("/logout")
async def logout(response: Response):
    response.delete_cookie(key="token", path="/", samesite="lax")
    return {"message": "Logged out"}
    
@app.get("/api/me")
def get_me(current_user: User = Depends(get_current_user)):
    return {
        "id": current_user.id,
        "username": current_user.username,
        "role": current_user.role
    }
    
# ================= BALANCE =================

@app.get("/balance")
def get_balance(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):

    return {
        "balance": current_user.balance
    }

# ================= DEPOSIT =================
class DepositRequest(BaseModel):
    amount: int
    method: str

@app.post("/deposit")
async def deposit(
    data: DepositRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    method_key = data.method.upper().strip()
    method_config = PAYMENT_METHODS.get(method_key)

    if not method_config:
        raise HTTPException(status_code=400, detail="Metode tidak valid")

    payload = {
        "appId": APP_ID,
        "merOrderNo": str(uuid.uuid4()),
        "notifyUrl": "https://bola433.my.id/webhook/starpago",

        "currency": "IDR",
        "amount": str(data.amount),
        "payMethod": method_config["payMethod"],

        "extra": {
            "accountName": current_user.username or "USER",
            "accountNo": current_user.phone or "0810000000",
            "bankCode": method_config["channelCode"],
            "email": current_user.email or "user@gmail.com",
            "mobile": current_user.phone or "0810000000"
        },

        "returnUrl": "/",
        "attach": "StarPago"
    }

    try:
        sign, raw_string = generate_sign(payload, APP_SECRET)
        payload["sign"] = sign

        print("SIGN STRING:", raw_string)
        print("SIGN:", sign)

        async with httpx.AsyncClient() as client:
            response = await client.post(
                STARPAGO_URL,
                json=payload
            )

        result = response.json()
        print("STARPAGO RESPONSE:", result)

        if result.get("code") == 200:
            new_depo = Deposit(
                user_id=current_user.id,
                amount=data.amount,
                method=method_key,
                reference=payload["merOrderNo"],
                status="pending"
            )
            db.add(new_depo)
            db.commit()

        return result

    except Exception as e:
        print("ERROR:", str(e))
        raise HTTPException(status_code=500, detail="Deposit error")

@app.post("/webhook/starpago")
async def starpago_callback(req: Request, db: Session = Depends(get_db)):

    body = await req.json()

    received_sign = body.get("sign")
    expected_sign, _ = generate_sign(body, APP_SECRET)

    if received_sign != expected_sign:
        return {"msg": "Invalid sign"}

    order_id = body.get("merOrderNo")
    status = body.get("status", "").lower()

    deposit = db.query(Deposit).filter(Deposit.reference == order_id).first()

    if not deposit:
        return {"msg": "Not found"}

    if deposit.status == "paid":
        return {"msg": "Already processed"}

    if status == "success":
        deposit.status = "paid"
        user = db.query(User).filter(User.id == deposit.user_id).with_for_update().first()
        
        if user:
            user.balance += Decimal(str(deposit.amount))
            
            try:
                res = await lunexa_deposit(user.username, int(deposit.amount))
                
                if res and res.get("status") == 1:
                    user.balance -= Decimal(str(deposit.amount))
                    print(f"AUTO DEPO LUNEXA SUCCESS: {user.username}")
            except Exception as e:
                print(f"AUTO DEPO LUNEXA FAILED: {e}")

    db.commit()
    return {"msg": "OK"}
# ================= WITHDRAW REQUEST =================

@app.post("/withdraw")
async def withdraw(
    req: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    try:
        body = await req.json()
        amount = Decimal(str(body.get("amount", "0")))
        method = body.get("method")
        account = body.get("account")
    except (InvalidOperation, Exception):
        raise HTTPException(status_code=400, detail="Format amount salah")

    if amount <= 0:
        raise HTTPException(status_code=400, detail="Amount harus > 0")
    if not method or not account:
        raise HTTPException(status_code=400, detail="Metode dan akun wajib diisi")

    user = db.query(User).filter(User.id == current_user.id).with_for_update().first()
    if user.balance < amount:
        raise HTTPException(status_code=400, detail="Saldo tidak cukup")

    user.balance -= amount 

    new_withdraw = Withdraw(
        user_id=user.id,
        amount=amount,
        method=method,
        account=account,
        status=WithdrawStatus.pending,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow()
    )

    db.add(new_withdraw)
    db.commit() 
    db.refresh(new_withdraw)
    
    message = f"""
<b>🔔 REQUEST WITHDRAW</b>

<b>ID:</b> <code>{new_withdraw.id}</code>
<b>User:</b> {user.username}
<b>Jumlah:</b> Rp {amount:,.0f}
<b>Metode:</b> {method} ({account})

<b>Aksi Manual:</b>
Setujui: <code>/approve {new_withdraw.id}</code>
Tolak: <code>/reject {new_withdraw.id}</code>
Paid: <code>/paid {new_withdraw.id}</code>
"""

    return {
        "msg": "Withdraw pending, menunggu konfirmasi admin",
        "withdraw_id": new_withdraw.id,
        "new_balance": float(user.balance)
    }

async def process_withdraw_update(wid: int, action: str, db: Session):

    withdraw = db.query(Withdraw).filter(Withdraw.id == wid).first()
    if not withdraw:
        return f"❌ ID {wid} tidak ditemukan"

    user = db.query(User).filter(User.id == withdraw.user_id).first()
    if not user:
        return f"❌ User tidak ditemukan"

    if action == "approve":

        if withdraw.status != WithdrawStatus.pending:
            return f"⚠️ WD {wid} tidak bisa di approve"

        withdraw.status = WithdrawStatus.approved
        msg = f"✅ WD ID {wid} DISETUJUI"

    elif action == "paid":

        if withdraw.status == WithdrawStatus.paid:
            return f"⚠️ WD {wid} sudah dibayar"

        if withdraw.status != WithdrawStatus.approved:
            return f"⚠️ WD {wid} harus APPROVED dulu"

        withdraw.status = WithdrawStatus.paid
        msg = f"💸 WD ID {wid} SUDAH DIBAYAR"

    elif action == "reject":

        if withdraw.status not in [WithdrawStatus.pending, WithdrawStatus.approved]:
            return f"⚠️ WD {wid} tidak bisa ditolak"

        withdraw.status = WithdrawStatus.rejected
        user.balance += withdraw.amount

        msg = f"❌ WD ID {wid} DITOLAK & SALDO DIKEMBALIKAN"

    withdraw.updated_at = datetime.utcnow()
    db.commit()

    return msg

    
# ================= TELEGRAM WEBHOOK HANDLER =================

async def handle_telegram_update_webhook(update: dict, db: Session):
    message = update.get("message")
    if not message or "text" not in message:
        return

    text = message.get("text").strip()
    
    if text.startswith("/approve") or text.startswith("/reject") or text.startswith("/paid"):
        parts = text.split()
        if len(parts) < 2:
            send_telegram_message("❌ Format salah. Contoh: <code>/approve 10</code>")
            return

        command = parts[0].replace("/", "")
        try:
            wid = int(parts[1])
            response_msg = await process_withdraw_update(wid, command, db)
            send_telegram_message(response_msg)
        except ValueError:
            send_telegram_message("❌ ID harus berupa angka!")

@app.post("/bot/telegram-webhook")
async def telegram_webhook(
    req: Request,
    db: Session = Depends(get_db)
):
    try:
        update = await req.json()
        await handle_telegram_update_webhook(update, db)
        return {"ok": True}
    except Exception as e:
        print(f"Error Webhook: {e}")
        return {"ok": False}

@app.get("/withdraw/status/{withdraw_id}")
def withdraw_status(
    withdraw_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    withdraw = db.query(Withdraw).filter(
        Withdraw.id == withdraw_id,
        Withdraw.user_id == current_user.id
    ).first()

    if not withdraw:
        raise HTTPException(status_code=404, detail="Withdraw tidak ditemukan")

    return {
        "status": withdraw.status
    }
    
# ================= TRANSACTION HISTORY =================

@app.get("/transactions")
async def get_transactions(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):

    deposits = db.query(Deposit).filter(
        Deposit.user_id == current_user.id
    ).all()

    withdraws = db.query(Withdraw).filter(
        Withdraw.user_id == current_user.id
    ).all()

    history = []

    for d in deposits:
        history.append({
            "type": "deposit",
            "amount": float(d.amount),
            "method": d.method,
            "status": d.status,
            "date": d.created_at
        })

    for w in withdraws:
        history.append({
            "type": "withdraw",
            "amount": float(w.amount),
            "method": w.method,
            "status": w.status,
            "date": w.created_at
        })
        
    history.sort(key=lambda x: x["date"], reverse=True)

    return history
 
    
# ================= USER INFO =================

@app.get("/api/user/me")
async def get_me(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    try:
        user = db.query(User).filter(User.id == current_user.id).first()

        return {
            "id": user.id,
            "username": user.username,
            "balance": float(user.balance),
            "profile_pic": user.profile_pic or "default.png"
        }

    except Exception as e:
        print(f"ERROR API ME: {str(e)}")
        return JSONResponse(status_code=500, content={"detail": "Internal Server Error"})




@app.post("/api/user/upload-profile")
async def upload_profile_pic(
    file: UploadFile = File(...), 
    db: Session = Depends(get_db), 
    current_user: User = Depends(get_current_user)
):
    
    allowed_extensions = ["jpg", "jpeg", "png"]
    file_ext = file.filename.split(".")[-1].lower()
    if file_ext not in allowed_extensions:
        raise HTTPException(status_code=400, detail="Hanya file JPG atau PNG bro")

    filename = f"profile_{current_user.id}_{uuid.uuid4().hex[:6]}.{file_ext}"
    upload_path = Path("static/uploads/profile")
    upload_path.mkdir(parents=True, exist_ok=True)
    
    file_path = upload_path / filename

    with file_path.open("wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    user_to_update = db.merge(current_user) 
    
    user_to_update.profile_pic = filename
    
    db.commit()      
    db.refresh(user_to_update) 

    return {"msg": "Foto profil berhasil diperbarui", "filename": filename}


    
# ================= UPDATE USER =================

class UpdateUsernameRequest(BaseModel):
    username: str
    
@app.put("/api/user/username")
def update_username(
    data: UpdateUsernameRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    
    user = db.query(User).filter(User.id == current_user.id).first()

    if not user:
        raise HTTPException(status_code=404, detail="User tidak ditemukan")

    existing = db.query(User).filter(
        User.username == data.username
    ).first()

    if existing and existing.id != user.id:
        raise HTTPException(status_code=400, detail="Username sudah dipakai")

    user.username = data.username
    db.commit()

    return {"message": "Username berhasil diupdate"}
    
# ================= UPDATE PASSWORD =================
class UpdatePasswordRequest(BaseModel):
    current_password: str
    new_password: str
    
@app.put("/api/user/password")
def update_password(
    data: UpdatePasswordRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    
    user = db.query(User).filter(User.id == current_user.id).first()

    if not user:
        raise HTTPException(status_code=404, detail="User tidak ditemukan")

    if not verify_password(data.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="Password lama salah")

    user.password_hash = hash_password(data.new_password)

    db.commit()

    return {"message": "Password berhasil diupdate"}
    
@app.get("/api/admin/users")
async def get_all_users(
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db)
):
    users = db.query(User).all()
    return [
        {
            "id": u.id,
            "username": u.username,
            "email": u.email,
            "phone": u.phone,
            "balance": float(u.balance),
            "ref_code": u.ref_code
        } for u in users
    ]

@app.delete("/api/admin/user/{user_id}")
async def admin_delete_user(admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User tidak ditemukan")
    
    db.delete(user)
    db.commit()
    return {"message": f"User {user.username} berhasil dihapus"}
    
# ================= DELETE USER =================

@app.delete("/api/user/delete")
async def delete_user(
    force: bool = False,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    user = db.query(User).filter(User.id == current_user.id).first()

    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    bets_count = db.query(Bet).filter(Bet.user_id == user.id).count()

    if bets_count > 0 and not force:
        return {
            "has_bets": True,
            "total_bets": bets_count
        }

    db.query(Bet).filter(Bet.user_id == user.id).delete()
    db.delete(user)
    db.commit()

    return {"msg": "Account deleted successfully"}
# ================= ODDS API =================
LEAGUE_MAP = {
	"soccer_uefa_europa_league": "UEFA Europa League",
	"soccer_uefa_europa_conference_league": "UEFA Europa Conference League",
	"soccer_uefa_champs_league": "UEFA Champions League",
    "soccer_epl": "Premier League",
    "soccer_spain_la_liga": "La Liga",
    "soccer_italy_serie_a": "Serie A",
    "soccer_germany_bundesliga": "Bundesliga",
    "soccer_france_ligue_one": "Ligue 1",
}
@app.get("/api/matches/{league}")
async def get_matches(league: str):
    if league not in LEAGUES:
        raise HTTPException(status_code=404, detail="League not supported")

    matches = ODDS_CACHE.get(league, [])

    enriched_matches = []
    for m in matches:
        enriched = {
            **m,
            "league_key": league,
            "league_name": LEAGUE_MAP.get(league, league)
        }
        enriched_matches.append(enriched)

    return enriched_matches

async def fetch_all_odds():
    print("[ODDS] Updating cache...")

    async with httpx.AsyncClient(timeout=15) as client:
        for league in LEAGUES:
            url = f"https://api.the-odds-api.com/v4/sports/{league}/odds"
            params = {
                "apiKey": ODDS_API_KEY,
                "regions": "eu",
                "markets": "h2h",
                "oddsFormat": "decimal"
            }

            try:
                response = await client.get(url, params=params)

                if response.status_code == 200:
                    ODDS_CACHE[league] = response.json()
                    print(f"[ODDS] Updated {league}")
                else:
                    print(f"[ODDS ERROR] {league} -> {response.text}")

            except Exception as e:
                print(f"[ODDS EXCEPTION] {league} -> {e}")

            await asyncio.sleep(1.2) 

    print("[ODDS] Cache update done")

# ================= PLACE BET (UPDATED & SECURE) =================
@app.post("/place-bet")
async def place_bet(
    req: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    try:
        body = await req.json()

        try:
            stake = Decimal(str(body.get("stake", "0")))
        except InvalidOperation:
            raise HTTPException(status_code=400, detail="Stake tidak valid")

        pick = body.get("pick")
        sport_key = body.get("sport_key")
        event_id = body.get("event_id")

        if not event_id or not pick:
            raise HTTPException(status_code=400, detail="Data tidak lengkap")

        if stake <= 0:
            raise HTTPException(status_code=400, detail="Stake harus > 0")

        league_data = ODDS_CACHE.get(sport_key)
        if not league_data:
            raise HTTPException(status_code=400, detail="League tidak ditemukan")

        event_data = next((e for e in league_data if e["id"] == event_id), None)
        if not event_data:
            raise HTTPException(status_code=400, detail="Event tidak valid")

        match_time = datetime.fromisoformat(
            event_data["commence_time"].replace("Z", "+00:00")
        )

        if match_time < datetime.now(timezone.utc):
            raise HTTPException(status_code=400, detail="Match sudah dimulai")

        real_odds = None
        for bookmaker in event_data.get("bookmakers", []):
            for market in bookmaker.get("markets", []):
                if market.get("key") == "h2h":
                    for outcome in market.get("outcomes", []):
                        if outcome.get("name") == pick:
                            real_odds = outcome.get("price")
                            break

        if not real_odds:
            raise HTTPException(status_code=400, detail="Odds tidak ditemukan")

        odds = Decimal(str(real_odds))

        # 🔥 UPDATE SALDO (ATOMIC)
        updated = db.query(User).filter(
            User.id == current_user.id,
            User.balance >= stake
        ).update({
            User.balance: User.balance - stake
        })

        if updated == 0:
            raise HTTPException(status_code=400, detail="Saldo tidak cukup")

        # 🔥 SIMPAN BET
        new_bet = Bet(
            user_id=current_user.id,
            home=event_data.get("home_team"),
            away=event_data.get("away_team"),
            selected=pick,
            odds=odds,
            stake=stake,
            sport_key=sport_key,
            commence_time=match_time,
            event_id=event_id,
            status="pending"
        )

        db.add(new_bet)
        db.commit()

        # 🔥 ambil user terbaru dari DB
        user = db.query(User).filter(User.id == current_user.id).first()

    except Exception as e:
        db.rollback()
        print(f"ERROR PLACE BET: {e}")
        raise HTTPException(status_code=500, detail="Gagal memproses taruhan")

    # 🔔 TELEGRAM (optional)
    try:
        from bottele import send_telegram_message
        message = f"""
<b>🔔 BETTING DETECTED</b>

<b>User:</b> <code>{user.username}</code>
<b>Match:</b> {new_bet.home} vs {new_bet.away}
<b>Pick:</b> {new_bet.selected}
<b>Odds:</b> {new_bet.odds}
<b>Stake:</b> Rp {new_bet.stake:,.0f}
<b>New Balance:</b> Rp {user.balance:,.0f}
"""
        send_telegram_message(message)
    except:
        pass

    return {
        "msg": "Bet placed",
        "new_balance": float(user.balance) # Tambahin ini bro!
    }

# ================= BET HISTORY =================

@app.get("/bet-history")
async def bet_history(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):

    bets = db.query(Bet).filter(
        Bet.user_id == current_user.id
    ).order_by(Bet.id.desc()).all()

    return [
        {
            "id": bet.id,
            "home": bet.home,
            "away": bet.away,
            "selected": bet.selected,
            "odds": bet.odds,
            "stake": bet.stake,
            "status": bet.status,
            "commence_time": bet.commence_time,
            "league_key": bet.sport_key,
            "league_name": LEAGUE_MAP.get(bet.sport_key, bet.sport_key)
        }
        for bet in bets
    ]


# ================= UPDATE BET RESULT =================

async def update_bet_results(db: Session):

    pending_bets = db.query(Bet).filter(Bet.status == "pending").all()

    print(f"[AUTO UPDATE] Checking {len(pending_bets)} pending bets")

    if not pending_bets:
        return

    bets_by_league = {}

    for bet in pending_bets:
        bets_by_league.setdefault(bet.sport_key, []).append(bet)

    async with httpx.AsyncClient(timeout=10) as client:

        for sport_key, bets in bets_by_league.items():

            url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/scores"

            params = {
                "apiKey": ODDS_API_KEY,
                "daysFrom": 3
            }

            try:

                response = await client.get(url, params=params)

                if response.status_code != 200:
                    print("[FETCH FAILED]", response.text)
                    continue

                data = response.json()

            except Exception as e:
                print("[FETCH ERROR]", e)
                continue

            if not data:
                continue

            for bet in bets:

                event_result = next(
                    (ev for ev in data if ev.get("id") == bet.event_id),
                    None
                )

                if not event_result:
                    continue

                if not event_result.get("completed"):
                    continue

                scores = event_result.get("scores")

                if not scores:
                    continue

                try:

                    home_score = int(
                        next(s["score"] for s in scores if s["name"] == bet.home)
                    )

                    away_score = int(
                        next(s["score"] for s in scores if s["name"] == bet.away)
                    )

                except StopIteration:
                    continue

                payout = 0

                if home_score > away_score and bet.selected == bet.home:

                    bet.status = "win"
                    payout = bet.stake * bet.odds

                elif away_score > home_score and bet.selected == bet.away:

                    bet.status = "win"
                    payout = bet.stake * bet.odds

                elif home_score == away_score and bet.selected.lower() == "draw":

                    bet.status = "win"
                    payout = bet.stake * bet.odds

                else:

                    bet.status = "lose"

                    # ================= REFERRAL COMMISSION =================

                    user = db.query(User).filter(
                        User.id == bet.user_id
                    ).first()

                    if user and user.referred_by:

                        referrer = db.query(User).filter(
                            User.id == user.referred_by
                        ).first()

                        if referrer:

                            commission = bet.stake * Decimal("0.10")

                            referrer.balance += commission
                            referrer.ref_earnings += commission

                            print(
                                f"[REFERRAL] {referrer.username} earned {commission} from {user.username}"
                            )

                if bet.status == "win":

                    user = db.query(User).filter(
                        User.id == bet.user_id
                    ).first()

                    if user:
                        user.balance += payout

                print(
                    f"[RESULT] Bet {bet.id} -> {bet.status}, payout={payout}"
                )

            db.commit()
# ================= SCHEMA =================
class AdminLoginRequest(BaseModel):
    username: str
    password: str

# ================= ADMIN LOGIN =================
from fastapi.responses import JSONResponse
from auth import create_access_token, verify_password 

@app.post("/api/admin/login")
def admin_login(data: AdminLoginRequest, db: Session = Depends(get_db)):
    
    user = db.query(User).filter(User.username == data.username).first()

    if not user:
        raise HTTPException(status_code=401, detail="User tidak ditemukan")

    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Akses ditolak, Anda bukan admin")

    if not verify_password(data.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Password salah")

    jwt_token = create_access_token({"sub": str(user.id)})

    response = JSONResponse(content={
        "message": "Login berhasil",
        "role": user.role
    })
    
    response.set_cookie(
        key="token",
        value=jwt_token,
        httponly=True,
        secure=True, 
        samesite="none",
        max_age=172800
    )
    
    return response
# ================= ADMIN DASHBOARD =================
@app.get("/api/admin/dashboard-stats")
async def get_dashboard_stats(admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db)):
    now = datetime.now()
    t_start = datetime(now.year, now.month, now.day, 0, 0, 0)
    t_end = datetime(now.year, now.month, now.day, 23, 59, 59)
    
    # Statistik Utama
    total_depo = db.query(func.sum(Deposit.amount)).filter(Deposit.created_at >= t_start, Deposit.created_at <= t_end, Deposit.status == "success").scalar() or 0
    total_wd = db.query(func.sum(Withdraw.amount)).filter(Withdraw.created_at >= t_start, Withdraw.created_at <= t_end, Withdraw.status.in_(["approved", "paid"])).scalar() or 0
    
    # Logika Chart 7 Hari
    labels, deposits, withdraws = [], [], []
    for i in range(6, -1, -1):
        d = date.today() - timedelta(days=i)
        ds = datetime.combine(d, time.min)
        de = datetime.combine(d, time.max)
        
        dv = db.query(func.sum(Deposit.amount)).filter(Deposit.created_at >= ds, Deposit.created_at <= de, Deposit.status == "success").scalar() or 0
        wv = db.query(func.sum(Withdraw.amount)).filter(Withdraw.created_at >= ds, Withdraw.created_at <= de, Withdraw.status.in_(["approved", "paid"])).scalar() or 0
        
        labels.append(d.strftime("%d %b"))
        deposits.append(float(dv))
        withdraws.append(float(wv))

    recent_wd = db.query(Withdraw).order_by(Withdraw.id.desc()).limit(5).all()
    logs = []
    for r in recent_wd:
        u = db.query(User).filter(User.id == r.user_id).first()
        logs.append({"msg": f"WD {u.username if u else 'User'} - {r.status}", "time": r.created_at.strftime("%H:%M")})

    return {
        "deposit_today": float(total_depo),
        "withdraw_today": float(total_wd),
        "profit": float(total_depo - total_wd),
        "total_user": db.query(User).count(),
        "recent_logs": logs,
        "chart": {"labels": labels, "deposits": deposits, "withdraws": withdraws}
    }

    
@app.get("/api/admin/withdraws")
async def get_all_withdraws(admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db)):
    withdraws = db.query(Withdraw).order_by(Withdraw.id.desc()).all()
    
    result = []
    for w in withdraws:
        user = db.query(User).filter(User.id == w.user_id).first()
        result.append({
            "id": w.id,
            "username": user.username if user else "Unknown",
            "amount": float(w.amount),
            "method": w.method,
            "account": w.account,
            "status": w.status,
            "date": w.created_at.strftime("%H:%M") 
        })
    return result

# ================= ADMIN WHIDRAW =================

class WithdrawAction(BaseModel):
    withdraw_id: int
    action: str  # "approve", "reject", "paid"
    
@app.post("/api/admin/withdraw/action")
async def admin_withdraw_action(body: WithdrawAction,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db)):
    wid = body.withdraw_id
    action = body.action # 'approve', 'reject', atau 'paid'
    
    msg = await process_withdraw_update(wid, action, db)
    
    if "❌" in msg or "⚠️" in msg:
        raise HTTPException(status_code=400, detail=msg)
        
    return {"message": msg}
    
# ================= REFERRAL ENDPOINT (FINAL FIX) =================


@app.get("/api/referral/leaderboard")
async def get_referral_leaderboard(db: Session = Depends(get_db)):
    try:
        top_referrers = db.query(
            User.username,
            func.count(User.id).label("total_ref")
        ).filter(User.referred_by != None) \
         .group_by(User.referred_by) \
         .order_by(func.count(User.id).desc()) \
         .limit(10).all()

        leaderboard = []
        for index, row in enumerate(top_referrers):
            leaderboard.append({
                "rank": index + 1,
                "username": row.username[:3] + "***" if row.username else "User",
                "total_ref": row.total_ref
            })

        return leaderboard
    except Exception as e:
        print(f"Leaderboard Error: {e}")
        return []

# ================= GET ALL BETS (ADMIN) =================

@app.get("/api/admin/bets")
def get_all_bets(
    status: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    
    query = db.query(Bet).options(joinedload(Bet.user))

    if status and status != "all":
        query = query.filter(Bet.status == status)

    bets = query.order_by(Bet.created_at.desc()).all()

    result = []

    for bet in bets:
        payout = float((bet.stake or 0) * (bet.odds or 0))

        result.append({
            "id": bet.id,
            "time": bet.created_at.strftime("%H:%M") if bet.created_at else "-",
            "date": bet.created_at.strftime("%d/%m") if bet.created_at else "-",
            "username": bet.user.username if bet.user else "unknown",
            "match": f"{bet.home} vs {bet.away}",
            "pick": bet.selected, 
            "odds": float(bet.odds or 0),
            "stake": float(bet.stake or 0),
            "win_amount": payout,
            "status": bet.status 
        })

    return result

# ================= CHAT ENDPOINTS =================
class ChatCreate(BaseModel):
    message: str

@app.get("/api/admin/chat/messages")
async def get_messages(
    last_id: int = Query(0), 
    db: Session = Depends(get_db), 
    admin: User = Depends(get_admin_user)
):
    
    for _ in range(60):  
        msgs = db.query(ChatMessage).filter(ChatMessage.id > last_id).order_by(ChatMessage.id.asc()).all()
        
        if msgs:
            return [{"id": m.id, "name": m.username, "text": m.message, "time": m.created_at.strftime("%H:%M")} for m in msgs]
        
        await asyncio.sleep(0.5) 
    
    return [] 

@app.post("/api/admin/chat/send")
async def send_message(data: ChatCreate, db: Session = Depends(get_db), admin: User = Depends(get_admin_user)):
    new_msg = ChatMessage(username=admin.username, message=data.message)
    db.add(new_msg)
    db.commit()
    return {"status": "sent"}
    
# ================= UPDATE ODDS =================
async def auto_update_odds():
    while True:
        await fetch_all_odds()
        await asyncio.sleep(10800)  
# ================= MANUAL TRIGGER UPDATE BETS =================
@app.post("/update-bets")
async def trigger_update_bets(db: Session = Depends(get_db)):
    await update_bet_results(db)
    return {"msg": "Bets updated"}

# ================= LIVE STREAM SYNC (WeStream) =================

async def fetch_live_streams() -> None:
    global LIVE_CACHE
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{WESTREAM_BASE}{WESTREAM_MATCHES_ENDPOINT}")
            if resp.status_code == 200:
                data = resp.json()
                async with LIVE_CACHE_LOCK:
                    LIVE_CACHE = data if isinstance(data, list) else []
                print(f"[LIVE] Fetched {len(LIVE_CACHE)} live matches from WeStream")
            else:
                print(f"[LIVE ERROR] WeStream returned {resp.status_code}")
    except Exception as e:
        print(f"[LIVE EXCEPTION] {e}")


import re
import unicodedata

# ── Normalize: lowercase, strip aksen, buang non-alphanum ──────
def _norm(text: str) -> str:
    text = unicodedata.normalize("NFD", text.lower().strip())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^a-z0-9 ]", "", text)
    return re.sub(r"\s+", " ", text).strip()

# ── Mapping: Odds API name → semua kemungkinan nama WeStream ──
# Key   = nama PERSIS dari Odds API (sudah dinormalisasi)
# Value = list variasi yang mungkin muncul di WeStream title
TEAM_SYNC: dict[str, list[str]] = {
    # === EPL ===
    "brighton and hove albion": ["brighton", "brighton hove albion", "brighton & hove albion"],
    "wolverhampton wanderers": ["wolverhampton", "wolves", "wolverhampton wanderers"],
    "tottenham hotspur":       ["tottenham", "spurs", "tottenham hotspur"],
    "nottingham forest":       ["nottingham forest", "nottm forest", "notts forest"],
    "newcastle united":        ["newcastle", "newcastle united"],
    "manchester united":       ["manchester united", "man united", "man utd"],
    "manchester city":         ["manchester city", "man city"],
    "west ham united":         ["west ham", "west ham united"],
    "aston villa":             ["aston villa"],
    "crystal palace":          ["crystal palace"],

    # === La Liga ===
    "atletico madrid":         ["atletico madrid", "atletico de madrid", "atletico"],
    "ca osasuna":              ["osasuna", "ca osasuna"],
    "real sociedad":           ["real sociedad"],
    "real betis":              ["real betis", "betis"],
    "athletic bilbao":         ["athletic bilbao", "athletic club", "bilbao"],
    "alaves":                  ["alaves", "deportivo alaves"],
    "rayo vallecano":          ["rayo vallecano", "rayo"],
    "celta vigo":              ["celta vigo", "celta"],
    "elche cf":                ["elche"],

    # === Serie A ===
    "inter milan":             ["inter milan", "inter", "internazionale", "fc internazionale"],
    "ac milan":                ["ac milan", "milan"],
    "as roma":                 ["roma", "as roma"],
    "atalanta bc":             ["atalanta", "atalanta bc"],
    "hellas verona":           ["verona", "hellas verona"],

    # === Ligue 1 ===
    "paris saint germain":     ["psg", "paris saint germain", "paris saint-germain", "paris sg"],
    "as monaco":               ["monaco", "as monaco"],
    "olympique lyon":          ["lyon", "olympique lyonnais"],
    "rc lens":                 ["lens", "rc lens"],
    "stade rennais":           ["rennes", "stade rennais"],

    # === Bundesliga ===
    "borussia monchengladbach": ["monchengladbach", "gladbach", "borussia mg", "borussia monchengladbach"],
    "fsv mainz 05":            ["mainz", "fsv mainz", "mainz 05"],
    "tsg hoffenheim":          ["hoffenheim", "tsg hoffenheim", "1899 hoffenheim"],
    "vfb stuttgart":           ["stuttgart", "vfb stuttgart"],
    "sc freiburg":             ["freiburg", "sc freiburg"],
    "rb leipzig":              ["leipzig", "rb leipzig", "red bull leipzig"],
    "bayer leverkusen":        ["leverkusen", "bayer leverkusen"],
    "eintracht frankfurt":     ["frankfurt", "eintracht frankfurt"],
    "1 fc koln":               ["koln", "cologne", "fc koln", "fc cologne"],
    "1 fc heidenheim":         ["heidenheim", "fc heidenheim"],
    "vfl wolfsburg":           ["wolfsburg", "vfl wolfsburg"],
    "fc st pauli":             ["st pauli", "fc st pauli"],
    "union berlin":            ["union berlin", "fc union berlin"],
    "hamburger sv":            ["hamburg", "hamburger sv", "hsv"],
    "werder bremen":           ["werder bremen", "werder"],
    "augsburg":                ["augsburg", "fc augsburg"],
    "borussia dortmund":       ["dortmund", "borussia dortmund", "bvb"],
    "bayern munich":           ["bayern munich", "bayern", "fc bayern"],
}

# Pre-build reverse lookup: westream_name_norm → odds_name_norm
_REVERSE: dict[str, str] = {}
for odds_norm, variants in TEAM_SYNC.items():
    for v in variants:
        _REVERSE[_norm(v)] = odds_norm
    # juga map diri sendiri
    _REVERSE[odds_norm] = odds_norm


def _to_odds_norm(westream_name: str) -> str:
    """Kembalikan normalized odds-API name dari westream name, atau name itu sendiri kalau tidak ada di map."""
    n = _norm(westream_name)
    return _REVERSE.get(n, n)


def _team_match(odds_team: str, ws_home: str, ws_away: str) -> bool:
    """
    Cocokkan satu odds_team ke home atau away dari WeStream.
    Gunakan TEAM_SYNC → reverse lookup, lalu fallback substring.
    """
    odds_n = _norm(odds_team)
    # normalisasi lewat TEAM_SYNC kalau ada
    odds_canonical = _REVERSE.get(odds_n, odds_n)

    for ws_name in [ws_home, ws_away]:
        ws_canonical = _to_odds_norm(ws_name)
        if odds_canonical == ws_canonical:
            return True

        # Fallback: substring setelah normalisasi
        ws_n = _norm(ws_name)
        if odds_n in ws_n or ws_n in odds_n:
            return True

        # Fallback: token overlap (min 1 kata unik cocok)
        STOP = {"fc", "cf", "sc", "ac", "as", "de", "la", "united", "city",
                "real", "club", "sport", "sporting", "atletico", "the", "1", "05"}
        t1 = set(odds_n.split()) - STOP
        t2 = set(ws_n.split()) - STOP
        if t1 and t2 and (t1 & t2):
            return True

    return False


# ── Endpoint /api/live/check ── GANTI YANG LAMA DENGAN INI ───
@app.get("/api/live/check")
async def live_check():
    async with LIVE_CACHE_LOCK:
        live_matches = list(LIVE_CACHE)

    result: dict = {}

    for sport_key, matches in ODDS_CACHE.items():
        for m in matches:
            event_id: str = m.get("id", "")
            home: str = m.get("home_team", "")
            away: str = m.get("away_team", "")
            if not event_id or not home or not away:
                continue

            for lm in live_matches:
                # WeStream sekarang punya field teams.home.name / teams.away.name
                ws_home = lm.get("teams", {}).get("home", {}).get("name", "")
                ws_away = lm.get("teams", {}).get("away", {}).get("name", "")
                lm_title = lm.get("title", "")

                # Minimal salah satu tim harus cocok
                home_match = _team_match(home, ws_home, ws_away)
                away_match = _team_match(away, ws_home, ws_away)

                if home_match and away_match:
                    result[event_id] = {
                        "westream_match_id": lm.get("id"),
                        "title": lm_title,
                        "sources": lm.get("sources", []),
                        "category": lm.get("category", "football"),
                    }
                    break

    return result
    
@app.get("/api/live/streams/{westream_match_id}")
async def live_streams(westream_match_id: str):
    async with LIVE_CACHE_LOCK:
        match = next((m for m in LIVE_CACHE if m.get("id") == westream_match_id), None)

    if not match:
        raise HTTPException(status_code=404, detail="Live match not found in cache")

    sources = match.get("sources", [])
    if not sources:
        return []

    async with httpx.AsyncClient(timeout=10) as client:
        tasks = [
            client.get(f"{WESTREAM_BASE}/stream/{s['source']}/{s['id']}")
            for s in sources
        ]
        responses = await asyncio.gather(*tasks, return_exceptions=True)

    streams: list = []
    for resp in responses:
        if isinstance(resp, Exception):
            continue
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, list):
                streams.extend(data)

    return streams


async def auto_update_live():
    while True:
        await asyncio.sleep(120)
        await fetch_live_streams()
# ================= AUTO UPDATE SETIAP STARTUP =================
@app.on_event("startup")
async def startup_event():
    await fetch_all_odds()
    await fetch_live_streams()
    asyncio.create_task(auto_update_bets())
    asyncio.create_task(auto_update_odds())
    asyncio.create_task(auto_update_live())

async def auto_update_bets():
    while True:
        db = SessionLocal()
        try:
            await update_bet_results(db)
        finally:
            db.close()
        await asyncio.sleep(1800)
# ================= RUN =================
if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8888, reload=True)