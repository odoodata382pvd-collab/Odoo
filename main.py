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
from datetime import datetime, time as dt_time
from urllib.parse import urlparse
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, filters, ContextTypes
import pytz
import json
import re
from groq import Groq

# ---------------- Config Environment ----------------
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')

# Cấu hình 3 API Key AI (Xoay vòng để tránh lỗi 429)
AI_KEYS = [
    os.environ.get('GROQ_API_KEY_1'),
    os.environ.get('GROQ_API_KEY_2'),
    os.environ.get('GROQ_API_KEY_3')
]
current_key_index = 0

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

# ---------------- TÍNH NĂNG: AI & XỬ LÝ EXCEL ----------------
PRICE_DATA_FILE = "price_cache.json"

def process_price_excel(file_bytes):
    try:
        xl = pd.ExcelFile(io.BytesIO(file_bytes))
        sheet_names = xl.sheet_names
        target_sheet = None
        max_date = None
        pattern = re.compile(r'T(\d+)[\.,_\-\s](\d+)', re.IGNORECASE)
        
        for name in sheet_names:
            match = pattern.search(name)
            if match:
                try:
                    month = int(match.group(1))
                    year = int(match.group(2))
                    current_date = datetime(year, month, 1)
                    if max_date is None or current_date > max_date:
                        max_date = current_date
                        target_sheet = name
                except ValueError:
                    continue
        
        if not target_sheet:
            target_sheet = sheet_names[0]
            logger.info(f"Dùng sheet đầu tiên: {target_sheet}")
        else:
            logger.info(f"Dùng sheet mới nhất: {target_sheet}")

        df_raw = pd.read_excel(io.BytesIO(file_bytes), sheet_name=target_sheet, header=None)
        
        header_row_idx = 0
        found_header = False
        
        for idx, row in df_raw.iterrows():
            row_list = [str(val).lower() for val in row.values]
            row_str = " ".join(row_list)
            
            if "niêm yết" in row_str:
                header_row_idx = idx
                found_header = True
                break
            elif "mã hàng" in row_str or "mã sp" in row_str:
                if not found_header:
                    header_row_idx = idx
        
        if not found_header:
            return False, f"Không tìm thấy dòng tiêu đề hợp lệ trong sheet {target_sheet}"

        df = pd.read_excel(io.BytesIO(file_bytes), sheet_name=target_sheet, header=header_row_idx)
        
        if header_row_idx > 0:
            for i, col_name in enumerate(df.columns):
                if str(col_name).startswith('Unnamed') or str(col_name).lower() == 'nan':
                    val_above = str(df_raw.iloc[header_row_idx - 1, i]).strip()
                    if val_above and val_above.lower() != 'nan':
                        df.columns.values[i] = val_above

        df.columns = [str(c).strip() for c in df.columns]
        ma_hang_col = next((c for c in df.columns if 'mã hàng' in c.lower() or 'mã sp' in c.lower()), None)
        
        if ma_hang_col:
            df = df.dropna(subset=[ma_hang_col])
            data_dict = df.astype(str).to_dict(orient='records')
            
            cache_data = {
                "sheet_name": target_sheet,
                "data": data_dict
            }
            
            with open(PRICE_DATA_FILE, 'w', encoding='utf-8') as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=4)
                
            return True, f"{len(df)} dòng (Sheet: {target_sheet})"
        
        return False, f"Lỗi cấu trúc cột trong sheet {target_sheet}"

    except Exception as e:
        logger.error(f"Lỗi nạp bảng giá: {e}")
        return False, str(e)

def ask_groq_ai(query):
    global current_key_index
    if not os.path.exists(PRICE_DATA_FILE):
        return "Iem chưa có dữ liệu bảng giá. Hãy gửi file Excel để nạp nhé!"
    try:
        with open(PRICE_DATA_FILE, 'r', encoding='utf-8') as f:
            cache = json.load(f)
            
        if isinstance(cache, list):
            full_data = cache
            sheet_name = "Mới nhất"
        else:
            full_data = cache.get("data", [])
            sheet_name = cache.get("sheet_name", "Mới nhất")

        query_upper = query.upper()
        found_item = None
        
        for item in full_data:
            key_ma = next((k for k in item.keys() if "mã" in k.lower() and ("hàng" in k.lower() or "sp" in k.lower())), None)
            if key_ma:
                ma_sp = str(item[key_ma]).upper().strip()
                if ma_sp and ma_sp in query_upper:
                    found_item = item
                    break
        
        if not found_item:
            return "Iem không tìm thấy mã hàng này trong bảng giá ạ."

        clean_info = {k: v for k, v in found_item.items() if str(v).lower() != 'nan' and 'unnamed' not in str(k).lower()}

        prompt = f"""
        Dữ liệu sản phẩm: {clean_info}
        Tên bảng giá: {sheet_name}
        Câu hỏi: "{query}"
        
        NHIỆM VỤ: Trả lời chính xác theo FORM mẫu bên dưới.
        QUY TẮC XỬ LÝ SỐ LIỆU (BẮT BUỘC):
        1. **CHẶN SỐ RÁC:** Bất kỳ con số nào nhỏ hơn 1000 => ĐÓ LÀ CHIẾT KHẤU HOẶC RÁC. BỎ QUA NGAY.
        2. **TÌM CỘT GIÁ:**
           - "Giá niêm yết": Cột 'Niêm Yết'.
           - "Giá nhập (VAT 10%)": Cột 'Giá nhập (+VAT 10%)' hoặc tương tự.
           - "VAT 8%": Cột 'Giá Mới (VAT 8%)' hoặc 'Giá nhập (Bao gồm VAT)'.
           - "Giá chưa VAT": Cột '- VAT' (giá cũ) hoặc '- VAT.1' (giá mới 8%). Ưu tiên lấy giá ở cột '- VAT.1' (cột sau) nếu có.
        3. **LÀM TRÒN:** Luôn làm tròn số đến hàng nghìn.
        4. Nếu một loại giá là 0 hoặc không tìm thấy, ghi "Chưa có thông tin".
        
        FORM TRẢ LỜI (Copy y nguyên):
        📦 *[Mã SP]*
        📅 Bảng giá tháng ({sheet_name})
        💰 *Giá nhập:*
        - *VAT 10%: * [Số tiền] VNĐ
        - *VAT 8%: * [Số tiền] VNĐ
        - *Giá niêm yết: * [Số tiền] VNĐ
        - *Giá chưa VAT: * [Số tiền] VNĐ
        """

        for _ in range(3):
            api_key = AI_KEYS[current_key_index]
            if not api_key:
                current_key_index = (current_key_index + 1) % 3
                continue
            try:
                client = Groq(api_key=api_key)
                completion = client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0
                )
                return completion.choices[0].message.content
            except Exception as e:
                if "429" in str(e):
                    current_key_index = (current_key_index + 1) % 3
                    continue
                return f"Lỗi AI: {e}"
        
        return "Hệ thống AI đang bận, vui lòng thử lại sau!"

    except Exception as e:
        return f"Lỗi hệ thống: {e}"

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
        if not uid:
            return None, None, "Đăng nhập thất bại. Kiểm tra DB/user/pass."

        class Models:
            def execute_kw(self, db, uid, pwd, model, method, args, kwargs=None):
                payload = {
                    "jsonrpc": "2.0",
                    "method": "call",
                    "params": {
                        "service": "object",
                        "method": "execute_kw",
                        "args": [db, uid, pwd, model, method, args, kwargs or {}]
                    },
                    "id": 2
                }
                r = requests.post(f"{ODOO_URL_FINAL}/jsonrpc", json=payload, timeout=60)
                return r.json().get("result")

        return uid, Models(), "OK"

    except Exception as e:
        return None, None, f"Lỗi kết nối: {e}"

def get_odoo_url_components():
    if not ODOO_URL_FINAL:
        return None, None
    parsed = urlparse(ODOO_URL_FINAL)
    scheme = parsed.scheme
    netloc = parsed.netloc
    if scheme == 'http':
        port = parsed.port or 80
    elif scheme == 'https':
        port = parsed.port or 443
    else:
        port = None
    return netloc, port

# ---------------- Location helpers ----------------
def find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD):
    out = {}
    def search(key):
        locs = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'stock.location', 'search_read',
            [[('display_name', 'ilike', key)]],
            {'fields': ['id', 'display_name', 'complete_name']}
        )
        if not locs:
            return None
        for l in locs:
            if key.lower() in (l['display_name'] or '').lower():
                return {'id': l['id'], 'name': l['display_name']}
        return {'id': locs[0]['id'], 'name': locs[0]['display_name']}

    hn = search(LOCATION_MAP['HN_STOCK_CODE'])
    if hn: out['HN_STOCK'] = hn
    hcm = search(LOCATION_MAP['HCM_STOCK_CODE'])
    if hcm: out['HCM_STOCK'] = hcm
    tran = search(LOCATION_MAP['HN_TRANSIT_NAME'])
    if tran: out['HN_TRANSIT'] = tran
    return out

def get_transit_quantity(models, uid, product_id, transit_location_id):
    if not transit_location_id: return 0
    quant_data = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        'stock.quant', 'search_read',
        [[('product_id', '=', product_id), ('location_id', '=', transit_location_id)]],
        {'fields': ['quantity']}
    )
    total = 0
    for q in quant_data:
        total += int(q.get('quantity') or 0)
    return total

def escape_markdown(text):
    chars = ['\\','_','*','[',']','(',')','~','`','>','#','+','-','=','|','{','}','.','!']
    text = str(text)
    for c in chars:
        text = text.replace(c, f"\\{c}")
    return text.replace('\\`', '`')

# ---------------- Chat ID Registry ----------------
REGISTERED_CHAT_IDS = set()
CHAT_IDS_LOCK = threading.Lock()

def register_chat_id(chat_id):
    if chat_id is None: return
    try: cid = int(chat_id)
    except Exception: cid = chat_id
    with CHAT_IDS_LOCK:
        REGISTERED_CHAT_IDS.add(cid)

def get_registered_chat_ids():
    with CHAT_IDS_LOCK:
        return list(REGISTERED_CHAT_IDS)

# ---------------- Report /keohang ----------------
def get_stock_data():
    uid, models, error_msg = connect_odoo()
    if not uid:
        return None, 0, error_msg

    try:
        location_ids = find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD)
        if len(location_ids) < 3:
            return None, 0, "Không tìm thấy đủ 3 kho cần thiết"

        hn_id   = location_ids.get('HN_STOCK', {}).get('id')
        hcm_id  = location_ids.get('HCM_STOCK', {}).get('id')
        tran_id = location_ids.get('HN_TRANSIT', {}).get('id')

        quant_data_raw = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'stock.quant', 'search_read',
            [[('location_id', 'in', [hn_id, hcm_id, tran_id])]],
            {'fields': ['product_id', 'location_id', 'quantity', 'reserved_quantity', 'available_quantity']}
        )

        stock_map = {}
        for q in quant_data_raw:
            pid = q['product_id'][0]
            loc = q['location_id'][0]

            if loc == tran_id:
                real_qty = float(q.get('quantity', 0))
            else:
                if 'available_quantity' in q and q.get('available_quantity') is not None:
                    real_qty = float(q.get('available_quantity', 0))
                else:
                    real_qty = float(q.get('quantity', 0)) - float(q.get('reserved_quantity', 0))

            if real_qty <= 0: continue

            if pid not in stock_map:
                stock_map[pid] = {'hn': 0, 'tran': 0, 'hcm': 0}

            if loc == hn_id: stock_map[pid]['hn'] += real_qty
            elif loc == tran_id: stock_map[pid]['tran'] += real_qty
            elif loc == hcm_id: stock_map[pid]['hcm'] += real_qty

        if not stock_map:
            df_empty = pd.DataFrame(columns=['Mã SP', 'Tên SP', 'Tồn Kho HN', 'Tồn Kho HCM', 'Kho Nhập HN', 'Số Lượng Đề Xuất'])
            buf = io.BytesIO()
            df_empty.to_excel(buf, index=False, sheet_name='DeXuatKeoHang')
            buf.seek(0)
            return buf, 0, "không có SP nào cần kéo"

        pids = list(stock_map.keys())
        product_info = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'product.product', 'search_read',
            [[('id', 'in', pids)]],
            {'fields': ['display_name', PRODUCT_CODE_FIELD]}
        )
        product_map = {p['id']: p for p in product_info}

        report = []
        for pid, qtys in stock_map.items():
            prod = product_map.get(pid)
            if not prod: continue

            code = prod.get(PRODUCT_CODE_FIELD, '')
            name = prod.get('display_name', '')

            ton_hn   = int(round(qtys['hn']))
            ton_tran = int(round(qtys['tran']))
            ton_hcm  = int(round(qtys['hcm']))
            tong_hn = ton_hn + ton_tran

            if tong_hn < TARGET_MIN_QTY:
                need = TARGET_MIN_QTY - tong_hn
                de_xuat = min(need, ton_hcm)
                if de_xuat > 0:
                    report.append({
                        'Mã SP': code, 'Tên SP': name,
                        'Tồn Kho HN': ton_hn, 'Tồn Kho HCM': ton_hcm,
                        'Kho Nhập HN': ton_tran, 'Số Lượng Đề Xuất': de_xuat
                    })

        df = pd.DataFrame(report)
        cols = ['Mã SP', 'Tên SP', 'Tồn Kho HN', 'Tồn Kho HCM', 'Kho Nhập HN', 'Số Lượng Đề Xuất']

        if not df.empty: df = df[cols]
        else: df = pd.DataFrame(columns=cols)

        buf = io.BytesIO()
        df.to_excel(buf, index=False, sheet_name="DeXuatKeoHang")
        buf.seek(0)

        return buf, len(df), "thành công"
    except Exception as e:
        logger.error(f"lỗi khi xử lý kéo hàng: {e}")
        return None, 0, f"lỗi: {e}"

# ---------------- Handle product code & Logic AI ----------------
async def handle_product_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    register_chat_id(chat_id)

    user_input = update.message.text.strip()
    
    if any(k in user_input.lower() for k in ['giá', 'bao nhiêu', 'vat', 'bảng giá', 'price']):
        await update.effective_message.reply_text("⌛️ Iem đang tra bảng giá xíu...")
        answer = ask_groq_ai(user_input)
        await update.effective_message.reply_text(answer, parse_mode='Markdown')
        return

    product_code = user_input.upper()
    await update.effective_message.reply_text(f"đang tra tồn cho `{product_code}`, vui lòng chờ!", parse_mode='Markdown')

    uid, models, error_msg = connect_odoo()
    if not uid:
        await update.effective_message.reply_text(f"❌ lỗi kết nối odoo. chi tiết: `{escape_markdown(error_msg)}`", parse_mode='Markdown')
        return

    try:
        location_ids = find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD)
        hn_stock_id   = location_ids.get('HN_STOCK', {}).get('id')
        hn_transit_id = location_ids.get('HN_TRANSIT', {}).get('id')
        hcm_stock_id  = location_ids.get('HCM_STOCK', {}).get('id')

        products = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'product.product', 'search_read',
            [[(PRODUCT_CODE_FIELD, '=', product_code)]],
            {'fields': ['display_name', 'id']}
        )

        if not products:
            await update.effective_message.reply_text(f"❌ Không tìm thấy sản phẩm nào có mã `{product_code}`")
            return

        product = products[0]
        product_id = product['id']
        product_name = product['display_name']

        def get_qty_available(location_id):
            if not location_id: return 0
            stock_info = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                'product.product', 'read',
                [[product_id]],
                {'fields': ['qty_available'], 'context': {'location': location_id}}
            )
            if stock_info and stock_info[0]:
                return int(round(stock_info[0].get('qty_available', 0.0)))
            return 0

        hn_stock_qty  = get_qty_available(hn_stock_id)
        hcm_stock_qty = get_qty_available(hcm_stock_id)
        hn_transit_qty = get_transit_quantity(models, uid, product_id, hn_transit_id)

        quant_domain = [('product_id', '=', product_id), ('available_quantity', '>', 0)]
        quant_data = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'stock.quant', 'search_read',
            [quant_domain],
            {'fields': ['location_id', 'available_quantity']}
        )

        location_ids_list = list({q['location_id'][0] for q in quant_data if q.get('location_id')})
        if location_ids_list:
            location_info = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                'stock.location', 'read',
                [location_ids_list],
                {'fields': ['id', 'display_name', 'complete_name', 'usage']}
            )
        else: location_info = []

        loc_map = {l['id']: l for l in location_info}
        stock_details = {}

        for q in quant_data:
            loc_field = q.get('location_id')
            if not loc_field: continue
            loc_id = loc_field[0]
            qty = float(q.get('available_quantity', 0.0))
            if qty <= 0: continue
            name_loc = loc_map.get(loc_id, {}).get('complete_name') or loc_map.get(loc_id, {}).get('display_name') or f"ID:{loc_id}"
            stock_details[name_loc] = stock_details.get(name_loc, 0) + int(qty)

        total_hn = hn_stock_qty + hn_transit_qty
        recommend = 0
        if total_hn < TARGET_MIN_QTY:
            need = TARGET_MIN_QTY - total_hn
            recommend = min(need, hcm_stock_qty)

        priority_items = []
        other_items = []
        used_names = set()

        for code in PRIORITY_LOCATIONS:
            for name, qty in stock_details.items():
                if code.lower() in name.lower() and name not in used_names:
                    priority_items.append((name, qty))
                    used_names.add(name)
                    break

        for name, qty in sorted(stock_details.items()):
            if name not in used_names:
                other_items.append((name, qty))
                used_names.add(name)

        final_list = priority_items + other_items

        msg = (
            f"📦 *{product_code}* {product_name}\n"
            f"Tồn kho HN: {int(hn_stock_qty)}\n"
            f"Tồn kho HCM: {int(hcm_stock_qty)}\n"
            f"Tồn kho nhập Hà Nội: {int(hn_transit_qty)}\n"
            f"=> Đề xuất nhập thêm {int(recommend)} sp để HN đủ tồn {TARGET_MIN_QTY} sản phẩm.\n\n"
            "📍 Tồn kho chi tiết (Có hàng):"
        )
        if final_list:
            for loc_name, qty in final_list: msg += f"\n- {loc_name}: {qty}"
        else:
            msg += "\nKhông có tồn kho chi tiết lớn hơn 0."

        await update.effective_message.reply_text(msg.strip(), parse_mode='Markdown')
    except Exception as e:
        logger.error(f"lỗi khi tra tồn: {e}")
        await update.effective_message.reply_text(f"❌ lỗi khi tra tồn: {e}")

# ---------------- Daily Reports Logic ----------------
def get_daily_movement_report():
    uid, models, error_msg = connect_odoo()
    if not uid: return None, error_msg
    try:
        tz_vn = pytz.timezone("Asia/Ho_Chi_Minh")
        now_vn = datetime.now(tz_vn)
        start_date_vn = now_vn.replace(hour=0, minute=0, second=0, microsecond=0)
        
        start_date_utc = start_date_vn.astimezone(pytz.utc).strftime('%Y-%m-%d %H:%M:%S')
        end_date_utc = now_vn.astimezone(pytz.utc).strftime('%Y-%m-%d %H:%M:%S')

        domain = [('state', '=', 'done'), ('date', '>=', start_date_utc), ('date', '<=', end_date_utc)]
        moves = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'stock.move', 'search_read', [domain], 
                                  {'fields': ['product_id', 'product_uom_qty', 'date', 'location_id', 'location_dest_id', 'picking_id', 'write_uid']})

        if not moves:
            buf = io.BytesIO()
            with pd.ExcelWriter(buf, engine='openpyxl') as writer:
                pd.DataFrame(columns=['Mã SP', 'Tên SP', 'Số lượng', 'Nhập từ đâu', 'Thời gian', 'Người thao tác', 'Mã lệnh']).to_excel(writer, index=False, sheet_name='NHẬP KHO')
                pd.DataFrame(columns=['Mã SP', 'Tên SP', 'Số lượng', 'Xuất đi đâu', 'Thời gian', 'Người thao tác', 'Mã lệnh']).to_excel(writer, index=False, sheet_name='XUẤT KHO')
            buf.seek(0)
            return buf, "Không có giao dịch Nhập/Xuất nào trong ngày hôm nay."

        product_ids = list(set([m['product_id'][0] for m in moves if m.get('product_id')]))
        product_map = {}
        if product_ids:
            products_info = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'product.product', 'search_read', [[('id', 'in', product_ids)]], {'fields': ['display_name', PRODUCT_CODE_FIELD]})
            product_map = {p['id']: p for p in products_info}

        import_rows, export_rows = [], []
        hn_stock_name = LOCATION_MAP.get('HN_STOCK_CODE', "201/201") 

        for m in moves:
            pid = m['product_id'][0] if m.get('product_id') else None
            prod = product_map.get(pid, {})
            code = prod.get(PRODUCT_CODE_FIELD, "N/A")
            name = prod.get('display_name', "Không tên")
            qty = int(m.get('product_uom_qty') or 0)
            
            from_location = m['location_id'][1] if m.get('location_id') else "N/A"
            to_location = m['location_dest_id'][1] if m.get('location_dest_id') else "N/A"
            picking_name = m['picking_id'][1] if m.get('picking_id') else "N/A"
            actor = m['write_uid'][1] if m.get('write_uid') else "Hệ thống"
            
            utc_time = datetime.strptime(m['date'], '%Y-%m-%d %H:%M:%S').replace(tzinfo=pytz.utc)
            vn_time_str = utc_time.astimezone(tz_vn).strftime('%H:%M:%S')

            row_data = {'Mã SP': code, 'Tên SP': name, 'Số lượng': qty, 'Thời gian': vn_time_str, 'Người thao tác': actor, 'Mã lệnh': picking_name}

            if hn_stock_name.lower() in to_location.lower():
                row_data['Nhập từ đâu'] = from_location
                import_rows.append(row_data)
            elif hn_stock_name.lower() in from_location.lower():
                row_data['Xuất đi đâu'] = to_location
                export_rows.append(row_data)

        buf = io.BytesIO()
        with pd.ExcelWriter(buf, engine='openpyxl') as writer:
            df_in = pd.DataFrame(import_rows)
            in_cols = ['Mã SP', 'Tên SP', 'Số lượng', 'Nhập từ đâu', 'Thời gian', 'Người thao tác', 'Mã lệnh']
            if df_in.empty: df_in = pd.DataFrame(columns=in_cols)
            else: df_in = df_in[in_cols]
            df_in.to_excel(writer, index=False, sheet_name='NHẬP KHO')

            df_out = pd.DataFrame(export_rows)
            out_cols = ['Mã SP', 'Tên SP', 'Số lượng', 'Xuất đi đâu', 'Thời gian', 'Người thao tác', 'Mã lệnh']
            if df_out.empty: df_out = pd.DataFrame(columns=out_cols)
            else: df_out = df_out[out_cols]
            df_out.to_excel(writer, index=False, sheet_name='XUẤT KHO')

        buf.seek(0)
        return buf, "Thành công"
    except Exception as e:
        return None, str(e)


# ---------------- Telegram Handlers & Commands ----------------

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    register_chat_id(chat_id)
    name = update.effective_user.first_name
    
    # TÍNH NĂNG MỚI: Bảng điều khiển bằng nút bấm (Inline Keyboard)
    keyboard = [
        [InlineKeyboardButton("📊 Báo cáo đề xuất kéo hàng", callback_data='btn_keohang')],
        [InlineKeyboardButton("📅 Báo cáo xuất/nhập hôm nay", callback_data='btn_baocaongay')],
        [InlineKeyboardButton("🧠 Nhờ AI đánh giá kho hôm nay", callback_data='btn_phantich')],
        [InlineKeyboardButton("⚙️ Ping kiểm tra Odoo", callback_data='btn_ping')]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.effective_message.reply_text(
        f"Xin chào {name}! Cứ gõ mã SP để tra tồn kho, hoặc hỏi giá để em AI trả lời nhé.\n\n"
        "👇 Ngoài ra, sếp có thể thao tác nhanh qua các nút bên dưới:",
        reply_markup=reply_markup
    )

async def button_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    
    if data == 'btn_keohang':
        await excel_report_command(update, context)
    elif data == 'btn_baocaongay':
        await daily_report_command(update, context)
    elif data == 'btn_phantich':
        await ai_analysis_command(update, context)
    elif data == 'btn_ping':
        await ping_command(update, context)

async def ai_analysis_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    register_chat_id(chat_id)
    
    msg = await context.bot.send_message(chat_id, "🧠 *AI đang thu thập dữ liệu Odoo hôm nay, sếp chờ xíu...*", parse_mode='Markdown')
    
    uid, models, err = connect_odoo()
    if not uid:
        await msg.edit_text(f"❌ Lỗi kết nối Odoo: {err}")
        return

    try:
        tz_vn = pytz.timezone("Asia/Ho_Chi_Minh")
        now_vn = datetime.now(tz_vn)
        start_date_utc = now_vn.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(pytz.utc).strftime('%Y-%m-%d %H:%M:%S')
        end_date_utc = now_vn.astimezone(pytz.utc).strftime('%Y-%m-%d %H:%M:%S')

        domain = [('state', '=', 'done'), ('date', '>=', start_date_utc), ('date', '<=', end_date_utc)]
        moves = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'stock.move', 'search_read', [domain], 
                                  {'fields': ['product_uom_qty', 'location_id', 'location_dest_id']})

        hn_stock_name = LOCATION_MAP.get('HN_STOCK_CODE', "201/201")
        total_in, total_out = 0, 0
        for m in moves:
            qty = m.get('product_uom_qty', 0)
            from_loc = m['location_id'][1] if m.get('location_id') else ""
            to_loc = m['location_dest_id'][1] if m.get('location_dest_id') else ""
            
            if hn_stock_name.lower() in to_loc.lower(): total_in += qty
            elif hn_stock_name.lower() in from_loc.lower(): total_out += qty

        prompt = (f"Số liệu kho hôm nay: Tổng nhập {total_in} sản phẩm, Tổng xuất {total_out} sản phẩm. "
                  "Đóng vai một chuyên gia quản lý kho, hãy viết 1 đoạn ngắn (dưới 100 chữ) nhận xét về nhịp độ luân chuyển hàng hóa và 1 lời động viên ngắn gọn cho team.")
        
        global current_key_index
        for _ in range(3):
            api_key = AI_KEYS[current_key_index]
            if not api_key:
                current_key_index = (current_key_index + 1) % 3
                continue
            try:
                client = Groq(api_key=api_key)
                completion = client.chat.completions.create(
                    model="llama-3.3-70b-versatile",
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.7
                )
                answer = completion.choices[0].message.content
                await msg.edit_text(f"📊 **GIÁM ĐỐC KHO AI NHẬN XÉT:**\n\n{answer}", parse_mode='Markdown')
                return
            except Exception:
                current_key_index = (current_key_index + 1) % 3
                
        await msg.edit_text("❌ AI hiện đang bận, sếp thử lại sau nhé.")
    except Exception as e:
        await msg.edit_text(f"❌ Lỗi xử lý: {e}")

async def daily_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    register_chat_id(chat_id)
    await context.bot.send_message(chat_id, "⌛️ Iem đang tổng hợp dữ liệu Xuất/Nhập kho hôm nay...")
    excel_buffer, error_msg = get_daily_movement_report()
    if excel_buffer:
        today_str = datetime.now(pytz.timezone("Asia/Ho_Chi_Minh")).strftime("%d-%m-%Y")
        await context.bot.send_document(chat_id=chat_id, document=excel_buffer, filename=f"Bao_cao_kho_ngay_{today_str}.xlsx", caption=f"📊 Báo cáo luồng hàng Nhập/Xuất ngày {today_str} đã sẵn sàng ạ!")
    else:
        await context.bot.send_message(chat_id, f"❌ Không thể tạo báo cáo: {error_msg}")

async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    register_chat_id(chat_id)
    await context.bot.send_message(chat_id, "Đang kiểm tra kết nối odoo, xin chờ...")
    uid, _, error_msg = connect_odoo()
    if uid: await context.bot.send_message(chat_id, f"✅ Thành công! Kết nối Odoo DB: {ODOO_DB}")
    else: await context.bot.send_message(chat_id, f"❌ Lỗi: {error_msg}")

async def excel_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    register_chat_id(chat_id)
    await context.bot.send_message(chat_id, "⌛️ Iem đang xử lý dữ liệu và tạo báo cáo kéo hàng Excel...")
    excel_buffer, item_count, error_msg = get_stock_data()
    if excel_buffer is None:
        await context.bot.send_message(chat_id, f"❌ Lỗi: {error_msg}")
        return
    if item_count > 0:
        await context.bot.send_document(chat_id=chat_id, document=excel_buffer, filename="de_xuat_keo_hang.xlsx", caption=f"Đã tìm thấy {item_count} sản phẩm cần kéo hàng.")
    else:
        await context.bot.send_message(chat_id, f"Không có sản phẩm nào cần kéo hàng (đủ tồn {TARGET_MIN_QTY}).")

async def checkpo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    register_chat_id(chat_id)
    context.user_data['waiting_for_po'] = True
    await update.effective_message.reply_text("Ok, gửi file PO Excel (.xlsx) để em kiểm tra tồn kho theo mẫu đối tác gửi nha!")

# ... (Hàm process_po_and_build_report được thu gọn tương tự file cũ, chạy tốt) ...
def process_po_and_build_report(file_bytes: bytes):
    df_raw, err = _read_po_with_auto_header(file_bytes)
    if df_raw is None: return None, err
    if df_raw.empty: return None, "File PO không có dữ liệu."
    code_col, qty_col, recv_col = _detect_po_columns(df_raw)
    if not code_col or not qty_col or not recv_col: return None, f"Không xác định được cột."
    df = df_raw[[code_col, qty_col, recv_col]].copy()
    df.columns = ['Mã SP', 'SL cần giao', 'ĐV nhận']
    df['Mã SP'] = df['Mã SP'].astype(str).str.strip().str.upper()
    df['SL cần giao'] = pd.to_numeric(df['SL cần giao'], errors='coerce').fillna(0)
    df = df[(df['Mã SP'] != "") & (df['SL cần giao'] > 0)]
    if df.empty: return None, "Không có dòng hợp lệ."
    uid, models, error_msg = connect_odoo()
    if not uid: return None, error_msg
    try:
        codes = sorted(df['Mã SP'].unique().tolist())
        products = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'product.product', 'search_read', [[(PRODUCT_CODE_FIELD, 'in', codes)]], {'fields': ['id', 'display_name', PRODUCT_CODE_FIELD]})
        code_map = {str(p.get(PRODUCT_CODE_FIELD) or "").strip().upper(): p for p in products}
        location_ids = find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD)
        stock_cache = {}
        rows = []
        for _, r in df.iterrows():
            code, need_qty, receiver = r['Mã SP'], int(round(r['SL cần giao'])), r['ĐV nhận']
            prod = code_map.get(code)
            if not prod:
                rows.append({'Mã SP': code, 'Tên SP': 'KHÔNG TÌM THẤY', 'ĐV nhận': receiver, 'SL cần giao': need_qty, 'Tồn HN': 0, 'Tồn Kho Nhập': 0, 'Tổng tồn HN': 0, 'Tồn HCM': 0, 'Trạng thái': 'KHÔNG TÌM THẤY MÃ', 'SL cần kéo từ HCM': 0, 'SL thiếu': need_qty})
                continue
            pid, name = prod['id'], prod['display_name']
            stock = _get_stock_for_product_with_cache(models, uid, pid, location_ids, stock_cache)
            hn, hcm = stock['hn'], stock['hcm']
            tr = get_transit_quantity(models, uid, pid, location_ids.get('HN_TRANSIT', {}).get('id'))
            total_hn = hn + tr
            pull, shortage = 0, 0
            if need_qty <= hn: status = "ĐỦ tại kho HN"
            elif need_qty <= total_hn: status = "ĐỦ (HN + Kho nhập)"
            else:
                req = need_qty - total_hn
                if req <= hcm: pull, status = req, "CẦN KÉO HCM"
                else: pull, shortage, status = hcm, req - hcm, "THIẾU HÀNG"
            rows.append({'Mã SP': code, 'Tên SP': name, 'ĐV nhận': receiver, 'SL cần giao': need_qty, 'Tồn HN': hn, 'Tồn Kho Nhập': tr, 'Tổng tồn HN': total_hn, 'Tồn HCM': hcm, 'Trạng thái': status, 'SL cần kéo từ HCM': pull, 'SL thiếu': shortage})
        df_out = pd.DataFrame(rows)[['Mã SP','Tên SP','ĐV nhận','SL cần giao','Tồn HN','Tồn Kho Nhập','Tổng tồn HN','Tồn HCM','Trạng thái','SL cần kéo từ HCM','SL thiếu']]
        buf = io.BytesIO()
        df_out.to_excel(buf, index=False, sheet_name='KiemTraPO')
        buf.seek(0)
        return buf, None
    except Exception as e: return None, f"Lỗi PO: {e}"

async def handle_po_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    register_chat_id(chat_id)
    document = update.message.document
    if not document: return
    file_name = (document.file_name or "").lower()
    if not file_name.endswith(".xlsx"):
        await update.effective_message.reply_text("Chỉ hỗ trợ file Excel .xlsx thôi nha.")
        return

    if context.user_data.get('waiting_for_po'):
        context.user_data['waiting_for_po'] = False
        await update.effective_message.reply_text("⌛️ Iem đang xử lý file PO...")
        try:
            file = await document.get_file()
            file_bytes = await file.download_as_bytearray()
            excel_buffer, error_msg = process_po_and_build_report(bytes(file_bytes))
            if excel_buffer: await update.message.reply_document(document=excel_buffer, filename="kiem_tra_po.xlsx", caption="❤️ File kiểm tra PO đã xong ạ!")
            else: await update.effective_message.reply_text(f"❌ Lỗi: {error_msg}")
        except Exception as e: await update.effective_message.reply_text(f"❌ Lỗi PO: {e}")
    else:
        await update.effective_message.reply_text("📥 Đang nạp bảng giá mới cho AI...")
        try:
            file = await document.get_file()
            file_bytes = await file.download_as_bytearray()
            success, info = process_price_excel(bytes(file_bytes))
            if success: await update.effective_message.reply_text(f"✅ Đã nạp thành công bảng giá ({info}). Chị có thể bắt đầu hỏi giá rồi nha!")
            else: await update.effective_message.reply_text(f"❌ Lỗi nạp bảng giá: {info}")
        except Exception as e: await update.effective_message.reply_text(f"❌ Lỗi: {e}")

# TÍNH NĂNG MỚI: Tự động chạy báo cáo kéo hàng vào 8h00 Sáng
async def auto_morning_alert(context: ContextTypes.DEFAULT_TYPE):
    logger.info("Chạy cron-job báo cáo sáng...")
    excel_buffer, item_count, error_msg = get_stock_data()
    
    if item_count > 0 and excel_buffer:
        for chat_id in get_registered_chat_ids():
            try:
                excel_buffer.seek(0) # Trả buffer về điểm 0 trước khi gửi cho người tiếp theo
                await context.bot.send_document(
                    chat_id=chat_id, 
                    document=excel_buffer, 
                    filename="Canh_Bao_Keo_Hang_Sang.xlsx",
                    caption=f"⏰ **BÁO CÁO SÁNG TỰ ĐỘNG:**\nHiện có {item_count} mã dưới định mức ({TARGET_MIN_QTY} sp). Team kho chú ý check file Excel để lên phương án kéo hàng từ HCM nhé!",
                    parse_mode='Markdown'
                )
            except Exception as e:
                logger.error(f"Lỗi gửi auto alert cho {chat_id}: {e}")

# ---------------- Watchdog 201 ----------------
WATCH_INTERVAL = 60
previous_snapshot = {}

def watchdog_201():
    global previous_snapshot
    tz = pytz.timezone("Asia/Ho_Chi_Minh")
    while True:
        try:
            uid, models, err = connect_odoo()
            if not uid:
                time.sleep(WATCH_INTERVAL)
                continue
            location_ids = find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD)
            hn_id = location_ids.get("HN_STOCK", {}).get("id")
            if not hn_id:
                time.sleep(WATCH_INTERVAL)
                continue

            quant_data = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "stock.quant", "search_read", [[("location_id", "=", hn_id)]], {"fields": ["product_id", "available_quantity"]})
            current_snapshot = {q["product_id"][0]: int(q.get("available_quantity") or 0) for q in quant_data}

            if not previous_snapshot:
                previous_snapshot = current_snapshot
                time.sleep(WATCH_INTERVAL)
                continue

            for pid, new_qty in current_snapshot.items():
                old_qty = previous_snapshot.get(pid, 0)
                if new_qty == old_qty: continue
                diff = new_qty - old_qty

                product_info = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "product.product", "read", [[pid]], {"fields": ["display_name", PRODUCT_CODE_FIELD]})[0]
                code = product_info.get(PRODUCT_CODE_FIELD, "???")
                name = product_info.get("display_name", "Không tên")

                move_data = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "stock.move", "search_read", [[("product_id", "=", pid)]], {"fields": ["id", "picking_id"], "limit": 1, "order": "id desc"})
                picking_name, actor = "N/A", "Hệ thống"

                if move_data and move_data[0].get("picking_id"):
                    picking_id = move_data[0]["picking_id"][0]
                    picking_info = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, "stock.picking", "read", [[picking_id]], {"fields": ["name", "write_uid", "create_uid"]})[0]
                    picking_name = picking_info.get("name", "N/A")
                    if picking_info.get("write_uid"): actor = picking_info.get("write_uid")[1]

                status = "NHẬP KHO" if diff > 0 else "XUẤT KHO"
                now_vn = datetime.now(tz).strftime('%H:%M %d/%m/%Y')
                msg = (f"📦 *Cập nhật 201/201 – {status}*\n\n*Mã SP:* {code}\n*Tên SP:* {name}\n"
                       f"*Biến động:* {'+' if diff > 0 else ''}{diff} SP\n*Tổng tồn mới:* {new_qty} SP\n\n"
                       f"*Mã lệnh:* {picking_name}\n*Người thao tác:* {actor}\n*Thời gian:* {now_vn}")

                for chat_id in get_registered_chat_ids():
                    try: asyncio.run(Bot(token=TELEGRAM_TOKEN).send_message(chat_id, msg, parse_mode="Markdown"))
                    except Exception as e: pass

            previous_snapshot = current_snapshot
            time.sleep(WATCH_INTERVAL)
        except Exception as e:
            time.sleep(WATCH_INTERVAL)

threading.Thread(target=watchdog_201, daemon=True).start()

# ---------------- HTTP Ping ----------------
class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is alive!")
    def log_message(self, format, *args): return

def start_http():
    try:
        server = HTTPServer(("0.0.0.0", 10001), PingHandler)
        server.serve_forever()
    except Exception: pass
threading.Thread(target=start_http, daemon=True).start()

def keep_alive_ping():
    while True:
        try: urllib.request.urlopen("https://google.com", timeout=10)
        except Exception: pass
        time.sleep(300)
threading.Thread(target=keep_alive_ping, daemon=True).start()

# ---------------- MAIN ----------------
def main():
    if not TELEGRAM_TOKEN or not ODOO_URL_RAW or not ODOO_DB or not ODOO_USERNAME or not ODOO_PASSWORD:
        logger.error("Thiếu cấu hình môi trường.")
        return

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        asyncio.get_event_loop().run_until_complete(bot.delete_webhook())
    except Exception: pass

    # Setup Cảnh báo tự động vào lúc 08:00 sáng giờ VN
    target_time = dt_time(hour=8, minute=0, tzinfo=pytz.timezone("Asia/Ho_Chi_Minh"))
    application.job_queue.run_daily(auto_morning_alert, target_time)

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", start_command))
    application.add_handler(CommandHandler("ping", ping_command))
    application.add_handler(CommandHandler("keohang", excel_report_command))
    application.add_handler(CommandHandler("checkpo", checkpo_command))
    application.add_handler(CommandHandler("baocaongay", daily_report_command))
    application.add_handler(CommandHandler("phantich", ai_analysis_command))
    
    # Đăng ký handler xử lý nút bấm
    application.add_handler(CallbackQueryHandler(button_callback_handler))
    
    application.add_handler(MessageHandler(filters.Document.ALL, handle_po_file))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_product_code))

    logger.info("Bot started!")
    application.run_polling()

if __name__ == "__main__":
    main()
