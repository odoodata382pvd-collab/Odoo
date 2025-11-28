import os
import io
import logging
import pandas as pd
import ssl
import xmlrpc.client
import asyncio
import socket
import threading
from urllib.parse import urlparse
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
ODOO_URL_RAW = os.environ.get('ODOO_URL').rstrip('/') if os.environ.get('ODOO_URL') else None
if ODOO_URL_RAW and ODOO_URL_RAW.lower().endswith('/odoo'):
    ODOO_URL_FINAL = ODOO_URL_RAW[:-len('/odoo')]
else:
    ODOO_URL_FINAL = ODOO_URL_RAW

ODOO_DB = os.environ.get('ODOO_DB')
ODOO_USERNAME = os.environ.get('ODOO_USERNAME')
ODOO_PASSWORD = os.environ.get('ODOO_PASSWORD')

TARGET_MIN_QTY = 50
LOCATION_MAP = {
    'HN_STOCK_CODE': '201/201',
    'HCM_STOCK_CODE': '124/124',
    'HN_TRANSIT_NAME': 'Kho nhập Hà Nội',
}
PRIORITY_LOCATIONS = [
    LOCATION_MAP['HN_STOCK_CODE'],
    LOCATION_MAP['HN_TRANSIT_NAME'],
    LOCATION_MAP['HCM_STOCK_CODE'],
]
PRODUCT_CODE_FIELD = 'default_code'

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    level=logging.INFO)
logger = logging.getLogger(__name__)

def keep_port_open():
    try:
        s = socket.socket()
        s.bind(("0.0.0.0", 10000))
        s.listen(1)
        while True:
            conn, _ = s.accept()
            conn.close()
    except:
        pass

threading.Thread(target=keep_port_open, daemon=True).start()

def connect_odoo():
    try:
        if not ODOO_URL_FINAL:
            return None, None, "ODoo URL không thiết lập."
        common = xmlrpc.client.ServerProxy(
            f"{ODOO_URL_FINAL}/xmlrpc/2/common",
            context=ssl._create_unverified_context()
        )
        uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD, {})
        if not uid:
            return None, None, "Sai DB/User/Pass"
        models = xmlrpc.client.ServerProxy(
            f"{ODOO_URL_FINAL}/xmlrpc/2/object",
            context=ssl._create_unverified_context()
        )
        return uid, models, "OK"
    except Exception as e:
        return None, None, str(e)

def find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD):
    out = {}
    def search(key):
        res = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'stock.location','search_read',
            [[('display_name','ilike',key)]],
            {'fields':['id','display_name']}
        )
        if not res:
            return None
        for l in res:
            if key.lower() in l['display_name'].lower():
                return {'id':l['id'],'name':l['display_name']}
        l=res[0]
        return {'id':l['id'],'name':l['display_name']}
    hn=search(LOCATION_MAP['HN_STOCK_CODE'])
    if hn: out['HN_STOCK']=hn
    hcm=search(LOCATION_MAP['HCM_STOCK_CODE'])
    if hcm: out['HCM_STOCK']=hcm
    tran=search(LOCATION_MAP['HN_TRANSIT_NAME'])
    if tran: out['HN_TRANSIT']=tran
    return out

def escape_markdown(t):
    for c in ['\\','_','*','[',']','(',')','~','`','>','#','+','-','=','|','{','}','.','!']:
        t=t.replace(c,f"\\{c}")
    return t.replace("\\`","`")
def get_stock_data():
    uid, models, err = connect_odoo()
    if not uid:
        return None,0,err

    try:
        location_ids = find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD)
        if len(location_ids)<3:
            return None,0,"Không đủ kho"

        hn_id = location_ids['HN_STOCK']['id']
        tran_id = location_ids['HN_TRANSIT']['id']
        hcm_id = location_ids['HCM_STOCK']['id']

        quant = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'stock.quant','search_read',
            [[('location_id','in',[hn_id,tran_id,hcm_id]),('available_quantity','>',0)]],
            {'fields':['product_id','location_id','available_quantity']}
        )

        if not quant:
            empty = pd.DataFrame(columns=[
                'Mã SP','Tên SP','Tồn Kho HN','Tồn Kho HCM','Kho Nhập HN','Số Lượng Đề Xuất'
            ])
            buf=io.BytesIO()
            empty.to_excel(buf,index=False,sheet_name="DeXuatKeoHang")
            buf.seek(0)
            return buf,0,"OK"

        stock_map={}
        for q in quant:
            pid = q['product_id'][0]
            loc = q['location_id'][0]
            qty = q['available_quantity']
            if pid not in stock_map:
                stock_map[pid]={'hn':0,'tran':0,'hcm':0}
            if loc==hn_id: stock_map[pid]['hn']+=qty
            elif loc==tran_id: stock_map[pid]['tran']+=qty
            elif loc==hcm_id: stock_map[pid]['hcm']+=qty

        pids = list(stock_map.keys())
        products = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'product.product','search_read',
            [[('id','in',pids)]],
            {'fields':['id','display_name',PRODUCT_CODE_FIELD]}
        )
        pmap={p['id']:p for p in products}

        rows=[]
        for pid,vals in stock_map.items():
            if pid not in pmap: continue
            info=pmap[pid]
            code=info.get(PRODUCT_CODE_FIELD,'')
            name=info.get('display_name','')
            hn=int(vals['hn'])
            tran=int(vals['tran'])
            hcm=int(vals['hcm'])
            total_hn=hn+tran
            need=max(TARGET_MIN_QTY-total_hn,0)
            dex=min(need,hcm)
            if dex>0:
                rows.append({
                    'Mã SP':code,
                    'Tên SP':name,
                    'Tồn Kho HN':hn,
                    'Tồn Kho HCM':hcm,
                    'Kho Nhập HN':tran,
                    'Số Lượng Đề Xuất':dex
                })

        df=pd.DataFrame(rows)
        cols=['Mã SP','Tên SP','Tồn Kho HN','Tồn Kho HCM','Kho Nhập HN','Số Lượng Đề Xuất']
        if not df.empty:
            df=df[cols]
        else:
            df=pd.DataFrame(columns=cols)

        buf=io.BytesIO()
        df.to_excel(buf,index=False,sheet_name="DeXuatKeoHang")
        buf.seek(0)
        return buf,len(df),"OK"
    except Exception as e:
        return None,0,str(e)
def _read_po_with_auto_header(file_bytes: bytes):
    try:
        tmp=pd.read_excel(io.BytesIO(file_bytes),header=None)
    except Exception as e:
        return None,f"Không đọc được file: {e}"
    idx=0
    for i in range(len(tmp)):
        row=" ".join(tmp.iloc[i].astype(str).str.lower())
        if any(k in row for k in ["model","mã sp","ma sp","mã hàng","ma hang"]):
            idx=i
            break
    try:
        df=pd.read_excel(io.BytesIO(file_bytes),header=idx)
        return df,None
    except Exception as e:
        return None,f"Lỗi header dòng {idx+1}: {e}"

def _detect_po_columns(df):
    cols={c:str(c).lower().strip() for c in df.columns}
    code=None
    for c,v in cols.items():
        if v=="model": code=c; break
    if not code:
        for c,v in cols.items():
            if v.strip()=="model": code=c; break
    def find(keys):
        for c,v in cols.items():
            for k in keys:
                if k in v: return c
        return None
    if not code:
        code=find(["mã sp","ma sp","mã hàng","ma hang"])
    qty=find(["sl","số lượng","so luong"])
    recv=find(["đv nhận","dv nhận","đơn vị nhận"])
    return code,qty,recv

def _get_stock_for_product_with_cache(models,uid,pid,loc_ids,cache):
    if pid in cache: return cache[pid]
    def q(loc):
        if not loc:return 0
        r=models.execute_kw(
            ODOO_DB,uid,ODOO_PASSWORD,
            'product.product','read',
            [[pid]],
            {'fields':['qty_available'],'context':{'location':loc}}
        )
        if r and r[0]:return int(round(r[0].get('qty_available',0)))
        return 0
    data={
        'hn':q(loc_ids['HN_STOCK']['id']),
        'transit':q(loc_ids['HN_TRANSIT']['id']),
        'hcm':q(loc_ids['HCM_STOCK']['id'])
    }
    cache[pid]=data
    return data

def process_po_and_build_report(file_bytes: bytes):
    df,err=_read_po_with_auto_header(file_bytes)
    if df is None: return None,err
    if df.empty: return None,"File rỗng"

    code_col,qty_col,recv_col=_detect_po_columns(df)
    if not code_col or not qty_col or not recv_col:
        return None,"Không tìm được cột bắt buộc"

    df=df[[code_col,qty_col,recv_col]]
    df.columns=['Mã SP','SL cần giao','ĐV nhận']
    df['Mã SP']=df['Mã SP'].astype(str).str.upper().str.strip()
    df['SL cần giao']=pd.to_numeric(df['SL cần giao'],errors='coerce').fillna(0)
    df=df[(df['Mã SP']!="")&(df['SL cần giao']>0)]
    if df.empty: return None,"Không có dòng hợp lệ"

    uid,models,err=connect_odoo()
    if not uid: return None,err

    codes=sorted(df['Mã SP'].unique().tolist())
    prds=models.execute_kw(
        ODOO_DB,uid,ODOO_PASSWORD,
        'product.product','search_read',
        [[(PRODUCT_CODE_FIELD,'in',codes)]],
        {'fields':['id','display_name',PRODUCT_CODE_FIELD]}
    )
    cmap={}
    for p in prds:
        c=str(p.get(PRODUCT_CODE_FIELD,"")).upper()
        cmap[c]=p

    loc_ids=find_required_location_ids(models,uid,ODOO_DB,ODOO_PASSWORD)
    if len(loc_ids)<2:
        return None,"Thiếu kho"

    cache={}
    rows=[]
    for _,r in df.iterrows():
        code=r['Mã SP']
        need=int(r['SL cần giao'])
        recv=r['ĐV nhận']
        prod=cmap.get(code)
        if not prod:
            rows.append({
                'Mã SP':code,'Tên SP':'KHÔNG TÌM THẤY','ĐV nhận':recv,
                'SL cần giao':need,'Tồn HN':0,'Tồn Kho Nhập':0,'Tổng tồn HN':0,
                'Tồn HCM':0,'Trạng thái':'KHÔNG TÌM THẤY MÃ','SL cần kéo từ HCM':0,'SL thiếu':need
            })
            continue

        pid=prod['id']
        name=prod['display_name']
        stock=_get_stock_for_product_with_cache(models,uid,pid,loc_ids,cache)
        hn,tran,hcm=stock['hn'],stock['transit'],stock['hcm']
        total=hn+tran
        pull=0; shortage=0
        if need<=hn:
            st="ĐỦ tại kho HN (201/201)"
        elif need<=total:
            st="ĐỦ (HN + Kho nhập HN)"
        else:
            req=need-total
            if req<=hcm:
                pull=req; st="CẦN KÉO HÀNG TỪ HCM"
            else:
                pull=hcm; shortage=req-hcm
                st="THIẾU DÙ ĐÃ KÉO TỐI ĐA TỪ HCM"

        rows.append({
            'Mã SP':code,'Tên SP':name,'ĐV nhận':recv,'SL cần giao':need,
            'Tồn HN':hn,'Tồn Kho Nhập':tran,'Tổng tồn HN':total,
            'Tồn HCM':hcm,'Trạng thái':st,'SL cần kéo từ HCM':pull,'SL thiếu':shortage
        })

    out=pd.DataFrame(rows)
    order=['Mã SP','Tên SP','ĐV nhận','SL cần giao','Tồn HN','Tồn Kho Nhập','Tổng tồn HN',
           'Tồn HCM','Trạng thái','SL cần kéo từ HCM','SL thiếu']
    for c in order:
        if c not in out: out[c]=""
    out=out[order]
    buf=io.BytesIO()
    out.to_excel(buf,index=False,sheet_name="KiemTraPO")
    buf.seek(0)
    return buf,None
async def handle_product_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code=update.message.text.strip().upper()
    await update.message.reply_text(f"Đang tra tồn `{code}`...",parse_mode="Markdown")

    uid,models,err=connect_odoo()
    if not uid:
        await update.message.reply_text(f"Lỗi: {escape_markdown(err)}",parse_mode="Markdown")
        return

    try:
        locs=find_required_location_ids(models,uid,ODOO_DB,ODOO_PASSWORD)
        hn_id=locs.get('HN_STOCK',{}).get('id')
        tr_id=locs.get('HN_TRANSIT',{}).get('id')
        hc_id=locs.get('HCM_STOCK',{}).get('id')

        prd=models.execute_kw(
            ODOO_DB,uid,ODOO_PASSWORD,
            'product.product','search_read',
            [[(PRODUCT_CODE_FIELD,'=',code)]],
            {'fields':['id','display_name']}
        )
        if not prd:
            await update.message.reply_text(f"Không tìm thấy mã `{code}`")
            return
        pid=prd[0]['id']
        name=prd[0]['display_name']

        def g(loc):
            if not loc:return 0
            r=models.execute_kw(
                ODOO_DB,uid,ODOO_PASSWORD,
                'product.product','read',
                [[pid]],
                {'fields':['qty_available'],'context':{'location':loc}}
            )
            if r and r[0]:return int(round(r[0].get('qty_available',0)))
            return 0

        hn=g(hn_id); tr=g(tr_id); hc=g(hc_id)

        quant=models.execute_kw(
            ODOO_DB,uid,ODOO_PASSWORD,
            'stock.quant','search_read',
            [[('product_id','=',pid),('available_quantity','>',0)]],
            {'fields':['location_id','available_quantity']}
        )

        lid=list({q['location_id'][0] for q in quant})
        if lid:
            locinfo=models.execute_kw(
                ODOO_DB,uid,ODOO_PASSWORD,
                'stock.location','read',
                [lid],
                {'fields':['id','display_name','complete_name']}
            )
        else:
            locinfo=[]
        mmap={l['id']:l for l in locinfo}

        detail={}
        for q in quant:
            loc=q['location_id'][0]
            qty=int(q['available_quantity'])
            nm=mmap.get(loc,{}).get('complete_name') or mmap.get(loc,{}).get('display_name') or f"ID:{loc}"
            detail[nm]=detail.get(nm,0)+qty

        # SẮP XẾP CHUẨN
        pri_list=[]
        oth=[]
        for nm,qty in detail.items():
            added=False
            for p in PRIORITY_LOCATIONS:
                if p.lower() in nm.lower():
                    pri_list.append((nm,qty))
                    added=True
                    break
            if not added:
                oth.append((nm,qty))
        oth=sorted(oth,key=lambda x: x[0])
        final_list=pri_list+oth

        total_hn=hn+tr
        rec=0
        if total_hn<TARGET_MIN_QTY:
            need=TARGET_MIN_QTY-total_hn
            rec=min(need,hc)

        msg=f"""{code} {name}
Tồn kho HN: {hn}
Tồn kho HCM: {hc}
Tồn kho nhập Hà Nội: {tr}
=> đề xuất nhập thêm {rec} sp để HN đủ tồn {TARGET_MIN_QTY} sp.

2/ Tồn kho chi tiết (Có hàng):"""

        if final_list:
            for n,q in final_list:
                msg+=f"\n{n}: {q}"
        else:
            msg+="\nKhông có tồn chi tiết."

        await update.message.reply_text(msg.strip())
    except Exception as e:
        await update.message.reply_text(str(e))

async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Đang kiểm tra Odoo...")
    uid,_,err=connect_odoo()
    if uid:
        await update.message.reply_text(f"OK! DB: {ODOO_DB}")
    else:
        await update.message.reply_text(f"Lỗi: {err}")

async def excel_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Đang tạo báo cáo kéo hàng...")
    f,count,msg=get_stock_data()
    if f is None:
        await update.message.reply_text(f"Lỗi: {msg}")
        return
    if count>0:
        await update.message.reply_document(f,"de_xuat_keo_hang.xlsx",
                                            caption=f"Tìm thấy {count} SP cần kéo.")
    else:
        await update.message.reply_text("Không có SP cần kéo.")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u=update.message.from_user.first_name
    await update.message.reply_text(
        f"Chào {u}!\n- Gõ mã SP để tra tồn\n- /keohang tạo báo cáo\n- /checkpo kiểm tra PO\n- /ping test kết nối"
    )

async def checkpo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['waiting_for_po']=True
    await update.message.reply_text("Gửi file PO (.xlsx).")

async def handle_po_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('waiting_for_po'): return
    context.user_data['waiting_for_po']=False
    doc=update.message.document
    if not doc or not doc.file_name.lower().endswith(".xlsx"):
        await update.message.reply_text("Chỉ nhận file .xlsx")
        return
    await update.message.reply_text("Đang xử lý...")

    f=await doc.get_file()
    data=await f.download_as_bytearray()
    buf,err=process_po_and_build_report(bytes(data))
    if buf is None:
        await update.message.reply_text(f"Lỗi: {err}")
        return

    await update.message.reply_document(buf,"kiem_tra_po.xlsx","Đã xử lý PO!")

def main():
    if not TELEGRAM_TOKEN:
        logger.error("Thiếu token")
        return
    app=Application.builder().token(TELEGRAM_TOKEN).build()

    try:
        bot=Bot(token=TELEGRAM_TOKEN)
        asyncio.get_event_loop().run_until_complete(bot.delete_webhook())
    except: pass

    app.add_handler(CommandHandler("start",start_command))
    app.add_handler(CommandHandler("ping",ping_command))
    app.add_handler(CommandHandler("keohang",excel_report_command))
    app.add_handler(CommandHandler("checkpo",checkpo_command))
    app.add_handler(MessageHandler(filters.Document.ALL,handle_po_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND,handle_product_code))
    app.run_polling()

from http.server import BaseHTTPRequestHandler,HTTPServer

class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type","text/plain")
        self.end_headers()
        self.wfile.write(b"Bot alive")
    def log_message(self,*args): return

def start_http():
    try:
        s=HTTPServer(("0.0.0.0",10001),PingHandler)
        s.serve_forever()
    except: pass

threading.Thread(target=start_http,daemon=True).start()

if __name__=="__main__":
    main()
