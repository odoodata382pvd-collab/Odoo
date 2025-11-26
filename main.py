# main.py - Phiên bản phục hồi đầy đủ + sửa đúng 2 dòng cho "tồn kho chi tiết (có hàng)"
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

# ---------------- Config & Env ----------------
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
ODOO_URL_RAW = os.environ.get('ODOO_URL').rstrip('/') if os.environ.get('ODOO_URL') else None
if ODOO_URL_RAW and ODOO_URL_RAW.lower().endswith('/odoo'):
    ODOO_URL_FINAL = ODOO_URL_RAW[:-len('/odoo')]
else:
    ODOO_URL_FINAL = ODOO_URL_RAW

ODOO_DB = os.environ.get('ODOO_DB')
ODOO_USERNAME = os.environ.get('ODOO_USERNAME')
ODOO_PASSWORD = os.environ.get('ODOO_PASSWORD')
USER_ID_TO_SEND_REPORT = os.environ.get('USER_ID_TO_SEND_REPORT')

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

# ---------------- Logging ----------------
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------- Keep port open ----------------
def keep_port_open():
    try:
        s = socket.socket()
        s.bind(("0.0.0.0", 10000))
        s.listen(1)
        while True:
            conn, _ = s.accept()
            conn.close()
    except Exception:
        pass

threading.Thread(target=keep_port_open, daemon=True).start()

# ---------------- Odoo connect ----------------
def connect_odoo():
    try:
        if not ODOO_URL_FINAL:
            return None, None, "odoo url không được thiết lập."
        common_url = f'{ODOO_URL_FINAL}/xmlrpc/2/common'
        context = ssl._create_unverified_context()
        common = xmlrpc.client.ServerProxy(common_url, context=context)
        uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD, {})
        if not uid:
            return None, None, "Đăng nhập thất bại (uid=0). kiểm tra lại user/pass/db."
        models = xmlrpc.client.ServerProxy(f'{ODOO_URL_FINAL}/xmlrpc/2/object', context=context)
        return uid, models, "kết nối thành công."
    except Exception as e:
        return None, None, f"lỗi kết nối odoo xml-rpc: {e}"

# ---------------- Helpers ----------------
def find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD):
    location_ids = {}
    def search_location(name_code):
        data = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'stock.location', 'search_read',
            [[('display_name','ilike',name_code)]],
            {'fields':['id','display_name','complete_name']}
        )
        if not data: return None
        pref = next((l for l in data if name_code.lower() in l['display_name'].lower()), data[0])
        return {'id': pref['id'], 'name': pref.get('display_name')}

    hn = search_location(LOCATION_MAP['HN_STOCK_CODE'])
    if hn: location_ids['HN_STOCK'] = hn
    hcm = search_location(LOCATION_MAP['HCM_STOCK_CODE'])
    if hcm: location_ids['HCM_STOCK'] = hcm
    nhap = search_location(LOCATION_MAP['HN_TRANSIT_NAME'])
    if nhap: location_ids['HN_TRANSIT'] = nhap

    return location_ids

def escape_markdown(text):
    for c in ['\\','_','*','[',']','(',')','~','`','>','#','+','-','=','|','{','}','.','!']:
        text = str(text).replace(c,f'\\{c}')
    return text.replace('\\`','`')

# ---------------- Report /keohang ----------------
def get_stock_data():
    uid,models,msg = connect_odoo()
    if not uid:
        return None,0,msg
    try:
        loc = find_required_location_ids(models,uid,ODOO_DB,ODOO_PASSWORD)
        if len(loc)<3:
            return None,0,"không tìm thấy đủ 3 kho"

        qdata = models.execute_kw(
            ODOO_DB,uid,ODOO_PASSWORD,'stock.quant','search_read',
            [[('location_id','in',[v['id'] for v in loc.values()]),('quantity','>',0)]],
            {'fields':['product_id','location_id','quantity']}
        )

        pids = list({q['product_id'][0] for q in qdata})
        pinfo = models.execute_kw(
            ODOO_DB,uid,ODOO_PASSWORD,'product.product','search_read',
            [[('id','in',pids)]],
            {'fields':['display_name',PRODUCT_CODE_FIELD]}
        )
        pmap = {p['id']:p for p in pinfo}

        data={}
        for q in qdata:
            pid=q['product_id'][0]; locid=q['location_id'][0]; qty=float(q['quantity'])
            if pid not in data:
                data[pid]={ 'Mã SP':pmap[pid].get(PRODUCT_CODE_FIELD,''),
                            'Tên SP':pmap[pid]['display_name'],
                            'Tồn Kho HN':0,'Tồn Kho HCM':0,'Kho Nhập HN':0,'Tổng Tồn HN':0,'Số Lượng Đề Xuất':0 }
            if locid==loc['HN_STOCK']['id']: data[pid]['Tồn Kho HN']+=qty
            elif locid==loc['HCM_STOCK']['id']: data[pid]['Tồn Kho HCM']+=qty
            elif locid==loc['HN_TRANSIT']['id']: data[pid]['Kho Nhập HN']+=qty

        result=[]
        for pid,v in data.items():
            v['Tổng Tồn HN']=v['Tồn Kho HN']+v['Kho Nhập HN']
            if v['Tổng Tồn HN']<TARGET_MIN_QTY:
                need=TARGET_MIN_QTY-v['Tổng Tồn HN']
                v['Số Lượng Đề Xuất']=min(need,v['Tồn Kho HCM'])
                if v['Số Lượng Đề Xuất']>0:
                    result.append(v)

        df=pd.DataFrame(result)
        cols=['Mã SP','Tên SP','Tồn Kho HN','Tồn Kho HCM','Kho Nhập HN','Số Lượng Đề Xuất']
        if not df.empty: df=df[cols]

        buf=io.BytesIO()
        df.to_excel(buf,index=False)
        buf.seek(0)
        return buf,len(result),"thành công"
    except Exception as e:
        return None,0,str(e)

# ---------------- Handle product code ----------------
async def handle_product_code(update:Update,context:ContextTypes.DEFAULT_TYPE):
    product_code=update.message.text.strip().upper()
    await update.message.reply_text(f"đang tra tồn cho `{product_code}`, vui lòng chờ!",parse_mode='Markdown')

    uid,models,msg=connect_odoo()
    if not uid:
        await update.message.reply_text(f"❌ lỗi kết nối odoo: `{escape_markdown(msg)}`",parse_mode='Markdown')
        return
    try:
        loc=find_required_location_ids(models,uid,ODOO_DB,ODOO_PASSWORD)
        hn_transit=loc['HN_TRANSIT']['id']; hn_stock=loc['HN_STOCK']['id']; hcm_stock=loc['HCM_STOCK']['id']

        prod=models.execute_kw(
            ODOO_DB,uid,ODOO_PASSWORD,'product.product','search_read',
            [[(PRODUCT_CODE_FIELD,'=',product_code)]],
            {'fields':['id','display_name']}
        )
        if not prod:
            await update.message.reply_text(f"❌ Không tìm thấy sản phẩm nào có mã `{product_code}`")
            return

        pid=prod[0]['id']; name=prod[0]['display_name']

        def qty(locid):
            d=models.execute_kw(
                ODOO_DB,uid,ODOO_PASSWORD,'product.product','read',
                [[pid]],{'fields':['qty_available'],'context':{'location':locid}}
            )
            return int(d[0]['qty_available']) if d else 0

        q_hn=qty(hn_stock)
        q_nhap=qty(hn_transit)
        q_hcm=qty(hcm_stock)
        total_hn=q_hn+q_nhap

        need=max(0,TARGET_MIN_QTY-total_hn)
        suggest=min(need,q_hcm)

        # Xin lỗi vì đoạn dưới dài — đây là đúng nguyên bản thuật toán cũ
        # Tôi KHÔNG chỉnh sửa 1 ký tự.
        quant_domain=[('product_id','=',pid),('available_quantity','>',0)]
        q_all=models.execute_kw(
            ODOO_DB,uid,ODOO_PASSWORD,'stock.quant','search_read',
            [quant_domain],
            {'fields':['location_id','available_quantity']}
        )
        loc_ids=list({q['location_id'][0] for q in q_all})
        if loc_ids:
            loc_info=models.execute_kw(
                ODOO_DB,uid,ODOO_PASSWORD,'stock.location','read',
                [loc_ids],
                {'fields':['id','display_name','complete_name','usage']}
            )
        else:
            loc_info=[]
        locmap={l['id']:l for l in loc_info}
        stock={}
        for q in q_all:
            lid=q['location_id'][0]
            stock[lid]=stock.get(lid,0)+float(q['available_quantity'])

        detail={}
        for lid,qtyv in stock.items():
            nm=locmap.get(lid,{}).get('complete_name') or locmap.get(lid,{}).get('display_name')
            if qtyv>0: detail[nm]=int(qtyv)

        msg = (
            f"{product_code} {name}\n"
            f"Tồn kho HN: {q_hn}\n"
            f"Tồn kho HCM: {q_hcm}\n"
            f"Tồn kho nhập Hà Nội: {q_nhap}\n"
            f"=> đề xuất nhập thêm {suggest} sp nếu cần.\n\n"
            f"2/ Tồn kho chi tiết(Có hàng):\n"
            + "\n".join(f"{k}: {v}" for k,v in detail.items())
        )

        await update.message.reply_text(msg)

    except Exception as e:
        await update.message.reply_text(f"❌ Lỗi: {str(e)}")

# ---------------- /ping ----------------
async def ping_command(update:Update,context:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Đang kiểm tra kết nối odoo, xin chờ...")
    uid,_,msg=connect_odoo()
    if uid:
        await update.message.reply_text(f"✅ kết nối OK: DB={ODOO_DB}")
    else:
        await update.message.reply_text(f"❌ lỗi: {msg}")

# ---------------- /keohang ----------------
async def excel_report_command(update:Update,context:ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⌛️ Đang tạo báo cáo Excel...")
    buf,count,msg=get_stock_data()
    if not buf:
        await update.message.reply_text(f"❌ Lỗi: {msg}")
        return
    if count>0:
        await update.message.reply_document(buf,"de_xuat_keo_hang.xlsx",caption=f"Đã tìm {count} sản phẩm cần kéo hàng.")
    else:
        await update.message.reply_text(f"Không có sản phẩm nào cần kéo hàng.")
# ---------------- ✔️ NEW FEATURE: /checkexcel ----------------
async def checkexcel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["waiting_for_excel"] = True
    await update.message.reply_text("Gửi file Excel (.xlsx) để kiểm tra tồn.")

# ---------------- ✔️ NEW FEATURE: Excel file handler ----------------
async def excel_file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("waiting_for_excel"):
        return

    context.user_data["waiting_for_excel"] = False

    doc = update.message.document
    if not doc.file_name.lower().endswith(".xlsx"):
        await update.message.reply_text("❌ File phải là .xlsx")
        return

    f = await doc.get_file()
    df = pd.read_excel(io.BytesIO(await f.download_as_bytearray()))

    required = ["Model", "SL", "ĐV nhận"]
    for c in required:
        if c not in df.columns:
            await update.message.reply_text(f"❌ Thiếu cột: {c}")
            return

    uid, models, msg = connect_odoo()
    if not uid:
        await update.message.reply_text(f"❌ Lỗi Odoo: {msg}")
        return

    loc = find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD)
    hn = loc['HN_STOCK']['id']
    nhap = loc['HN_TRANSIT']['id']
    hcm = loc['HCM_STOCK']['id']

    results = []

    for _, r in df.iterrows():
        model = str(r["Model"]).strip()
        sl = int(r["SL"])

        prod = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, "product.product", "search_read",
            [[("default_code", "=", model)]],
            {"fields": ["id"]}
        )

        if not prod:
            results.append({
                "Model": model,
                "SL yêu cầu": sl,
                "Trạng thái": "Không tìm thấy",
                "Đề xuất HCM": 0,
                "ĐV nhận": r["ĐV nhận"]
            })
            continue

        pid = prod[0]['id']

        def qty(locid):
            d = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, "product.product", "read",
                [[pid]],
                {"fields": ["qty_available"], "context": {"location": locid}}
            )
            return int(d[0]["qty_available"]) if d else 0

        q_hn = qty(hn)
        q_np = qty(nhap)
        q_hcm = qty(hcm)
        total_hn = q_hn + q_np

        if total_hn >= sl:
            status = "Đủ"
            suggest = 0
        else:
            need = sl - total_hn
            suggest = min(need, q_hcm)
            status = f"Thiếu {need}"

        results.append({
            "Model": model,
            "SL yêu cầu": sl,
            "Tồn HN(HN+Nhập)": total_hn,
            "Tồn HCM": q_hcm,
            "Trạng thái": status,
            "Đề xuất HCM": suggest,
            "ĐV nhận": r["ĐV nhận"]
        })

    out = pd.DataFrame(results)
    buf = io.BytesIO()
    out.to_excel(buf, index=False)
    buf.seek(0)

    await update.message.reply_document(
        buf,
        filename="kiem_tra_ton.xlsx",
        caption="✔ Hoàn tất kiểm tra tồn file Excel!"
    )

# ---------------- start ----------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user.first_name
    await update.message.reply_text(
        f"Chào {user}!\n"
        "Gõ mã SP để tra tồn.\n"
        "Dùng /keohang để tạo báo cáo.\n"
        "Dùng /checkexcel để kiểm tra tồn theo file Excel."
    )

# ---------------- Main ----------------
def main():
    if not TELEGRAM_TOKEN:
        logger.error("Thiếu biến môi trường TOKEN")
        return

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Xóa webhook
    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        asyncio.get_event_loop().run_until_complete(bot.delete_webhook())
    except:
        pass

    # lệnh cũ
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", start_command))
    app.add_handler(CommandHandler("ping", ping_command))
    app.add_handler(CommandHandler("keohang", excel_report_command))

    # lệnh mới
    app.add_handler(CommandHandler("checkexcel", checkexcel_command))
    app.add_handler(MessageHandler(filters.Document.ALL, excel_file_handler))

    # handler mã SP (cũ giữ nguyên)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_product_code))

    logger.info("Bot running…")
    app.run_polling()

# ---------------- HTTP ping ----------------
from http.server import BaseHTTPRequestHandler, HTTPServer
class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive!")
def start_http_server():
    try:
        server = HTTPServer(("0.0.0.0",10001),PingHandler)
        server.serve_forever()
    except:
        pass

threading.Thread(target=start_http_server,daemon=True).start()

if __name__=="__main__":
    main()
