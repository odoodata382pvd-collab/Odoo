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
import urllib.parse
import requests
from datetime import datetime
from urllib.parse import urlparse
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import pytz
import json
import re
import xml.etree.ElementTree as ET
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
        1. **CHẶN SỐ RÁC:** Bất kỳ con số nào nhỏ hơn 1000 (Ví dụ: 0, 0.3, 0.15, 30, 40) => ĐÓ LÀ CHIẾT KHẤU HOẶC RÁC. BỎ QUA NGAY.
        2. **TÌM CỘT GIÁ:**
           - "Giá niêm yết": Cột 'Niêm Yết'.
           - "Giá nhập (VAT 10%)": Cột 'Giá nhập (+VAT 10%)' hoặc tương tự.
           - "VAT 8%": Cột 'Giá Mới (VAT 8%)' hoặc 'Giá nhập (Bao gồm VAT)'.
           - "Giá chưa VAT": Cột '- VAT' (giá cũ) hoặc '- VAT.1' (giá mới 8%). Ưu tiên lấy giá ở cột '- VAT.1' (cột sau) nếu có.
        3. **LÀM TRÒN:** Luôn làm tròn số đến hàng nghìn (VD: 525909 -> 526.000).
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


# ---------------- CÁC HÀM HỖ TRỢ REAL-TIME CHO AI ----------------
def get_realtime_weather(location="Hà Nội"):
    try:
        url = f"https://wttr.in/{urllib.parse.quote(location)}?format=%l:+%c+%t,+%w,+%h"
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            return res.text.strip()
        return "Hiện tại không lấy được dữ liệu thời tiết thực tế."
    except Exception:
        return "Lỗi kết nối khi lấy thời tiết."

def get_realtime_news():
    try:
        res = requests.get("https://vnexpress.net/rss/tin-moi-nhat.rss", timeout=5)
        if res.status_code == 200:
            root = ET.fromstring(res.content)
            news_items = []
            for item in root.findall('./channel/item')[:5]:
                title = item.find('title').text
                news_items.append(f"- {title}")
            return "\n".join(news_items)
        return "Không lấy được tin tức mới nhất từ hệ thống."
    except Exception:
        return "Lỗi kết nối khi lấy tin tức."

def analyze_chat_intent(user_input):
    global current_key_index
    tz_vn = pytz.timezone("Asia/Ho_Chi_Minh")
    current_time_str = datetime.now(tz_vn).strftime("%Y-%m-%d %H:%M:%S")
    
    system_prompt = f"""
    Bạn là bộ não điều hướng. Thời gian hiện tại: {current_time_str}.
    Nhiệm vụ của bạn là phân tích câu nói của người dùng và trả về DUY NHẤT một chuỗi JSON hợp lệ. KHÔNG giải thích.
    
    Quy tắc phân loại:
    1. Nếu yêu cầu THỐNG KÊ / BÁO CÁO ĐƠN HÀNG từ ngày này đến ngày khác:
    -> {{"action": "export_report", "start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD"}}
    
    2. Nếu người dùng hỏi về THỜI TIẾT:
    -> {{"action": "weather", "location": "Tên địa phương"}} (mặc định là 'Hà Nội' nếu không rõ)
    
    3. Nếu người dùng hỏi TIN TỨC, thời sự:
    -> {{"action": "news"}}
    
    4. Nếu câu lệnh CHỈ LÀ MÃ SẢN PHẨM (chuỗi ngắn, liền nhau, hoặc từ khóa kho, vd: 'SP01', 'IPHONE12', 'A123', '201'):
    -> {{"action": "stock_search"}}
    
    5. Nếu là câu giao tiếp bình thường (chào hỏi, tâm sự, trêu đùa):
    -> {{"action": "chat", "response": "Câu trả lời dí dỏm, thông minh, hài hước của bạn"}}
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
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_input}
                ],
                temperature=0.1,
                response_format={"type": "json_object"}
            )
            return json.loads(completion.choices[0].message.content)
        except Exception as e:
            if "429" in str(e):
                current_key_index = (current_key_index + 1) % 3
                continue
            return {"action": "error", "response": f"Lỗi phân tích AI: {e}"}
            
    return {"action": "error", "response": "Server AI đang quá tải, sếp thử lại sau nhé!"}

def generate_witty_response(user_input, topic, real_data):
    global current_key_index
    system_prompt = f"""
    Bạn là một trợ lý AI thông minh, dí dỏm và rất biết cách giao tiếp hài hước.
    Người dùng vừa hỏi về {topic}. Dưới đây là THÔNG TIN THỰC TẾ CHÍNH XÁC 100% vừa được hệ thống lấy về:
    ---
    {real_data}
    ---
    Nhiệm vụ: Hãy trả lời câu hỏi '{user_input}' của người dùng dựa vào thông tin trên một cách tự nhiên. Bạn có thể dùng ngôn ngữ gen Z, trêu đùa hoặc chúc một câu năng lượng. TUYỆT ĐỐI KHÔNG bịa đặt sai lệch dữ liệu thực tế đã cung cấp ở trên.
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
                messages=[{"role": "system", "content": system_prompt}],
                temperature=0.5
            )
            return completion.choices[0].message.content
        except Exception:
            if "429" in str(Exception):
                current_key_index = (current_key_index + 1) % 3
                continue
            return f"Thông tin cập nhật: {real_data}"
    return real_data

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

        r = requests.post(
            f"{ODOO_URL_FINAL}/jsonrpc",
            json=payload,
            timeout=15
        )

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
                        "args": [
                            db, uid, pwd, model, method, args, kwargs or {}
                        ]
                    },
                    "id": 2
                }

                r = requests.post(
                    f"{ODOO_URL_FINAL}/jsonrpc",
                    json=payload,
                    timeout=60
                )
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
    if hn:
        out['HN_STOCK'] = hn

    hcm = search(LOCATION_MAP['HCM_STOCK_CODE'])
    if hcm:
        out['HCM_STOCK'] = hcm

    tran = search(LOCATION_MAP['HN_TRANSIT_NAME'])
    if tran:
        out['HN_TRANSIT'] = tran

    return out

# ---------------- Kho Nhập HN – quantity ----------------
def get_transit_quantity(models, uid, product_id, transit_location_id):
    if not transit_location_id:
        return 0

    quant_data = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        'stock.quant', 'search_read',
        [[('product_id', '=', product_id),
          ('location_id', '=', transit_location_id)]],
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
    if chat_id is None:
        return
    try:
        cid = int(chat_id)
    except Exception:
        cid = chat_id

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
            error_msg = f"không tìm thấy đủ 3 kho cần thiết: {list(location_ids.keys())}"
            logger.error(error_msg)
            return None, 0, error_msg

        hn_id   = location_ids.get('HN_STOCK', {}).get('id')
        hcm_id  = location_ids.get('HCM_STOCK', {}).get('id')
        tran_id = location_ids.get('HN_TRANSIT', {}).get('id')

        quant_data_raw = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'stock.quant', 'search_read',
            [[('location_id', 'in', [hn_id, hcm_id, tran_id])]],
            {'fields': ['product_id', 'location_id', 'quantity',
                        'reserved_quantity', 'available_quantity']}
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

            if real_qty <= 0:
                continue

            if pid not in stock_map:
                stock_map[pid] = {'hn': 0, 'tran': 0, 'hcm': 0}

            if loc == hn_id:
                stock_map[pid]['hn'] += real_qty
            elif loc == tran_id:
                stock_map[pid]['tran'] += real_qty
            elif loc == hcm_id:
                stock_map[pid]['hcm'] += real_qty

        if not stock_map:
            df_empty = pd.DataFrame(columns=[
                'Mã SP', 'Tên SP', 'Tồn Kho HN',
                'Tồn Kho HCM', 'Kho Nhập HN', 'Số Lượng Đề Xuất'
            ])
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
            if not prod:
                continue

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
                        'Mã SP': code,
                        'Tên SP': name,
                        'Tồn Kho HN': ton_hn,
                        'Tồn Kho HCM': ton_hcm,
                        'Kho Nhập HN': ton_tran,
                        'Số Lượng Đề Xuất': de_xuat
                    })

        df = pd.DataFrame(report)
        cols = [
            'Mã SP', 'Tên SP', 'Tồn Kho HN',
            'Tồn Kho HCM', 'Kho Nhập HN', 'Số Lượng Đề Xuất'
        ]

        if not df.empty:
            df = df[cols]
        else:
            df = pd.DataFrame(columns=cols)

        buf = io.BytesIO()
        df.to_excel(buf, index=False, sheet_name="DeXuatKeoHang")
        buf.seek(0)

        return buf, len(df), "thành công"

    except Exception as e:
        logger.error(f"lỗi khi xử lý kéo hàng: {e}")
        return None, 0, f"lỗi khi xử lý kéo hàng: {e}"


# ---------------- PO /checkpo helpers ----------------
def _read_po_with_auto_header(file_bytes: bytes):
    try:
        df_tmp = pd.read_excel(io.BytesIO(file_bytes), header=None)
    except Exception as e:
        return None, f"Không đọc được file Excel PO: {e}"

    header_row_idx = None
    for idx in range(len(df_tmp)):
        row_values = df_tmp.iloc[idx].astype(str).str.lower()
        row_text = " ".join(row_values)
        if any(key in row_text for key in [
            "model", "mã sp", "ma sp", "mã hàng", "ma hang",
            "mã sản phẩm", "ma san pham"
        ]):
            header_row_idx = idx
            break

    if header_row_idx is None:
        header_row_idx = 0

    try:
        df_raw = pd.read_excel(io.BytesIO(file_bytes), header=header_row_idx)
        return df_raw, None
    except Exception as e:
        return None, f"Không đọc được file Excel PO với header tại dòng {header_row_idx + 1}: {e}"


def _detect_po_columns(df: pd.DataFrame):
    cols_lower = {col: str(col).strip().lower() for col in df.columns}

    code_col = None
    for col, lower in cols_lower.items():
        if lower == "model":
            code_col = col
            break

    if code_col is None:
        for col, lower in cols_lower.items():
            if lower.strip() == "model":
                code_col = col
                break

    def find_col(candidates):
        for col, lower in cols_lower.items():
            for key in candidates:
                if key in lower:
                    return col
        return None

    if code_col is None:
        code_col = find_col([
            'mã sp', 'ma sp', 'mã hàng', 'ma hang',
            'mã sản phẩm', 'ma san pham'
        ])

    qty_col = find_col([
        'sl', 'số lượng', 'so luong', 's.l', 'sl đặt', 'sl dat'
    ])

    recv_col = find_col([
        'đv nhận', 'dv nhận', 'đơn vị nhận', 'don vi nhan',
        'đv nhận hàng', 'dv nhận hang',
        'cửa hàng nhận', 'cua hang nhan'
    ])

    return code_col, qty_col, recv_col


def _get_stock_for_product_with_cache(models, uid, product_id, location_ids, cache):
    if product_id in cache:
        return cache[product_id]

    hn_id      = location_ids.get('HN_STOCK', {}).get('id')
    transit_id = location_ids.get('HN_TRANSIT', {}).get('id')
    hcm_id     = location_ids.get('HCM_STOCK', {}).get('id')

    def _get_qty(location_id):
        if not location_id:
            return 0
        stock_product_info = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'product.product', 'read',
            [[product_id]],
            {'fields': ['qty_available'], 'context': {'location': location_id}}
        )
        if stock_product_info and stock_product_info[0]:
            return int(round(stock_product_info[0].get('qty_available', 0.0)))
        return 0

    result = {
        'hn': _get_qty(hn_id),
        'transit': _get_qty(transit_id),
        'hcm': _get_qty(hcm_id),
    }
    cache[product_id] = result
    return result


def process_po_and_build_report(file_bytes: bytes):
    df_raw, err = _read_po_with_auto_header(file_bytes)
    if df_raw is None:
        return None, err

    if df_raw.empty:
        return None, "File PO không có dữ liệu."

    code_col, qty_col, recv_col = _detect_po_columns(df_raw)
    if not code_col or not qty_col or not recv_col:
        return None, (
            "Không xác định được Model – Số lượng – ĐV nhận.\n"
            f"Các cột hiện có: {list(df_raw.columns)}"
        )

    df = df_raw[[code_col, qty_col, recv_col]].copy()
    df.columns = ['Mã SP', 'SL cần giao', 'ĐV nhận']

    df['Mã SP'] = df['Mã SP'].astype(str).str.strip().str.upper()
    df['SL cần giao'] = pd.to_numeric(df['SL cần giao'], errors='coerce').fillna(0)
    df = df[(df['Mã SP'] != "") & (df['SL cần giao'] > 0)]

    if df.empty:
        return None, "Không có dòng hợp lệ."

    uid, models, error_msg = connect_odoo()
    if not uid:
        return None, error_msg

    try:
        codes = sorted(df['Mã SP'].unique().tolist())
        products = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'product.product', 'search_read',
            [[(PRODUCT_CODE_FIELD, 'in', codes)]],
            {'fields': ['id', 'display_name', PRODUCT_CODE_FIELD]}
        )

        code_map = {}
        for p in products:
            c = str(p.get(PRODUCT_CODE_FIELD) or "").strip().upper()
            code_map[c] = p

        location_ids = find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD)
        stock_cache = {}
        rows = []

        for _, r in df.iterrows():
            code = r['Mã SP']
            need_qty = int(round(r['SL cần giao']))
            receiver = r['ĐV nhận']

            prod = code_map.get(code)
            if not prod:
                rows.append({
                    'Mã SP': code,
                    'Tên SP': 'KHÔNG TÌM THẤY',
                    'ĐV nhận': receiver,
                    'SL cần giao': need_qty,
                    'Tồn HN': 0,
                    'Tồn Kho Nhập': 0,
                    'Tổng tồn HN': 0,
                    'Tồn HCM': 0,
                    'Trạng thái': 'KHÔNG TÌM THẤY MÃ',
                    'SL cần kéo từ HCM': 0,
                    'SL thiếu': need_qty,
                })
                continue

            pid = prod['id']
            name = prod['display_name']

            stock = _get_stock_for_product_with_cache(
                models, uid, pid, location_ids, stock_cache
            )

            hn  = stock['hn']
            hcm = stock['hcm']

            tr = get_transit_quantity(
                models, uid, pid,
                location_ids.get('HN_TRANSIT', {}).get('id')
            )

            total_hn = hn + tr
            pull = 0
            shortage = 0

            if need_qty <= hn:
                status = "ĐỦ tại kho HN (201/201)"
            elif need_qty <= total_hn:
                status = "ĐỦ (HN + Kho nhập HN)"
            else:
                req = need_qty - total_hn
                if req <= hcm:
                    pull = req
                    status = "CẦN KÉO HÀNG TỪ HCM"
                else:
                    pull = hcm
                    shortage = req - hcm
                    status = "THIẾU DÙ ĐÃ KÉO TỐI ĐA"

            rows.append({
                'Mã SP': code,
                'Tên SP': name,
                'ĐV nhận': receiver,
                'SL cần giao': need_qty,
                'Tồn HN': hn,
                'Tồn Kho Nhập': tr,
                'Tổng tồn HN': total_hn,
                'Tồn HCM': hcm,
                'Trạng thái': status,
                'SL cần kéo từ HCM': pull,
                'SL thiếu': shortage,
            })

        df_out = pd.DataFrame(rows)
        cols = [
            'Mã SP','Tên SP','ĐV nhận','SL cần giao',
            'Tồn HN','Tồn Kho Nhập','Tổng tồn HN','Tồn HCM',
            'Trạng thái','SL cần kéo từ HCM','SL thiếu'
        ]
        df_out = df_out[cols]

        buf = io.BytesIO()
        df_out.to_excel(buf, index=False, sheet_name='KiemTraPO')
        buf.seek(0)
        return buf, None

    except Exception as e:
        return None, f"Lỗi khi xử lý PO: {e}"

# =====================================================================
# ---> TẠO FILE EXCEL TỒN KHO CHI TIẾT <---
# =====================================================================
async def process_export_inventory(update: Update, context: ContextTypes.DEFAULT_TYPE, loc_id: int, loc_name: str):
    uid, models, error_msg = connect_odoo()
    if not uid:
        await update.message.reply_text(f"❌ Lỗi kết nối Odoo: {error_msg}")
        return

    try:
        quants = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'stock.quant', 'search_read',
            [[('location_id', '=', loc_id), ('quantity', '>', 0)]],
            {'fields': ['product_id', 'quantity', 'available_quantity', 'reserved_quantity']}
        )

        if not quants:
            await update.message.reply_text(f"📭 Kho *{loc_name}* hiện đang trống, không có sản phẩm nào tồn kho.", parse_mode='Markdown')
            return

        stock_map = {}
        for q in quants:
            pid = q['product_id'][0]
            if pid not in stock_map:
                stock_map[pid] = {'qty': 0, 'available': 0, 'reserved': 0}
            stock_map[pid]['qty'] += float(q.get('quantity', 0))
            stock_map[pid]['available'] += float(q.get('available_quantity', 0))
            stock_map[pid]['reserved'] += float(q.get('reserved_quantity', 0))

        pids = list(stock_map.keys())
        products = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'product.product', 'search_read',
            [[('id', 'in', pids)]],
            {'fields': ['id', 'display_name', PRODUCT_CODE_FIELD]}
        )
        product_map = {p['id']: p for p in products}

        rows = []
        for pid, qtys in stock_map.items():
            prod = product_map.get(pid, {})
            rows.append({
                'Mã SP': prod.get(PRODUCT_CODE_FIELD, 'N/A'),
                'Tên SP': prod.get('display_name', 'Không xác định'),
                'Tồn thực tế (Quantity)': qtys['qty'],
                'Có sẵn (Available)': qtys['available'],
                'Đã giữ (Reserved)': qtys['reserved']
            })

        df = pd.DataFrame(rows)
        df = df.sort_values(by='Mã SP')

        buf = io.BytesIO()
        df.to_excel(buf, index=False, sheet_name='Ton_Kho')
        buf.seek(0)

        safe_loc_name = "".join(c for c in loc_name if c.isalnum() or c in (' ', '_')).replace(' ', '_')
        today_str = datetime.now().strftime('%d%m%Y')
        filename = f"Ton_Kho_{safe_loc_name}_{today_str}.xlsx"

        await update.message.reply_document(
            document=buf,
            filename=filename,
            caption=f"📊 Iem gửi file thống kê tồn kho của *{loc_name}* ạ!\nTổng cộng có {len(df)} mã sản phẩm đang có hàng.",
            parse_mode='Markdown'
        )

    except Exception as e:
        logger.error(f"Lỗi khi đổ tồn kho: {e}")
        await update.message.reply_text(f"❌ Lỗi khi xuất dữ liệu tồn kho: {e}")


# =====================================================================
# ---> HÀM TÌM VÀ QUÉT KHO THEO TỪ KHÓA <---
# =====================================================================
async def dotonkho_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    register_chat_id(chat_id)

    keyword = " ".join(context.args).strip()

    if not keyword:
        msg = (
            "💡 Danh sách kho trên Odoo thường rất dài. Để tìm và xuất dữ liệu nhanh nhất, "
            "chị vui lòng gõ lệnh kèm theo **từ khóa** tên kho nhé!\n\n"
            "👉 *Ví dụ:* `/dotonkho 201` hoặc `/dotonkho hcm`"
        )
        await update.message.reply_text(msg, parse_mode='Markdown')
        return

    await update.message.reply_text(f"🔍 Đang tìm các kho chứa từ khóa `*{keyword}*`...", parse_mode='Markdown')
    
    uid, models, error_msg = connect_odoo()
    if not uid:
        await update.message.reply_text(f"❌ Lỗi kết nối Odoo: {error_msg}")
        return

    try:
        domain = [('usage', '=', 'internal'), ('display_name', 'ilike', keyword)]
        locations = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'stock.location', 'search_read',
            [domain],
            {'fields': ['id', 'display_name']}
        )

        if not locations:
            await update.message.reply_text(f"📭 Không tìm thấy kho nào có tên chứa từ khóa `*{keyword}*`.", parse_mode='Markdown')
            return

        if len(locations) == 1:
            loc = locations[0]
            await update.message.reply_text(f"✅ Tìm thấy đúng 1 kho: *{loc['display_name']}*\n⌛️ Iem đang gom số liệu tồn...", parse_mode='Markdown')
            await process_export_inventory(update, context, loc['id'], loc['display_name'])
            return

        loc_dict = {str(loc['id']): loc for loc in locations}
        context.user_data['waiting_for_location'] = True
        context.user_data['available_locations'] = loc_dict

        msg = f"📦 *TÌM THẤY {len(locations)} KHO PHÙ HỢP:*\n\n"
        for loc in locations:
            msg += f"🔹 Gõ `{loc['id']}` - Kho: {loc['display_name']}\n"

        msg += "\n👉 *Vui lòng gõ ID kho mà chị muốn xem (Gõ 'hủy' để thoát).* "

        await update.message.reply_text(msg, parse_mode='Markdown')

    except Exception as e:
        logger.error(f"Lỗi khi lấy danh sách kho: {e}")
        await update.message.reply_text(f"❌ Lỗi quét danh sách kho: {e}")

# =====================================================================
# ---> HÀM XUẤT ĐƠN HÀNG THEO TỪ NGÀY TỚI NGÀY <---
# =====================================================================
async def export_orders_by_date_range(update: Update, context: ContextTypes.DEFAULT_TYPE, start_date: str, end_date: str):
    await update.message.reply_text(f"🔍 Đang tổng hợp các đơn hàng từ `{start_date}` đến `{end_date}`...", parse_mode='Markdown')
    uid, models, error_msg = connect_odoo()
    if not uid:
        await update.message.reply_text(f"❌ Lỗi kết nối Odoo: {error_msg}")
        return

    try:
        domain = [
            ('date_order', '>=', f"{start_date} 00:00:00"),
            ('date_order', '<=', f"{end_date} 23:59:59")
        ]
        
        orders = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'sale.order', 'search_read',
            [domain],
            {'fields': ['name', 'partner_id', 'state', 'date_order', 'amount_total'], 'order': 'date_order asc'}
        )

        if not orders:
            await update.message.reply_text(f"📭 Iem không tìm thấy đơn hàng nào trong khoảng từ {start_date} đến {end_date} ạ.")
            return

        rows = []
        state_map = {'draft': 'Nháp', 'sent': 'Đã gửi báo giá', 'sale': 'Đã chốt', 'done': 'Hoàn thành', 'cancel': 'Đã hủy'}
        
        for o in orders:
            rows.append({
                'Mã Đơn Hàng': o['name'],
                'Khách Hàng': o['partner_id'][1] if o.get('partner_id') else 'N/A',
                'Ngày Lên Đơn': o['date_order'],
                'Trạng Thái': state_map.get(o['state'], o['state']),
                'Tổng Tiền': o.get('amount_total', 0)
            })

        df = pd.DataFrame(rows)
        buf = io.BytesIO()
        df.to_excel(buf, index=False, sheet_name='Thống Kê Đơn Hàng')
        buf.seek(0)

        await update.message.reply_document(
            document=buf,
            filename=f"Thong_Ke_Don_Hang_{start_date}_den_{end_date}.xlsx",
            caption=f"📊 Iem đã tổng hợp xong! Tổng cộng có {len(orders)} đơn hàng trong khoảng thời gian này nhé."
        )

    except Exception as e:
        logger.error(f"Lỗi xuất đơn hàng theo ngày: {e}")
        await update.message.reply_text(f"❌ Lỗi xuất Excel: {e}")

# =====================================================================
# ---> [NEW FEATURE] KIỂM TRA ĐƠN HÀNG <---
# =====================================================================
async def check_single_order(update: Update, context: ContextTypes.DEFAULT_TYPE, order_code: str):
    await update.message.reply_text(f"🔍 Đang truy xuất thông tin đơn hàng `*{order_code}*`...", parse_mode='Markdown')
    uid, models, error_msg = connect_odoo()
    if not uid:
        await update.message.reply_text(f"❌ Lỗi kết nối Odoo: {error_msg}")
        return

    try:
        orders = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'sale.order', 'search_read',
            [[('name', 'ilike', order_code)]],
            {'fields': ['name', 'partner_id', 'state', 'date_order', 'amount_total', 'order_line']}
        )

        if not orders:
            await update.message.reply_text(f"📭 Iem không tìm thấy đơn hàng nào khớp với mã `*{order_code}*` trên hệ thống ạ.", parse_mode='Markdown')
            return

        o = orders[0]
        state_map = {
            'draft': 'Nháp / Báo giá',
            'sent': 'Đã gửi báo giá',
            'sale': 'Đã chốt (Sale Order)',
            'done': 'Đã khóa / Hoàn thành',
            'cancel': 'Đã hủy'
        }
        state_vn = state_map.get(o['state'], o['state'])
        
        lines = []
        if o.get('order_line'):
            lines = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                'sale.order.line', 'read',
                [o['order_line']],
                {'fields': ['product_id', 'product_uom_qty', 'qty_delivered', 'price_subtotal']}
            )

        msg = f"🧾 **THÔNG TIN ĐƠN HÀNG: {o['name']}**\n"
        msg += f"👤 Khách hàng: {o['partner_id'][1] if o.get('partner_id') else 'Không xác định'}\n"
        msg += f"📅 Ngày lập: {o['date_order']}\n"
        msg += f"✅ Trạng thái: {state_vn}\n"
        msg += f"💰 Tổng tiền: {o['amount_total']:,.0f} VNĐ\n\n"
        msg += "📦 **CHI TIẾT SẢN PHẨM:**\n"
        
        if not lines:
            msg += "Đơn hàng chưa có sản phẩm nào."
        else:
            for i, l in enumerate(lines, 1):
                pname = l['product_id'][1] if l.get('product_id') else 'Không rõ'
                qty = l.get('product_uom_qty', 0)
                deliv = l.get('qty_delivered', 0)
                msg += f"{i}. {pname}\n   ▫️ SL đặt: {qty} | Đã giao: {deliv}\n"

        await update.message.reply_text(msg, parse_mode='Markdown')

    except Exception as e:
        logger.error(f"Lỗi kiểm tra đơn hàng: {e}")
        await update.message.reply_text(f"❌ Lỗi truy xuất đơn hàng: {e}")


async def export_customer_orders(update: Update, context: ContextTypes.DEFAULT_TYPE, customer_name: str):
    await update.message.reply_text(f"🔍 Iem đang tìm kiếm tối đa 20 đơn hàng gần nhất của khách hàng `*{customer_name}*`...", parse_mode='Markdown')
    uid, models, error_msg = connect_odoo()
    if not uid:
        await update.message.reply_text(f"❌ Lỗi kết nối Odoo: {error_msg}")
        return

    try:
        # 1. Tìm thông vị trí khách hàng
        partners = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'res.partner', 'search_read',
            [[('name', 'ilike', customer_name)]],
            {'fields': ['id', 'name']}
        )
        
        if not partners:
            await update.message.reply_text(f"📭 Iem không tìm thấy khách hàng nào tên là `*{customer_name}*` trên hệ thống ạ.", parse_mode='Markdown')
            return
            
        p_ids = [p['id'] for p in partners]

        # 2. Tìm Sale Orders của khách
        orders = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'sale.order', 'search_read',
            [[('partner_id', 'in', p_ids)]],
            {'fields': ['name', 'partner_id', 'state', 'date_order', 'amount_total', 'order_line'], 'limit': 20, 'order': 'date_order desc'}
        )

        if not orders:
            await update.message.reply_text(f"📭 Khách hàng `*{customer_name}*` chưa có đơn đặt hàng nào.", parse_mode='Markdown')
            return

        # 3. Lấy chi tiết line của các đơn
        line_ids = []
        for o in orders:
            line_ids.extend(o.get('order_line', []))

        lines_dict = {}
        if line_ids:
            lines_info = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                'sale.order.line', 'read',
                [line_ids],
                {'fields': ['order_id', 'product_id', 'product_uom_qty', 'qty_delivered', 'price_unit', 'price_subtotal']}
            )
            for l in lines_info:
                oid = l['order_id'][0] if l.get('order_id') else 0
                if oid not in lines_dict: 
                    lines_dict[oid] = []
                lines_dict[oid].append(l)

        state_map = {'draft': 'Nháp', 'sent': 'Đã gửi BG', 'sale': 'Đã chốt', 'done': 'Hoàn thành', 'cancel': 'Đã hủy'}

        rows = []
        for o in orders:
            oid = o['id']
            oname = o['name']
            cname = o['partner_id'][1] if o.get('partner_id') else ''
            date_str = o['date_order']
            st = state_map.get(o['state'], o['state'])
            total_amount = o.get('amount_total', 0)

            o_lines = lines_dict.get(oid, [])
            if not o_lines:
                rows.append({
                    'Mã Đơn': oname, 'Khách Hàng': cname, 'Ngày Đặt': date_str, 'Trạng Thái': st,
                    'Sản Phẩm': 'Không có SP', 'SL Đặt': 0, 'SL Đã Giao': 0, 'Đơn Giá': 0, 'Thành Tiền': 0, 'Tổng Đơn': total_amount
                })
            else:
                for l in o_lines:
                    pname = l['product_id'][1] if l.get('product_id') else ''
                    rows.append({
                        'Mã Đơn': oname, 'Khách Hàng': cname, 'Ngày Đặt': date_str, 'Trạng Thái': st,
                        'Sản Phẩm': pname, 
                        'SL Đặt': l.get('product_uom_qty', 0), 
                        'SL Đã Giao': l.get('qty_delivered', 0), 
                        'Đơn Giá': l.get('price_unit', 0), 
                        'Thành Tiền': l.get('price_subtotal', 0),
                        'Tổng Đơn': total_amount
                    })

        df = pd.DataFrame(rows)
        buf = io.BytesIO()
        df.to_excel(buf, index=False, sheet_name='Lich_Su_Don_Hang')
        buf.seek(0)

        safe_name = "".join(c for c in customer_name if c.isalnum() or c in (' ', '_')).replace(' ', '_')
        await update.message.reply_document(
            document=buf,
            filename=f"Don_Hang_{safe_name}.xlsx",
            caption=f"📊 Iem đã tổng hợp xong {len(orders)} đơn hàng gần nhất của khách `*{customer_name}*` rồi ạ!",
            parse_mode='Markdown'
        )

    except Exception as e:
        logger.error(f"Lỗi xuất đơn hàng khách hàng: {e}")
        await update.message.reply_text(f"❌ Lỗi khi xuất Excel: {e}")


# =====================================================================
# ---> XỬ LÝ TEXT: NLP & CHỌN KHO & KIỂM TRA ĐƠN HÀNG <---
# =====================================================================
async def handle_product_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    register_chat_id(chat_id)

    user_input = update.message.text.strip()
    user_input_lower = user_input.lower()

    # --- 1. Lọc Lệnh Chọn ID Kho cho Đổ Tồn Kho ---
    if context.user_data.get('waiting_for_location'):
        loc_dict = context.user_data.get('available_locations', {})
        if user_input in loc_dict:
            context.user_data['waiting_for_location'] = False
            selected_loc = loc_dict[user_input]
            await update.message.reply_text(f"⌛️ Iem đang gom số liệu tồn cho kho *{selected_loc['display_name']}*...", parse_mode='Markdown')
            await process_export_inventory(update, context, selected_loc['id'], selected_loc['display_name'])
            return
        elif user_input_lower in ['huy', 'hủy', 'cancel']:
            context.user_data['waiting_for_location'] = False
            await update.message.reply_text("✅ Đã hủy lệnh đổ tồn kho nha!")
            return
        else:
            await update.message.reply_text("❌ Mã kho không hợp lệ. Chị vui lòng nhập đúng ID kho trong danh sách hoặc gõ 'hủy' để thoát ạ.")
            return

    # --- 2. Báo Giá (Luồng tĩnh ưu tiên) ---
    if any(k in user_input_lower for k in ['giá', 'bao nhiêu', 'vat', 'bảng giá', 'price']):
        await update.message.reply_text("⌛️ Iem đang tra bảng giá xíu...")
        answer = ask_groq_ai(user_input)
        await update.message.reply_text(answer, parse_mode='Markdown')
        return

    # --- 3. TÌM KIẾM ĐƠN HÀNG QUA TỪ KHÓA TĨNH (Luồng ưu tiên) ---
    if "đơn hàng" in user_input_lower and "của" in user_input_lower:
        khach_hang = user_input_lower.split("của")[-1].strip()
        if khach_hang:
            await export_customer_orders(update, context, khach_hang)
            return

    if user_input_lower.startswith("kiểm tra đơn") or user_input_lower.startswith("check đơn") or user_input_lower.startswith("đơn hàng "):
        ma_don = user_input_lower.replace("kiểm tra đơn hàng", "").replace("kiểm tra đơn", "").replace("check đơn", "")
        if user_input_lower.startswith("đơn hàng "):
            ma_don = user_input_lower.replace("đơn hàng", "")
            
        ma_don = ma_don.strip().split()[0].upper()
        if ma_don:
            await check_single_order(update, context, ma_don)
            return

    # --- 4. GIAO CHO AI PHÂN TÍCH Ý ĐỊNH VÀ GỌI DỮ LIỆU REAL-TIME ---
    ai_intent = analyze_chat_intent(user_input)
    action = ai_intent.get("action")
    
    if action == "export_report":
        start_d = ai_intent.get("start_date")
        end_d = ai_intent.get("end_date")
        await export_orders_by_date_range(update, context, start_d, end_d)
        return
        
    elif action == "weather":
        loc = ai_intent.get("location", "Hà Nội")
        await update.message.reply_text("🌤 Iem đang ngó nghiêng bầu trời lấy thông tin thời tiết thực tế...")
        weather_data = get_realtime_weather(loc)
        final_answer = generate_witty_response(user_input, f"Thời tiết tại {loc}", weather_data)
        await update.message.reply_text(final_answer)
        return
        
    elif action == "news":
        await update.message.reply_text("📰 Đang hóng hớt lướt báo lấy tin nóng nhất cho sếp...")
        news_data = get_realtime_news()
        final_answer = generate_witty_response(user_input, "Tin tức mới nhất", news_data)
        await update.message.reply_text(final_answer)
        return
        
    elif action == "chat":
        await update.message.reply_text(ai_intent.get("response", "Lỗi rồi sếp ơi!"))
        return

    # --- 5. LOGIC ODOO: Tra tồn kho sản phẩm (Fallback nếu AI xác nhận là mã SP hoặc không rõ ý định) ---
    product_code = user_input.upper()
    await update.message.reply_text(f"Đang tra tồn cho `{product_code}`, vui lòng chờ!", parse_mode='Markdown')

    uid, models, error_msg = connect_odoo()
    if not uid:
        await update.message.reply_text(f"❌ lỗi kết nối odoo. chi tiết: `{escape_markdown(error_msg)}`", parse_mode='Markdown')
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
            await update.message.reply_text(f"❌ Không tìm thấy sản phẩm nào có mã `{product_code}`")
            return

        product = products[0]
        product_id = product['id']
        product_name = product['display_name']

        def get_qty_available(location_id):
            if not location_id:
                return 0
            stock_product_info = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                'product.product', 'read',
                [[product_id]],
                {'fields': ['qty_available'], 'context': {'location': location_id}}
            )
            if stock_product_info and stock_product_info[0]:
                return int(round(stock_product_info[0].get('qty_available', 0.0)))
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
        else:
            location_info = []

        loc_map = {l['id']: l for l in location_info}
        stock_details = {}

        for q in quant_data:
            loc_field = q.get('location_id')
            if not loc_field:
                continue

            loc_id = loc_field[0]
            qty = float(q.get('available_quantity', 0.0))
            if qty <= 0:
                continue

            name_loc = (
                loc_map.get(loc_id, {}).get('complete_name')
                or loc_map.get(loc_id, {}).get('display_name')
                or f"ID:{loc_id}"
            )

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
            f"{product_code} {product_name}\n"
            f"Tồn kho HN: {int(hn_stock_qty)}\n"
            f"Tồn kho HCM: {int(hcm_stock_qty)}\n"
            f"Tồn kho nhập Hà Nội: {int(hn_transit_qty)}\n"
            f"=> Đề xuất nhập thêm {int(recommend)} sp để HN đủ tồn {TARGET_MIN_QTY} sản phẩm.\n\n"
            "2/ Tồn kho chi tiết(Có hàng):"
        )

        if final_list:
            for loc_name, qty in final_list:
                msg += f"\n{loc_name}: {qty}"
        else:
            msg += "\nKhông có tồn kho chi tiết lớn hơn 0."

        await update.message.reply_text(msg.strip())

    except Exception as e:
        logger.error(f"lỗi khi tra tồn: {e}")
        await update.message.reply_text(f"❌ lỗi khi tra tồn: {e}")


# ---------------- Telegram Handlers ----------------

def get_daily_movement_report():
    uid, models, error_msg = connect_odoo()
    if not uid:
        return None, error_msg

    try:
        tz_vn = pytz.timezone("Asia/Ho_Chi_Minh")
        now_vn = datetime.now(tz_vn)
        start_date_vn = now_vn.replace(hour=0, minute=0, second=0, microsecond=0)
        
        start_date_utc = start_date_vn.astimezone(pytz.utc).strftime('%Y-%m-%d %H:%M:%S')
        end_date_utc = now_vn.astimezone(pytz.utc).strftime('%Y-%m-%d %H:%M:%S')

        domain = [
            ('state', '=', 'done'),
            ('date', '>=', start_date_utc),
            ('date', '<=', end_date_utc)
        ]
        
        moves = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'stock.move', 'search_read',
            [domain],
            {'fields': [
                'product_id', 'product_uom_qty', 'date', 
                'location_id', 'location_dest_id', 'picking_id', 'write_uid'
            ]}
        )

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
            products_info = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                'product.product', 'search_read',
                [[('id', 'in', product_ids)]],
                {'fields': ['display_name', PRODUCT_CODE_FIELD]}
            )
            product_map = {p['id']: p for p in products_info}

        import_rows = []
        export_rows = []
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

            row_data = {
                'Mã SP': code,
                'Tên SP': name,
                'Số lượng': qty,
                'Thời gian': vn_time_str,
                'Người thao tác': actor,
                'Mã lệnh': picking_name
            }

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
            if df_in.empty:
                df_in = pd.DataFrame(columns=in_cols)
            else:
                df_in = df_in[in_cols]
            df_in.to_excel(writer, index=False, sheet_name='NHẬP KHO')

            df_out = pd.DataFrame(export_rows)
            out_cols = ['Mã SP', 'Tên SP', 'Số lượng', 'Xuất đi đâu', 'Thời gian', 'Người thao tác', 'Mã lệnh']
            if df_out.empty:
                df_out = pd.DataFrame(columns=out_cols)
            else:
                df_out = df_out[out_cols]
            df_out.to_excel(writer, index=False, sheet_name='XUẤT KHO')

        buf.seek(0)
        return buf, "Thành công"
    except Exception as e:
        logger.error(f"Lỗi tạo báo cáo ngày: {e}")
        return None, str(e)


async def daily_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    register_chat_id(chat_id)
    
    await update.message.reply_text("⌛️ Iem đang tổng hợp dữ liệu Xuất/Nhập kho hôm nay...")
    
    excel_buffer, error_msg = get_daily_movement_report()
    
    if excel_buffer:
        today_str = datetime.now(pytz.timezone("Asia/Ho_Chi_Minh")).strftime("%d-%m-%Y")
        await update.message.reply_document(
            document=excel_buffer,
            filename=f"Bao_cao_kho_ngay_{today_str}.xlsx",
            caption=f"📊 Báo cáo luồng hàng Nhập/Xuất ngày {today_str} đã sẵn sàng ạ!"
        )
    else:
        await update.message.reply_text(f"❌ Không thể tạo báo cáo. Chi tiết: {error_msg}")


async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    register_chat_id(chat_id)

    await update.message.reply_text("Đang kiểm tra kết nối odoo, xin chờ...")
    uid, _, error_msg = connect_odoo()
    if uid:
        await update.message.reply_text(f"✅ Thành công! Kết nối Odoo DB: {ODOO_DB}")
    else:
        await update.message.reply_text(f"❌ Lỗi: {error_msg}")


async def excel_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    register_chat_id(chat_id)

    await update.message.reply_text("⌛️ Iem đang xử lý dữ liệu và tạo báo cáo Excel...")
    excel_buffer, item_count, error_msg = get_stock_data()

    if excel_buffer is None:
        await update.message.reply_text(f"❌ Lỗi: {error_msg}")
        return

    if item_count > 0:
        await update.message.reply_document(
            document=excel_buffer,
            filename="de_xuat_keo_hang.xlsx",
            caption=f"Đã tìm thấy {item_count} sản phẩm cần kéo hàng."
        )
    else:
        await update.message.reply_text(
            f"Không có sản phẩm nào cần kéo hàng (đủ tồn {TARGET_MIN_QTY})."
        )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    register_chat_id(chat_id)

    name = update.message.from_user.first_name
    await update.message.reply_text(
        f"Chào {name}!\n"
        "1. Gõ mã sp để tra tồn.\n"
        "2. Hỏi giá sản phẩm để em báo giá.\n"
        "3. Gửi file Excel bảng giá để cập nhật.\n"
        "4. `/keohang` để tạo báo cáo Excel kéo hàng.\n"
        "5. `/checkpo` để đối chiếu tồn kho PO.\n"
        "6. `/baocaongay` để xuất báo cáo Nhập/Xuất cuối ngày.\n"
        "7. `/dotonkho <tên kho>` để xuất tồn 1 kho.\n"
        "8. Gõ `kiểm tra đơn hàng S...` để xem chi tiết 1 đơn.\n"
        "9. Gõ `đơn hàng của [Tên]` để xuất Excel đơn của khách.\n"
        "10. Hỏi bất cứ thông tin nào như thời tiết, tin tức hiện tại.\n"
        "11. Hoặc yêu cầu: 'Tổng hợp đơn hàng từ ngày 2 đến ngày 20'\n"
        "12. `/ping` để kiểm tra kết nối Odoo.",
        parse_mode='Markdown'
    )


async def checkpo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    register_chat_id(chat_id)

    context.user_data['waiting_for_po'] = True
    await update.message.reply_text(
        "Ok, gửi file PO Excel (.xlsx) để em kiểm tra tồn kho theo mẫu đối tác gửi nha!"
    )


async def handle_po_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    register_chat_id(chat_id)

    document = update.message.document
    if not document:
        return

    file_name = (document.file_name or "").lower()
    if not file_name.endswith(".xlsx"):
        await update.message.reply_text("Chỉ hỗ trợ file Excel định dạng .xlsx thôi nha.")
        return

    if context.user_data.get('waiting_for_po'):
        context.user_data['waiting_for_po'] = False
        await update.message.reply_text("⌛️ Iem đang xử lý file PO, chờ em xíu xìu xiu nha...")

        try:
            file = await document.get_file()
            file_bytes = await file.download_as_bytearray()
            excel_buffer, error_msg = process_po_and_build_report(bytes(file_bytes))
            if excel_buffer:
                await update.message.reply_document(
                    document=excel_buffer,
                    filename="kiem_tra_po.xlsx",
                    caption="❤️ Em gửi file kiểm tra PO và đối chiếu tồn kho đây ạ!"
                )
            else:
                await update.message.reply_text(f"❌ Lỗi: {error_msg}")
        except Exception as e:
            await update.message.reply_text(f"❌ Lỗi khi tải file PO: {e}")
        return
    else:
        await update.message.reply_text("📥 Đang nạp bảng giá mới cho AI...")
        try:
            file = await document.get_file()
            file_bytes = await file.download_as_bytearray()
            success, info = process_price_excel(bytes(file_bytes))
            if success:
                await update.message.reply_text(f"✅ Đã nạp thành công bảng giá ({info}). Chị có thể bắt đầu hỏi giá rồi nha!")
            else:
                await update.message.reply_text(f"❌ Lỗi nạp bảng giá: {info}")
        except Exception as e:
            await update.message.reply_text(f"❌ Lỗi xử lý file: {e}")


# ---------------- HTTP Ping Server ----------------
class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

    def log_message(self, format, *args):
        return

def start_http():
    try:
        server = HTTPServer(("0.0.0.0", 10001), PingHandler)
        logger.info("HTTP ping server chạy port 10001")
        server.serve_forever()
    except Exception as e:
        logger.error(f"Lỗi HTTP server: {e}")

threading.Thread(target=start_http, daemon=True).start()

# ---------------- AUTO-PING ----------------
PING_URL = "https://google.com"

def keep_alive_ping():
    while True:
        try:
            urllib.request.urlopen(PING_URL, timeout=10)
            logger.info("Keep-alive ping sent.")
        except Exception as e:
            logger.warning(f"Keep-alive ping failed: {e}")
        time.sleep(300)

threading.Thread(target=keep_alive_ping, daemon=True).start()


# =====================================================================
# ---> WATCHDOG GOM NHÓM (BATCHING) CHO TẤT CẢ CÁC KHO HÀ NỘI <---
# =====================================================================
last_move_id = 0

def watchdog_batch():
    global last_move_id
    tz = pytz.timezone("Asia/Ho_Chi_Minh")
    WATCH_INTERVAL = 60

    while True:
        try:
            uid, models, err = connect_odoo()
            if not uid:
                logger.error(f"Watchdog không kết nối được Odoo: {err}")
                time.sleep(WATCH_INTERVAL)
                continue

            # 1. Khởi tạo mốc ID mới nhất khi Bot vừa chạy
            if last_move_id == 0:
                latest_move = models.execute_kw(
                    ODOO_DB, uid, ODOO_PASSWORD,
                    'stock.move', 'search_read',
                    [[('state', '=', 'done')]],
                    {'fields': ['id'], 'limit': 1, 'order': 'id desc'}
                )
                if latest_move:
                    last_move_id = latest_move[0]['id']
                else:
                    last_move_id = -1
                time.sleep(WATCH_INTERVAL)
                continue

            # 2. Tìm các lệnh Done mới sinh ra sau mốc ID
            new_moves = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                'stock.move', 'search_read',
                [[('id', '>', last_move_id), ('state', '=', 'done')]],
                {'fields': ['id', 'product_id', 'product_uom_qty', 'location_id', 'location_dest_id', 'picking_id', 'write_uid', 'date']}
            )

            if not new_moves:
                time.sleep(WATCH_INTERVAL)
                continue

            # Cập nhật ID lớn nhất
            max_id = max(m['id'] for m in new_moves)
            last_move_id = max(last_move_id, max_id)

            # Hàm nhận diện kho Hà Nội (Chứa 201 hoặc chữ HN)
            def is_hn_loc(name):
                n = str(name).upper()
                return '201' in n or 'HN' in n or 'HÀ NỘI' in n or 'HA NOI' in n

            # 3. Gom nhóm theo Picking (Phiếu)
            groups = {}
            for m in new_moves:
                src = m.get('location_id')
                dest = m.get('location_dest_id')
                if not src or not dest: continue

                src_name = src[1]
                dest_name = dest[1]

                is_src_hn = is_hn_loc(src_name)
                is_dest_hn = is_hn_loc(dest_name)

                # Chỉ lấy giao dịch dính dáng tới kho Hà Nội
                if not is_src_hn and not is_dest_hn:
                    continue 

                pick = m.get('picking_id')
                pick_id = pick[0] if pick else f"NOPICK_{src[0]}_{dest[0]}"
                pick_name = pick[1] if pick else "N/A"

                group_key = (pick_id, pick_name, src[0], src_name, dest[0], dest_name)
                if group_key not in groups:
                    groups[group_key] = []
                groups[group_key].append(m)

            if not groups:
                time.sleep(WATCH_INTERVAL)
                continue

            # 4. Xử lý từng nhóm Phiếu và gửi thông báo
            for g_key, moves in groups.items():
                pick_id, pick_name, src_id, src_name, dest_id, dest_name = g_key
                is_src_hn = is_hn_loc(src_name)
                is_dest_hn = is_hn_loc(dest_name)

                # Xét hướng biến động của kho HN
                if is_src_hn and not is_dest_hn:
                    direction = "XUẤT KHO"
                    target_loc_id = src_id
                    target_loc_name = src_name
                    sign = -1
                elif is_dest_hn and not is_src_hn:
                    direction = "NHẬP KHO"
                    target_loc_id = dest_id
                    target_loc_name = dest_name
                    sign = 1
                else: 
                    direction = "ĐIỀU CHUYỂN NỘI BỘ"
                    target_loc_id = dest_id 
                    target_loc_name = f"{src_name} ➡️ {dest_name}"
                    sign = 1

                # Tính tổng biến động từng mã SP
                prod_qtys = {}
                for m in moves:
                    pid = m['product_id'][0]
                    pname = m['product_id'][1]
                    qty = float(m.get('product_uom_qty') or 0.0)
                    if pid not in prod_qtys:
                        prod_qtys[pid] = {'name': pname, 'qty': 0}
                    prod_qtys[pid]['qty'] += qty

                # Lấy chi tiết thông tin Phiếu (Trạng thái & Người thao tác)
                state_vn = "Đã duyệt (Hoàn thành)"
                w_uid = moves[0].get('write_uid')
                actor = w_uid[1] if isinstance(w_uid, list) and len(w_uid) > 1 else "Hệ thống"
                
                move_date = moves[0].get('date')
                if move_date:
                    utc_time = datetime.strptime(move_date, '%Y-%m-%d %H:%M:%S').replace(tzinfo=pytz.utc)
                    vn_time_str = utc_time.astimezone(tz).strftime('%H:%M %d/%m/%Y')
                else:
                    vn_time_str = datetime.now(tz).strftime('%H:%M %d/%m/%Y')

                if pick_id and isinstance(pick_id, int):
                    p_info = models.execute_kw(
                        ODOO_DB, uid, ODOO_PASSWORD,
                        "stock.picking", "read",
                        [[pick_id]],
                        {"fields": ["state", "write_uid"]}
                    )
                    if p_info:
                        raw_state = p_info[0].get('state')
                        state_map = {
                            'draft': 'Nháp (Chưa duyệt)',
                            'waiting': 'Đang chờ (Chưa duyệt)',
                            'confirmed': 'Chờ có hàng (Chưa duyệt)',
                            'assigned': 'Sẵn sàng (Chưa duyệt)',
                            'done': 'Đã duyệt (Hoàn thành)',
                            'cancel': 'Đã hủy'
                        }
                        state_vn = state_map.get(raw_state, raw_state) if raw_state else state_vn
                        p_w_uid = p_info[0].get('write_uid')
                        if p_w_uid: actor = p_w_uid[1]

                # Truy vấn tồn kho Odoo để lấy Tồn Mới
                pids = list(prod_qtys.keys())
                prod_info = models.execute_kw(
                    ODOO_DB, uid, ODOO_PASSWORD,
                    'product.product', 'search_read',
                    [[('id', 'in', pids)]],
                    {'fields': ['id', PRODUCT_CODE_FIELD]}
                )
                pcode_map = {p['id']: p.get(PRODUCT_CODE_FIELD, 'N/A') for p in prod_info}

                loc_to_check = target_loc_id if sign == 1 or direction == "XUẤT KHO" else dest_id
                quants = models.execute_kw(
                    ODOO_DB, uid, ODOO_PASSWORD,
                    'stock.quant', 'search_read',
                    [[('location_id', '=', loc_to_check), ('product_id', 'in', pids)]],
                    {'fields': ['product_id', 'available_quantity']}
                )
                
                quant_map = {}
                for q in quants:
                    pid = q['product_id'][0]
                    quant_map[pid] = quant_map.get(pid, 0) + float(q.get('available_quantity', 0))

                # Build nội dung thông báo
                msg_header = (
                    f"📦 **Cập nhật tồn kho {target_loc_name} – {direction}**\n\n"
                    f"🔖 **Mã lệnh:** {pick_name}\n"
                    f"🏢 **Lệnh đi cho kho:** {dest_name}\n"
                    f"✅ **Trạng thái lệnh:** {state_vn}\n"
                    f"👤 **Người thao tác:** {actor}\n"
                    f"🕒 **Thời gian:** {vn_time_str}\n\n"
                    f"📝 **CHI TIẾT BIẾN ĐỘNG ({len(prod_qtys)} Mã sản phẩm):**\n\n"
                )

                number_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
                
                current_msg = msg_header
                idx = 1
                
                for pid, data in prod_qtys.items():
                    code = pcode_map.get(pid, 'N/A')
                    name = data['name']
                    qty_diff = data['qty']
                    new_ton = int(quant_map.get(pid, 0))
                    
                    emoji = number_emojis[idx-1] if idx <= 10 else f"{idx}."
                    
                    if direction == "XUẤT KHO":
                        diff_str = f"-{int(qty_diff)} SP"
                        icon = "🔻"
                    elif direction == "NHẬP KHO":
                        diff_str = f"+{int(qty_diff)} SP"
                        icon = "🔺"
                    else:
                        diff_str = f"Chuyển {int(qty_diff)} SP"
                        icon = "🔄"

                    line = (
                        f"{emoji} **[{code}]** {name}\n"
                        f"{icon} Biến động: {diff_str}  |  📦 Tồn mới: {new_ton} SP\n\n"
                    )

                    # Băm nhỏ tin nhắn nếu quá dài
                    if len(current_msg) + len(line) > 3800:
                        for chat_id in get_registered_chat_ids():
                            try:
                                bot = Bot(token=TELEGRAM_TOKEN)
                                asyncio.run(bot.send_message(chat_id, current_msg, parse_mode="Markdown"))
                            except Exception as e:
                                logger.error(f"Lỗi gửi thông báo: {e}")
                        current_msg = "" 
                        
                    current_msg += line
                    idx += 1

                # Gửi đoạn tin nhắn cuối cùng
                if current_msg:
                    for chat_id in get_registered_chat_ids():
                        try:
                            bot = Bot(token=TELEGRAM_TOKEN)
                             asyncio.run(bot.send_message(chat_id, current_msg, parse_mode="Markdown"))
                        except Exception as e:
                            logger.error(f"Lỗi gửi thông báo: {e}")

            time.sleep(WATCH_INTERVAL)

        except Exception as e:
            logger.error(f"Lỗi watchdog batch: {e}")
            time.sleep(WATCH_INTERVAL)

threading.Thread(target=watchdog_batch, daemon=True).start()

# ---------------- MAIN ----------------
def main():
    if not TELEGRAM_TOKEN or not ODOO_URL_RAW or not ODOO_DB or not ODOO_USERNAME or not ODOO_PASSWORD:
        logger.error("Thiếu cấu hình môi trường (token, url, db, user, pass).")
        return

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        asyncio.get_event_loop().run_until_complete(bot.delete_webhook())
        logger.info("đã xóa webhook cũ (nếu có).")
    except Exception as e:
        logger.warning(f"Lỗi xóa webhook: {e}")

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", start_command))
    application.add_handler(CommandHandler("ping", ping_command))
    application.add_handler(CommandHandler("keohang", excel_report_command))
    application.add_handler(CommandHandler("checkpo", checkpo_command))
    application.add_handler(CommandHandler("baocaongay", daily_report_command))
    application.add_handler(CommandHandler("dotonkho", dotonkho_command))  
    
    application.add_handler(MessageHandler(filters.Document.ALL, handle_po_file))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_product_code))

    logger.info("Bot started!")
    application.run_polling()


if __name__ == "__main__":
    main()
