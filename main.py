import os
import io
import logging
import pandas as pd
import ssl
import xmlrpc.client
import asyncio
import socket
import threading
import time
import urllib.request
import requests
from datetime import datetime
from urllib.parse import urlparse
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import pytz
# --- TÍCH HỢP THÊM GROQ ---
from groq import Groq

# ---------------- Config Environment ----------------
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
GROQ_API_KEY = os.environ.get('GROQ_API_KEY') 

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

# ---------------- Logging ----------------
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------------- AI Groq & Price Data (SỬA LỖI MẤT DỮ LIỆU) ----------------
# Tao lưu bảng giá vào file để tránh bị mất khi Render restart
PRICE_DATA_FILE = "stored_price_list.txt"

def save_price_context(content):
    with open(PRICE_DATA_FILE, "w", encoding="utf-8") as f:
        f.write(content)

def get_price_context():
    if os.path.exists(PRICE_DATA_FILE):
        with open(PRICE_DATA_FILE, "r", encoding="utf-8") as f:
            return f.read()
    return ""

def process_price_excel(file_bytes):
    try:
        xl = pd.ExcelFile(io.BytesIO(file_bytes))
        # Ưu tiên lấy sheet cuối cùng vì mày hay để tab mới nhất ở cuối
        latest_sheet = xl.sheet_names[-1]
        df_raw = pd.read_excel(io.BytesIO(file_bytes), sheet_name=latest_sheet, header=None)
        
        # Tự động tìm header để nhận đủ > 100 dòng dữ liệu
        header_row_idx = 0
        for idx in range(min(len(df_raw), 25)):
            row_values = df_raw.iloc[idx].astype(str).str.lower().fillna('')
            row_text = " ".join(row_values)
            if any(key in row_text for key in ["mã hàng", "model", "mã sp", "niêm yết"]):
                header_row_idx = idx
                break
        
        df = pd.read_excel(io.BytesIO(file_bytes), sheet_name=latest_sheet, header=header_row_idx)
        df = df.dropna(how='all').dropna(axis=1, how='all')
        
        context = df.to_string(index=False)
        save_price_context(context) # Lưu vĩnh viễn vào file
        return True, len(df)
    except Exception as e:
        logger.error(f"Lỗi xử lý bảng giá: {e}")
        return False, str(e)

def ask_groq_ai(query):
    if not GROQ_API_KEY: return "Chưa cấu hình GROQ_API_KEY."
    
    price_data = get_price_context() # Đọc từ file
    if not price_data: return "Iem chưa có dữ liệu bảng giá."
    
    try:
        client = Groq(api_key=GROQ_API_KEY)
        # Sử dụng Model llama-3.3-70b-versatile ổn định nhất
        completion = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": f"Bảng giá:\n{price_data}\n\nKhách hỏi: {query}\nTìm mã SP và báo giá. Ngắn gọn."}],
            temperature=0
        )
        return completion.choices[0].message.content
    except Exception as e: return f"Lỗi AI: {e}"

# ---------------- Keep port open (Render free) ----------------
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

# ---------------- Odoo connect (GIỮ NGUYÊN) ----------------
def connect_odoo():
    try:
        if not ODOO_URL_FINAL:
            return None, None, "odoo url không được thiết lập."

        payload = {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {
                "service": "common",
                "method": "login",
                "args": [ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD]
            },
            "id": 1
        }

        r = requests.post(f"{ODOO_URL_FINAL}/jsonrpc", json=payload, timeout=15)
        uid = r.json().get("result")
        if not uid: return None, None, "Đăng nhập thất bại."

        class Models:
            def execute_kw(self, db, uid, pwd, model, method, args, kwargs=None):
                payload = {
                    "jsonrpc": "2.0",
                    "method": "call",
                    "params": {"service": "object", "method": "execute_kw", "args": [db, uid, pwd, model, method, args, kwargs or {}]},
                    "id": 2
                }
                r = requests.post(f"{ODOO_URL_FINAL}/jsonrpc", json=payload, timeout=60)
                return r.json().get("result")

        return uid, Models(), "OK"
    except Exception as e: return None, None, f"Lỗi kết nối: {e}"

# ================== PHẦN DƯỚI GIỮ NGUYÊN 100% ==================
def get_odoo_url_components():
    if not ODOO_URL_FINAL: return None, None
    parsed = urlparse(ODOO_URL_FINAL)
    scheme, netloc = parsed.scheme, parsed.netloc
    port = parsed.port or (80 if scheme == 'http' else 443)
    return netloc, port

def find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD):
    out = {}
    def search(key):
        locs = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'stock.location', 'search_read', [[('display_name', 'ilike', key)]], {'fields': ['id', 'display_name', 'complete_name']})
        if not locs: return None
        for l in locs:
            if key.lower() in (l['display_name'] or '').lower(): return {'id': l['id'], 'name': l['display_name']}
        return {'id': locs[0]['id'], 'name': locs[0]['display_name']}
    hn = search(LOCATION_MAP['HN_STOCK_CODE']); 
    if hn: out['HN_STOCK'] = hn
    hcm = search(LOCATION_MAP['HCM_STOCK_CODE']); 
    if hcm: out['HCM_STOCK'] = hcm
    tran = search(LOCATION_MAP['HN_TRANSIT_NAME']); 
    if tran: out['HN_TRANSIT'] = tran
    return out

def get_transit_quantity(models, uid, product_id, transit_location_id):
    if not transit_location_id: return 0
    quant_data = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'stock.quant', 'search_read', [[('product_id', '=', product_id), ('location_id', '=', transit_location_id)]], {'fields': ['quantity']})
    return sum(int(q.get('quantity') or 0) for q in quant_data)

def escape_markdown(text):
    chars = ['\\','_','*','[',']','(',')','~','`','>','#','+','-','=','|','{','}','.','!']
    for c in chars: text = str(text).replace(c, f"\\{c}")
    return text.replace('\\`', '`')

REGISTERED_CHAT_IDS = set()
CHAT_IDS_LOCK = threading.Lock()

def register_chat_id(chat_id):
    if chat_id is None: return
    with CHAT_IDS_LOCK: REGISTERED_CHAT_IDS.add(int(chat_id))

def get_registered_chat_ids():
    with CHAT_IDS_LOCK: return list(REGISTERED_CHAT_IDS)

def get_stock_data():
    uid, models, error_msg = connect_odoo()
    if not uid: return None, 0, error_msg
    try:
        location_ids = find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD)
        hn_id, hcm_id, tran_id = location_ids['HN_STOCK']['id'], location_ids['HCM_STOCK']['id'], location_ids['HN_TRANSIT']['id']
        quant_data_raw = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'stock.quant', 'search_read', [[('location_id', 'in', [hn_id, hcm_id, tran_id])]], {'fields': ['product_id', 'location_id', 'quantity', 'reserved_quantity', 'available_quantity']})
        stock_map = {}
        for q in quant_data_raw:
            pid, loc = q['product_id'][0], q['location_id'][0]
            real_qty = float(q.get('quantity', 0)) if loc == tran_id else float(q.get('available_quantity') or (float(q.get('quantity', 0)) - float(q.get('reserved_quantity', 0))))
            if real_qty <= 0: continue
            if pid not in stock_map: stock_map[pid] = {'hn': 0, 'tran': 0, 'hcm': 0}
            if loc == hn_id: stock_map[pid]['hn'] += real_qty
            elif loc == tran_id: stock_map[pid]['tran'] += real_qty
            elif loc == hcm_id: stock_map[pid]['hcm'] += real_qty
        
        pids = list(stock_map.keys())
        product_info = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'product.product', 'search_read', [[('id', 'in', pids)]], {'fields': ['display_name', PRODUCT_CODE_FIELD]})
        product_map = {p['id']: p for p in product_info}
        report = []
        for pid, qtys in stock_map.items():
            prod = product_map.get(pid)
            if not prod: continue
            ton_hn, ton_tran, ton_hcm = int(round(qtys['hn'])), int(round(qtys['tran'])), int(round(qtys['hcm']))
            if (ton_hn + ton_tran) < TARGET_MIN_QTY:
                de_xuat = min(TARGET_MIN_QTY - (ton_hn + ton_tran), ton_hcm)
                if de_xuat > 0:
                    report.append({'Mã SP': prod.get(PRODUCT_CODE_FIELD, ''), 'Tên SP': prod.get('display_name', ''), 'Tồn Kho HN': ton_hn, 'Tồn Kho HCM': ton_hcm, 'Kho Nhập HN': ton_tran, 'Số Lượng Đề Xuất': de_xuat})
        df = pd.DataFrame(report)
        buf = io.BytesIO(); df.to_excel(buf, index=False); buf.seek(0)
        return buf, len(df), "thành công"
    except Exception as e: return None, 0, str(e)

async def handle_product_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    register_chat_id(chat_id)
    user_input = update.message.text.strip()
    
    if any(k in user_input.lower() for k in ['giá', 'bao nhiêu', 'vat', 'bảng giá', 'price']):
        await update.message.reply_text("⌛️ Iem đang tra bảng giá xíu...")
        await update.message.reply_text(ask_groq_ai(user_input))
        return

    product_code = user_input.upper()
    await update.message.reply_text(f"đang tra tồn cho `{product_code}`, vui lòng chờ!")
    uid, models, err = connect_odoo()
    if not uid: await update.message.reply_text(f"❌ lỗi: {err}"); return
    try:
        locs = find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD)
        prods = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'product.product', 'search_read', [[(PRODUCT_CODE_FIELD, '=', product_code)]], {'fields': ['display_name', 'id']})
        if not prods: await update.message.reply_text(f"❌ Không tìm thấy `{product_code}`"); return
        
        p = prods[0]
        pid, name = p['id'], p['display_name']
        def get_q(l_id):
            if not l_id: return 0
            res = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'product.product', 'read', [[pid]], {'fields': ['qty_available'], 'context': {'location': l_id}})
            return int(round(res[0]['qty_available'])) if res else 0
        
        hn_q, hcm_q, tr_q = get_q(locs.get('HN_STOCK',{}).get('id')), get_q(locs.get('HCM_STOCK',{}).get('id')), get_transit_quantity(models, uid, pid, locs.get('HN_TRANSIT',{}).get('id'))
        rec = min(TARGET_MIN_QTY - (hn_q + tr_q), hcm_q) if (hn_q + tr_q) < TARGET_MIN_QTY else 0
        
        msg = f"{product_code} {name}\nTồn HN: {hn_q}\nTồn HCM: {hcm_q}\nKho Nhập HN: {tr_q}\n=> Đề xuất: {max(0, rec)}"
        await update.message.reply_text(msg)
    except Exception as e: await update.message.reply_text(f"❌ Lỗi: {e}")

async def handle_po_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    register_chat_id(chat_id)
    doc = update.message.document
    if not doc or not doc.file_name.lower().endswith('.xlsx'): return

    if context.user_data.get('waiting_for_po'):
        context.user_data['waiting_for_po'] = False
        await update.message.reply_text("⌛️ Đang xử lý file PO...")
        file = await doc.get_file(); f_bytes = await file.download_as_bytearray()
        buf, err = process_po_and_build_report(bytes(f_bytes)) 
        if buf: await update.message.reply_document(buf, filename="kiem_tra_po.xlsx")
        else: await update.message.reply_text(f"❌ Lỗi: {err}")
    else:
        await update.message.reply_text("📥 Đang nạp bảng giá mới cho AI...")
        file = await doc.get_file(); f_bytes = await file.download_as_bytearray()
        success, info = process_price_excel(bytes(f_bytes))
        if success: await update.message.reply_text(f"✅ Đã nạp thành công bảng giá ({info} dòng). Chị có thể bắt đầu hỏi giá rồi nha!")
        else: await update.message.reply_text(f"❌ Lỗi nạp bảng giá: {info}")

# ---------------- Watchdog & Main (GIỮ NGUYÊN) ----------------
def watchdog_201():
    global previous_snapshot
    tz = pytz.timezone("Asia/Ho_Chi_Minh")
    while True:
        try:
            uid, models, err = connect_odoo()
            if not uid: time.sleep(60); continue
            locs = find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD)
            hn_id = locs.get("HN_STOCK", {}).get("id")
            if not hn_id: time.sleep(60); continue
            quant_data = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "stock.quant", "search_read", [[("location_id", "=", hn_id)]], {"fields": ["product_id", "available_quantity"]})
            current_snapshot = {q["product_id"][0]: int(q.get("available_quantity") or 0) for q in quant_data}
            if previous_snapshot:
                for pid, new_qty in current_snapshot.items():
                    old_qty = previous_snapshot.get(pid, 0)
                    if new_qty != old_qty:
                        diff = new_qty - old_qty
                        p_info = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "product.product", "read", [[pid]], {"fields": ["display_name", PRODUCT_CODE_FIELD]})[0]
                        move_data = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "stock.move", "search_read", [[("product_id", "=", pid)]], {"fields": ["id", "picking_id"], "limit": 1, "order": "id desc"})
                        pick_name, actor = "N/A", "Không rõ"
                        if move_data and move_data[0].get("picking_id"):
                            p_id = move_data[0]["picking_id"][0]
                            p_detail = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "stock.picking", "read", [[p_id]], {"fields": ["name", "write_uid"]})[0]
                            pick_name, actor = p_detail.get("name", "N/A"), p_detail.get("write_uid", [0, "Không rõ"])[1]
                        msg = f"📦 *Cập nhật tồn kho 201/201*\nMã: {p_info.get(PRODUCT_CODE_FIELD)}\nBiến động: {diff}\nTổng tồn mới: {new_qty}\nLệnh: {pick_name}\nNgười: {actor}\nLúc: {datetime.now(tz).strftime('%H:%M %d/%m/%Y')}"
                        for c_id in get_registered_chat_ids():
                            try: Bot(token=TELEGRAM_TOKEN).send_message(c_id, msg, parse_mode="Markdown")
                            except: pass
            previous_snapshot = current_snapshot
            time.sleep(60)
        except: time.sleep(60)

threading.Thread(target=watchdog_201, daemon=True).start()

def main():
    if not TELEGRAM_TOKEN or not ODOO_URL_RAW or not ODOO_DB or not ODOO_USERNAME or not ODOO_PASSWORD: return
    application = Application.builder().token(TELEGRAM_TOKEN).build()
    try: asyncio.get_event_loop().run_until_complete(Bot(token=TELEGRAM_TOKEN).delete_webhook())
    except: pass
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("checkpo", checkpo_command))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_po_file))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_product_code))
    application.run_polling()

if __name__ == "__main__":
    main()
