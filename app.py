from __future__ import annotations
import os, uuid
from pathlib import Path
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from typing import Literal
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from pymongo import MongoClient, ASCENDING, DESCENDING
from pymongo.errors import DuplicateKeyError
import cloudinary, cloudinary.uploader
from dotenv import load_dotenv

load_dotenv()

MONGODB_URI=os.getenv('MONGODB_URI')
MONGODB_DB=os.getenv('MONGODB_DB','stokku')
if not MONGODB_URI: raise RuntimeError('MONGODB_URI wajib di-set.')
client=MongoClient(MONGODB_URI, serverSelectionTimeoutMS=5000)
db=client[MONGODB_DB]
products_c=db.products; colors_c=db.colors; variants_c=db.variants; moves_c=db.stock_moves; transactions_c=db.transactions

def now(): return datetime.now(timezone.utc)
def oid(): return uuid.uuid4().hex
def public(obj):
    if not obj:return None
    obj=dict(obj); obj['id']=str(obj.pop('_id')); return obj

def init_db():
    client.admin.command('ping')
    products_c.create_index([('name',ASCENDING),('model',ASCENDING)])
    colors_c.create_index([('product_id',ASCENDING),('name_key',ASCENDING)],unique=True)
    variants_c.create_index([('product_id',ASCENDING),('color_id',ASCENDING),('size_key',ASCENDING)],unique=True)
    transactions_c.create_index('invoice_no',unique=True)
    transactions_c.create_index([('created_at',DESCENDING)])

@asynccontextmanager
async def lifespan(_:FastAPI): init_db(); yield; client.close()
app=FastAPI(title='Stokku MongoDB',lifespan=lifespan)
app.add_middleware(CORSMiddleware,allow_origins=['*'],allow_methods=['*'],allow_headers=['*'])

def color_key(s): return s.strip().casefold()
def image_url(path): return path or None

def upload_image(data:bytes):
    if not all(os.getenv(k) for k in ('CLOUDINARY_CLOUD_NAME','CLOUDINARY_API_KEY','CLOUDINARY_API_SECRET')): raise HTTPException(500,'Konfigurasi Cloudinary belum lengkap.')
    cloudinary.config(cloud_name=os.environ['CLOUDINARY_CLOUD_NAME'],api_key=os.environ['CLOUDINARY_API_KEY'],api_secret=os.environ['CLOUDINARY_API_SECRET'],secure=True)
    r=cloudinary.uploader.upload(data,folder='stokku/products',resource_type='image')
    return r['secure_url'],r['public_id']

def delete_cloudinary(public_id):
    if public_id:
        try: cloudinary.uploader.destroy(public_id,resource_type='image')
        except Exception: pass

async def save_image(image:UploadFile):
    if image.content_type not in {'image/jpeg','image/png','image/webp'}: raise HTTPException(400,'Gambar harus JPG, PNG, atau WEBP.')
    data=await image.read()
    if len(data)>5*1024*1024: raise HTTPException(400,'Ukuran gambar maksimal 5 MB.')
    return upload_image(data)

class SizeCreate(BaseModel):
    size:str=Field(min_length=1,max_length=60); warehouse_qty:int=Field(ge=0); hpp:int=Field(ge=0); normal_price:int=Field(ge=0); minimum_price:int=Field(ge=0)
class ColorCreate(BaseModel): color:str=Field(min_length=1,max_length=60); color_hex:str|None=None; sizes:list[SizeCreate]=Field(min_length=1)
class ProductCreate(BaseModel): name:str=Field(min_length=1,max_length=100); model:str=Field(min_length=1,max_length=100); colors:list[ColorCreate]=Field(min_length=1)
class ProductUpdate(BaseModel): name:str=Field(min_length=1,max_length=100); model:str=Field(min_length=1,max_length=100)
class VariantCreate(BaseModel):
    product_id:str; color:str=Field(min_length=1,max_length=60); color_hex:str|None=None; size:str=Field(min_length=1,max_length=60); warehouse_qty:int=Field(ge=0); hpp:int=Field(ge=0); normal_price:int=Field(ge=0); minimum_price:int=Field(ge=0)
class VariantUpdate(BaseModel):
    color:str=Field(min_length=1,max_length=60); color_hex:str|None=None; size:str=Field(min_length=1,max_length=60); warehouse_qty:int=Field(ge=0); hpp:int=Field(ge=0); normal_price:int=Field(ge=0); minimum_price:int=Field(ge=0)
class TransferCreate(BaseModel): variant_id:str; qty:int=Field(gt=0)
class TransactionCreate(BaseModel): variant_id:str; qty:int=Field(gt=0); unit_price:int=Field(gt=0)
class TransactionLineCreate(TransactionCreate): pass
class TransactionBatchCreate(BaseModel): items:list[TransactionLineCreate]=Field(min_length=1)

def get_variant(vid):
    v=variants_c.find_one({'_id':vid})
    if not v: raise HTTPException(404,'Varian tidak ditemukan.')
    p=products_c.find_one({'_id':v['product_id']}); c=colors_c.find_one({'_id':v['color_id']})
    if not p or not c: raise HTTPException(500,'Data relasi varian rusak.')
    return v,p,c

def variant_out(v,p,c): return {'id':v['_id'],'product_id':v['product_id'],'product_name':p['name'],'model':p['model'],'color_id':v['color_id'],'image_url':p.get('image_path'),'color_hex':c.get('color_hex'),'color':c['name'],'size':v['size'],'warehouse_qty':v['warehouse_qty'],'sale_qty':v['sale_qty'],'hpp':v['hpp'],'normal_price':v['normal_price'],'minimum_price':v['minimum_price'],'created_at':v['created_at'].isoformat(),'updated_at':v['updated_at'].isoformat()}

@app.get('/api/health')
def health(): return {'status':'ok','database':'mongodb','image_storage':'cloudinary'}
@app.get('/api/products')
def products():
    return [{'id':p['_id'],'name':p['name'],'model':p['model'],'image_url':p.get('image_path')} for p in products_c.find().sort([('name',ASCENDING),('model',ASCENDING)])]
@app.post('/api/products')
def create_product(x:ProductCreate):
    seen=set()
    for c in x.colors:
        ck=color_key(c.color)
        if ck in seen: raise HTTPException(400,'Nama warna tidak boleh duplikat.')
        seen.add(ck); sizes=set()
        for s in c.sizes:
            if s.minimum_price>s.normal_price: raise HTTPException(400,'Harga minimum tidak boleh melebihi harga normal.')
            sk=color_key(s.size)
            if sk in sizes: raise HTTPException(400,f'Size pada warna {c.color} tidak boleh duplikat.')
            sizes.add(sk)
    pid=oid(); t=now(); products_c.insert_one({'_id':pid,'name':x.name.strip(),'model':x.model.strip(),'image_path':None,'image_public_id':None,'created_at':t})
    out=[]
    try:
      for c in x.colors:
        cid=oid(); colors_c.insert_one({'_id':cid,'product_id':pid,'name':c.color.strip(),'name_key':color_key(c.color),'color_hex':c.color_hex.strip() if c.color_hex else None,'created_at':t,'updated_at':t}); out.append({'id':cid,'name':c.color.strip(),'color_hex':c.color_hex})
        for s in c.sizes:
          vid=oid(); variants_c.insert_one({'_id':vid,'product_id':pid,'color_id':cid,'size':s.size.strip(),'size_key':color_key(s.size),'warehouse_qty':s.warehouse_qty,'sale_qty':0,'hpp':s.hpp,'normal_price':s.normal_price,'minimum_price':s.minimum_price,'created_at':t,'updated_at':t})
          if s.warehouse_qty:moves_c.insert_one({'_id':oid(),'variant_id':vid,'move_type':'stok_awal','qty':s.warehouse_qty,'note':'Produk baru dibuat','created_at':t})
    except Exception:
      created_vids=[v['_id'] for v in variants_c.find({'product_id':pid},{'_id':1})]
      moves_c.delete_many({'variant_id':{'$in':created_vids}})
      variants_c.delete_many({'product_id':pid})
      colors_c.delete_many({'product_id':pid})
      products_c.delete_one({'_id':pid})
      raise
    return {'id':pid,'colors':out,'message':'Produk beserta varian berhasil dibuat.'}
@app.patch('/api/products/{pid}')
def update_product(pid:str,x:ProductUpdate):
    r=products_c.update_one({'_id':pid},{'$set':{'name':x.name.strip(),'model':x.model.strip()}})
    if not r.matched_count: raise HTTPException(404,'Produk tidak ditemukan.')
    return {'message':'Produk berhasil diperbarui.'}
@app.delete('/api/products/{pid}')
def delete_product(pid:str):
    if not products_c.find_one({'_id':pid}): raise HTTPException(404,'Produk tidak ditemukan.')
    vids=[v['_id'] for v in variants_c.find({'product_id':pid},{'_id':1})]
    if transactions_c.find_one({'variant_id':{'$in':vids}}): raise HTTPException(400,'Produk tidak bisa dihapus karena sudah memiliki riwayat transaksi.')
    p=products_c.find_one({'_id':pid}); delete_cloudinary(p.get('image_public_id')); products_c.delete_one({'_id':pid}); colors_c.delete_many({'product_id':pid}); variants_c.delete_many({'product_id':pid}); moves_c.delete_many({'variant_id':{'$in':vids}})
    return {'message':'Produk berhasil dihapus.'}
@app.post('/api/products/{pid}/image')
async def product_image(pid:str,image:UploadFile=File(...)):
    p=products_c.find_one({'_id':pid})
    if not p: raise HTTPException(404,'Produk tidak ditemukan.')
    url,public_id=await save_image(image); old=p.get('image_public_id'); products_c.update_one({'_id':pid},{'$set':{'image_path':url,'image_public_id':public_id}}); delete_cloudinary(old)
    return {'image_url':url,'message':'Gambar utama produk berhasil disimpan.'}
@app.get('/api/variants')
def variants(location:Literal['warehouse','sale','all']='all'):
    q={} if location=='all' else {f'{location}_qty':{'$gt':0}}
    out=[]
    for v in variants_c.find(q):
      p=products_c.find_one({'_id':v['product_id']}); c=colors_c.find_one({'_id':v['color_id']})
      if p and c: out.append(variant_out(v,p,c))
    return sorted(out,key=lambda z:(z['product_name'].casefold(),z['model'].casefold(),z['color'].casefold(),z['size'].casefold()))
@app.post('/api/variants')
def create_variant(x:VariantCreate):
    if x.minimum_price>x.normal_price: raise HTTPException(400,'Harga minimum tidak boleh melebihi harga normal.')
    if not products_c.find_one({'_id':x.product_id}): raise HTTPException(404,'Produk tidak ditemukan.')
    c=colors_c.find_one({'product_id':x.product_id,'name_key':color_key(x.color)})
    t=now()
    if not c:
      cid=oid(); c={'_id':cid,'product_id':x.product_id,'name':x.color.strip(),'name_key':color_key(x.color),'color_hex':x.color_hex,'created_at':t,'updated_at':t}; colors_c.insert_one(c)
    else:
      cid=c['_id'];
      if x.color_hex: colors_c.update_one({'_id':cid},{'$set':{'color_hex':x.color_hex,'updated_at':t}})
    if variants_c.find_one({'product_id':x.product_id,'color_id':cid,'size_key':color_key(x.size)}): raise HTTPException(400,'Warna dan size ini sudah ada pada produk tersebut.')
    vid=oid(); variants_c.insert_one({'_id':vid,'product_id':x.product_id,'color_id':cid,'size':x.size.strip(),'size_key':color_key(x.size),'warehouse_qty':x.warehouse_qty,'sale_qty':0,'hpp':x.hpp,'normal_price':x.normal_price,'minimum_price':x.minimum_price,'created_at':t,'updated_at':t})
    if x.warehouse_qty:moves_c.insert_one({'_id':oid(),'variant_id':vid,'move_type':'stok_awal','qty':x.warehouse_qty,'note':'Varian baru dibuat','created_at':t})
    return {'id':vid,'color_id':cid,'message':'Varian berhasil ditambahkan ke Gudang.'}
@app.patch('/api/variants/{vid}')
def update_variant(vid:str,x:VariantUpdate):
    if x.minimum_price>x.normal_price: raise HTTPException(400,'Harga minimum tidak boleh melebihi harga normal.')
    v,p,_=get_variant(vid); t=now(); c=colors_c.find_one({'product_id':v['product_id'],'name_key':color_key(x.color)})
    if not c:
      cid=oid(); c={'_id':cid,'product_id':v['product_id'],'name':x.color.strip(),'name_key':color_key(x.color),'color_hex':x.color_hex,'created_at':t,'updated_at':t}; colors_c.insert_one(c)
    else: cid=c['_id']; colors_c.update_one({'_id':cid},{'$set':{'color_hex':x.color_hex or c.get('color_hex'),'updated_at':t}})
    dup=variants_c.find_one({'product_id':v['product_id'],'color_id':cid,'size_key':color_key(x.size),'_id':{'$ne':vid}})
    if dup: raise HTTPException(400,'Warna dan size ini sudah dipakai oleh varian lain.')
    diff=x.warehouse_qty-v['warehouse_qty']; variants_c.update_one({'_id':vid},{'$set':{'color_id':cid,'size':x.size.strip(),'size_key':color_key(x.size),'warehouse_qty':x.warehouse_qty,'hpp':x.hpp,'normal_price':x.normal_price,'minimum_price':x.minimum_price,'updated_at':t}})
    if diff:moves_c.insert_one({'_id':oid(),'variant_id':vid,'move_type':'update_stok_gudang','qty':diff,'note':'Update unit','created_at':t})
    return {'message':'Unit berhasil diperbarui.'}
@app.delete('/api/variants/{vid}')
def delete_variant(vid:str):
    v,p,c=get_variant(vid)
    if v['sale_qty']>0: raise HTTPException(400,'Varian tidak bisa dihapus selama masih ada stok jual.')
    if transactions_c.find_one({'variant_id':vid}): raise HTTPException(400,'Varian tidak bisa dihapus karena sudah memiliki riwayat transaksi.')
    variants_c.delete_one({'_id':vid}); moves_c.delete_many({'variant_id':vid})
    if not variants_c.find_one({'color_id':v['color_id']}): colors_c.delete_one({'_id':v['color_id']})
    return {'message':'Varian berhasil dihapus.'}
@app.delete('/api/colors/{cid}')
def delete_color(cid:str):
    c=colors_c.find_one({'_id':cid})
    if not c: raise HTTPException(404,'Warna tidak ditemukan.')
    if variants_c.find_one({'color_id':cid}): raise HTTPException(400,'Warna masih memiliki size/varian.')
    colors_c.delete_one({'_id':cid}); return {'message':'Warna berhasil dihapus.'}
@app.post('/api/transfers')
def transfer(x:TransferCreate):
    v,p,c=get_variant(x.variant_id)
    if v['warehouse_qty']<x.qty: raise HTTPException(400,'Stok Gudang tidak mencukupi.')
    r=variants_c.update_one({'_id':x.variant_id,'warehouse_qty':{'$gte':x.qty}},{'$inc':{'warehouse_qty':-x.qty,'sale_qty':x.qty},'$set':{'updated_at':now()}})
    if not r.modified_count: raise HTTPException(409,'Stok berubah, silakan coba lagi.')
    moves_c.insert_one({'_id':oid(),'variant_id':x.variant_id,'move_type':'pindah_ke_jual','qty':x.qty,'note':'Gudang ke Stok Jual','created_at':now()}); return {'message':'Barang berhasil dipindahkan ke Stok Jual.'}
def sale(items):
    # Validate the complete request before mutating stock.
    req={}
    for i in items: req[i.variant_id]=req.get(i.variant_id,0)+i.qty
    loaded={k:get_variant(k) for k in req}
    for vid,q in req.items():
        v,p,c=loaded[vid]
        if v['sale_qty']<q: raise HTTPException(400,f'Stok Jual {p["name"]} tidak mencukupi.')
    for i in items:
        v,p,c=loaded[i.variant_id]
        if not v['minimum_price']<=i.unit_price<=v['normal_price']:
            raise HTTPException(400,f'Harga {p["name"]} harus berada di rentang yang diizinkan.')

    batch=f'TRX-{datetime.now().strftime("%Y%m%d-%H%M%S")}-{uuid.uuid4().hex[:4].upper()}'
    completed=[]
    try:
        for n,i in enumerate(items,1):
            v,p,c=loaded[i.variant_id]; inv=batch if len(items)==1 else f'{batch}-{n:02d}'
            # Atomic conditional decrement prevents overselling under concurrency.
            r=variants_c.update_one({'_id':i.variant_id,'sale_qty':{'$gte':i.qty}}, {'$inc':{'sale_qty':-i.qty},'$set':{'updated_at':now()}})
            if not r.modified_count: raise HTTPException(409,'Stok berubah, silakan ulangi transaksi.')
            completed.append((i.variant_id,i.qty))
            total=i.qty*i.unit_price; tid=oid(); mid=oid()
            transactions_c.insert_one({'_id':tid,'invoice_no':inv,'variant_id':i.variant_id,'qty':i.qty,'unit_price':i.unit_price,'total':total,'hpp_snapshot':v['hpp'],'created_at':now()})
            moves_c.insert_one({'_id':mid,'variant_id':i.variant_id,'move_type':'penjualan','qty':-i.qty,'note':inv,'created_at':now()})
        return batch
    except Exception:
        # Compensating rollback for deployments without MongoDB replica-set transactions.
        for vid,qty in completed:
            variants_c.update_one({'_id':vid},{'$inc':{'sale_qty':qty},'$set':{'updated_at':now()}})
        # Remove records belonging to this batch that may have been inserted before failure.
        transactions_c.delete_many({'invoice_no':{'$regex':f'^{batch}'}})
        moves_c.delete_many({'note':{'$regex':f'^{batch}'},'move_type':'penjualan'})
        raise
@app.post('/api/transactions')
def transaction(x:TransactionCreate): return {'invoice_no':sale([x]),'message':'Transaksi berhasil disimpan.'}
@app.post('/api/transactions/batch')
def batch(x:TransactionBatchCreate): return {'invoice_no':sale(x.items),'item_count':len(x.items),'message':'Transaksi keranjang berhasil disimpan.'}
@app.get('/api/transactions')
def history():
    out=[]
    for t in transactions_c.find().sort('created_at',DESCENDING):
      v,p,c=get_variant(t['variant_id']); out.append({'id':t['_id'],'invoice_no':t['invoice_no'],'variant_id':t['variant_id'],'qty':t['qty'],'unit_price':t['unit_price'],'total':t['total'],'hpp_snapshot':t['hpp_snapshot'],'created_at':t['created_at'].isoformat(),'product_name':p['name'],'model':p['model'],'color':c['name'],'size':v['size'],'image_url':p.get('image_path')})
    return out
@app.get('/api/dashboard')
def dashboard():
    vs=list(variants_c.find()); wh=sum(v['warehouse_qty'] for v in vs); sq=sum(v['sale_qty'] for v in vs); capital=sum((v['warehouse_qty']+v['sale_qty'])*v['hpp'] for v in vs); today=datetime.now().astimezone().date().isoformat(); ts=[t for t in transactions_c.find() if t['created_at'].astimezone().date().isoformat()==today]
    recent=[]
    for t in transactions_c.find().sort('created_at',DESCENDING).limit(5):
      v,p,c=get_variant(t['variant_id']); recent.append({'invoice_no':t['invoice_no'],'qty':t['qty'],'unit_price':t['unit_price'],'total':t['total'],'created_at':t['created_at'].isoformat(),'product_name':p['name'],'model':p['model'],'color':c['name'],'size':v['size'],'image_url':p.get('image_path')})
    agg={}
    for t in transactions_c.find():
      v,p,c=get_variant(t['variant_id']); a=agg.setdefault(p['_id'],{'product_name':p['name'],'model':p['model'],'color':c['name'],'size':v['size'],'image_url':p.get('image_path'),'sold_qty':0,'revenue':0}); a['sold_qty']+=t['qty']; a['revenue']+=t['total']
    top=sorted(agg.values(),key=lambda a:(a['sold_qty'],a['revenue']),reverse=True)[:5]
    return {'warehouse_qty':wh,'sale_qty':sq,'capital':capital,'revenue_today':sum(t['total'] for t in ts),'sold_qty_today':sum(t['qty'] for t in ts),'profit_today':sum(t['total']-t['qty']*t['hpp_snapshot'] for t in ts),'transaction_count_today':len(ts),'recent_transactions':recent,'top_products':top}

# Serve the bundled frontend when deployed as a Python/FastAPI app (including Vercel).
BASE_DIR = Path(__file__).resolve().parent

@app.get('/', include_in_schema=False)
def frontend():
    index_file = BASE_DIR / 'index.html'
    if not index_file.is_file():
        raise HTTPException(500, 'index.html tidak ditemukan di deployment.')
    return FileResponse(index_file, media_type='text/html')

