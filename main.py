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
from telegram import Update, Bot, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler, CallbackQueryHandler
import pytz
import json
import re
from groq import Groq

# ---------------- Trạng thái Hội thoại Lên đơn ----------------
LENDON_CUSTOMER, LENDON_REF, LENDON_PRODUCTS, LENDON_WAREHOUSE_SEARCH = range(4)

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

# =====================================================================
# ---> CẤU HÌNH LƯU TRỮ ĐÁM MÂY (JSONBIN) BẢO TOÀN DỮ LIỆU <---
# =====================================================================
JSONBIN_API_KEY = os.environ.get('JSONBIN_API_KEY')
JSONBIN_BIN_ID = os.environ.get('JSONBIN_BIN_ID')

# Bộ nhớ đệm chạy trên RAM
cloud_data = {
    "sales_mapping": {} 
}

def load_cloud_db():
    global cloud_data, JSONBIN_BIN_ID
    if not JSONBIN_API_KEY:
        logger.info("Chưa có JSONBIN_API_KEY. Bot sẽ không đồng bộ mây.")
        return
    try:
        if JSONBIN_BIN_ID:
            url = f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}"
            headers = {"X-Master-Key": JSONBIN_API_KEY}
            res = requests.get(url, headers=headers)
            if res.status_code == 200:
                record = res.json().get('record')
                if record:
                    cloud_data = record
                    # Phục hồi file Excel giá nếu có trên Đám mây để hàm AI cũ đọc được
                    if "price_cache" in cloud_data:
                        with open("price_cache.json", 'w', encoding='utf-8') as f:
                            json.dump(cloud_data["price_cache"], f, ensure_ascii=False, indent=4)
                    logger.info("✅ Load Cloud DB thành công! Dữ liệu được bảo toàn.")
    except Exception as e:
        logger.error(f"Lỗi tải Cloud DB: {e}")

async def save_cloud_db(context=None, chat_id=None):
    global JSONBIN_BIN_ID
    if not JSONBIN_API_KEY:
        return
        
    headers = {"X-Master-Key": JSONBIN_API_KEY, "Content-Type": "application/json"}
    try:
        # Nếu đang có file giá, tự động nạp nó vào Đám mây
        if os.path.exists("price_cache.json"):
            with open("price_cache.json", 'r', encoding='utf-8') as f:
                cloud_data["price_cache"] = json.load(f)

        if JSONBIN_BIN_ID:
            requests.put(f"https://api.jsonbin.io/v3/b/{JSONBIN_BIN_ID}", headers=headers, json=cloud_data)
            logger.info("Đã cập nhật dữ liệu lên Đám mây JSONBin.")
        else:
            # Tự động tạo kho lưu trữ mới nếu chưa có BIN_ID
            res = requests.post("https://api.jsonbin.io/v3/b", headers=headers, json=cloud_data)
            if res.status_code == 200:
                JSONBIN_BIN_ID = res.json().get('metadata', {}).get('id')
                logger.info(f"New Bin Created: {JSONBIN_BIN_ID}")
                if context and chat_id:
                    msg = (
                        f"☁️ **HỆ THỐNG ĐÃ TẠO Ổ ĐĨA MÂY MỚI!**\n\n"
                        f"Hãy copy đoạn mã ID này: `{JSONBIN_BIN_ID}`\n"
                        f"Và thêm vào Environment trên Render với tên biến là `JSONBIN_BIN_ID` nhé.\n"
                        f"*(Thêm xong thì Render khởi động lại vô tư, không bao giờ mất Bảng giá hay Báo danh nữa!)*"
                    )
                    await context.bot.send_message(chat_id=chat_id, text=msg, parse_mode='Markdown')
    except Exception as e:
        logger.error(f"Lỗi lưu Cloud DB: {e}")


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
        return "Chưa có dữ liệu bảng giá. Hãy gửi file Excel để nạp!"

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
            return "Không tìm thấy mã hàng này trong bảng giá."

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
        url = f"https://wttr.in/{urllib.parse.quote(location)}?format=%l:+%C,+Nhiệt+độ:+%t,+Cảm+giác+như:+%f,+Độ+ẩm:+%h&m"
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            return res.text.strip()
        return "Hiện tại không lấy được dữ liệu thời tiết thực tế."
    except Exception:
        return "Lỗi kết nối khi lấy thời tiết."

def perform_web_search(query):
    """Sử dụng duckduckgo-search phiên bản mở rộng để lấy nhiều tin tức hơn"""
    try:
        from duckduckgo_search import DDGS
        info = ""
        with DDGS() as ddgs:
            # Ưu tiên lấy News (Tin tức) trước, tối đa 5 bài mới nhất
            news_results = list(ddgs.news(query, region='wt-wt', safesearch='off', timelimit='d', max_results=5))
            if news_results:
                info += "📰 **TIN TỨC MỚI NHẤT:**\n"
                for res in news_results:
                    info += f"- {res.get('title', '')}: {res.get('body', '')}\n"
            
            # Cào thêm Web thông thường (Web Search) để lấy thêm ngữ cảnh chung
            web_results = list(ddgs.text(query, region='wt-wt', safesearch='off', timelimit='d', max_results=3))
            if web_results:
                info += "\n🌐 **THÔNG TIN WEB BỔ SUNG:**\n"
                for res in web_results:
                    info += f"- {res.get('title', '')}: {res.get('body', '')}\n"
        
        if not info.strip():
            return "Không tìm thấy thông tin mới nhất trên mạng cho từ khóa này."
        
        return info
    except ImportError:
        return "Sếp ơi, em chưa lướt web được! Sếp nhớ thêm 'duckduckgo-search' vào file requirements.txt rồi deploy lại nhé."
    except Exception as e:
        return f"Lỗi khi lướt web tìm kiếm: {e}"

def analyze_chat_intent(user_input):
    global current_key_index
    tz_vn = pytz.timezone("Asia/Ho_Chi_Minh")
    current_time_str = datetime.now(tz_vn).strftime("%Y-%m-%d %H:%M:%S")
    
    system_prompt = f"""
    Bạn là bộ não điều hướng. Thời gian hiện tại: {current_time_str}.
    Nhiệm vụ của bạn là phân tích câu nói của người dùng và trả về DUY NHẤT một chuỗi JSON hợp lệ. KHÔNG giải thích.
    
    Quy tắc phân loại (QUAN TRỌNG):
    1. Nếu yêu cầu THỐNG KÊ / BÁO CÁO ĐƠN HÀNG từ ngày này đến ngày khác:
    -> {{"action": "export_report", "start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD"}}
    
    2. Nếu yêu cầu XUẤT ĐƠN HÀNG của MỘT KHÁCH HÀNG cụ thể (VD: "Đơn hàng HC", "Đơn hàng của anh Tuấn", "Cho xem đơn VHC"):
    -> {{"action": "export_customer_orders", "customer_name": "Tên khách hàng cần tìm (VD: HC, VHC, Tuấn)"}}
    
    3. Nếu yêu cầu KIỂM TRA CHI TIẾT 1 MÃ ĐƠN HÀNG cụ thể (VD: "Kiểm tra đơn SO001", "Check đơn S12345"):
    -> {{"action": "check_single_order", "order_code": "Mã đơn hàng"}}
    
    4. Nếu người dùng hỏi về THỜI TIẾT:
    -> {{"action": "weather", "location": "Tên địa phương"}} (mặc định là 'Hà Nội' nếu không rõ)
    
    5. Nếu người dùng hỏi TIN TỨC, thời sự, thể thao, giá vàng, hoặc cần tra cứu kiến thức mạng:
    -> {{"action": "web_search", "query": "Từ khóa tìm kiếm tối ưu (ngắn gọn, tập trung)"}}
    
    6. Nếu câu lệnh CHỈ LÀ MÃ SẢN PHẨM (chuỗi ngắn, liền nhau, vd: 'SP01', 'IPHONE12', 'A123'):
    -> {{"action": "stock_search"}}
    
    7. Nếu là câu giao tiếp bình thường (chào hỏi, tâm sự, trêu đùa không cần cào mạng):
    -> {{"action": "chat", "response": "Câu trả lời dí dỏm, thông minh của bạn"}}
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
    Bạn là một trợ lý AI thông minh, dí dỏm, làm việc cho sếp.
    Người dùng vừa hỏi về: {topic}. 
    Dưới đây là THÔNG TIN THỰC TẾ CHÍNH XÁC được cào từ Internet:
    ---
    {real_data}
    ---
    Nhiệm vụ: Trả lời câu hỏi '{user_input}'.
    
    LUẬT THÉP:
    1. Tổng hợp thông tin từ dữ liệu được cung cấp một cách khéo léo, tự nhiên như người thật đang đọc báo cho sếp nghe. KHÔNG copy paste nguyên xi.
    2. Nếu thông tin cào được bị thiếu hoặc không rõ ràng, hãy trả lời dựa trên những gì tốt nhất có được và thành thật báo sếp là tin này chưa đầy đủ.
    3. Nếu là THỜI TIẾT: Phải bắt buộc dùng đúng ĐỘ C (°C). Tùy vào nhiệt độ mà than vãn hoặc trêu đùa.
    4. Giọng văn dí dỏm, chuyên nghiệp nhưng thân thiện. Có thể nịnh sếp nhẹ nhàng 1 câu ở cuối.
    5. KHÔNG VIẾT DÀI DÒNG. Tối đa 4-5 câu.
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
            return f"Thông tin nguyên bản đây sếp ơi: \n{real_data}"
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
            caption=f"📊 Em gửi file thống kê tồn kho của *{loc_name}* ạ!\nTổng cộng có {len(df)} mã sản phẩm đang có hàng.",
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
            "Vui lòng gõ lệnh kèm theo **từ khóa** tên kho!\n\n"
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
            await update.message.reply_text(f"✅ Tìm thấy đúng 1 kho: *{loc['display_name']}*\n⌛️ Em đang gom số liệu tồn...", parse_mode='Markdown')
            await process_export_inventory(update, context, loc['id'], loc['display_name'])
            return

        loc_dict = {str(loc['id']): loc for loc in locations}
        context.user_data['waiting_for_location'] = True
        context.user_data['available_locations'] = loc_dict

        msg = f"📦 *TÌM THẤY {len(locations)} KHO PHÙ HỢP:*\n\n"
        for loc in locations:
            msg += f"🔹 Gõ `{loc['id']}` - Kho: {loc['display_name']}\n"

        msg += "\n👉 *Vui lòng gõ ID kho muốn xem (Gõ 'hủy' để thoát).* "

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
            await update.message.reply_text(f"📭 Em không tìm thấy đơn hàng nào trong khoảng từ {start_date} đến {end_date} ạ.")
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
            caption=f"📊 Em đã tổng hợp xong! Tổng cộng có {len(orders)} đơn hàng trong khoảng thời gian này nhé."
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
            await update.message.reply_text(f"📭 Em không tìm thấy đơn hàng nào khớp với mã `*{order_code}*` trên hệ thống ạ.", parse_mode='Markdown')
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
    await update.message.reply_text(f"🔍 Em đang tìm kiếm tối đa 20 đơn hàng gần nhất của khách hàng `*{customer_name}*`...", parse_mode='Markdown')
    uid, models, error_msg = connect_odoo()
    if not uid:
        await update.message.reply_text(f"❌ Lỗi kết nối Odoo: {error_msg}")
        return

    try:
        partners = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'res.partner', 'search_read',
            [[('name', 'ilike', customer_name)]],
            {'fields': ['id', 'name']}
        )
        
        if not partners:
            await update.message.reply_text(f"📭 Em không tìm thấy khách hàng nào tên là `*{customer_name}*` trên hệ thống ạ.", parse_mode='Markdown')
            return
            
        p_ids = [p['id'] for p in partners]

        orders = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'sale.order', 'search_read',
            [[('partner_id', 'in', p_ids)]],
            {'fields': ['name', 'partner_id', 'state', 'date_order', 'amount_total', 'order_line'], 'limit': 20, 'order': 'date_order desc'}
        )

        if not orders:
            await update.message.reply_text(f"📭 Khách hàng `*{customer_name}*` chưa có đơn đặt hàng nào.", parse_mode='Markdown')
            return

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
            caption=f"📊 Em đã tổng hợp xong {len(orders)} đơn hàng gần nhất của khách `*{customer_name}*` rồi ạ!",
            parse_mode='Markdown'
        )

    except Exception as e:
        logger.error(f"Lỗi xuất đơn hàng khách hàng: {e}")
        await update.message.reply_text(f"❌ Lỗi khi xuất Excel: {e}")

# =====================================================================
# ---> LỆNH BÁO DANH ĐỊNH DANH NHÂN VIÊN <---
# =====================================================================
async def baodanh_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.message.chat_id)
    register_chat_id(chat_id)
    
    args = context.args
    if not args:
        await update.message.reply_text(
            "💡 **Hướng dẫn Báo Danh:**\nVui lòng gõ lệnh kèm theo **Email đăng nhập Odoo** của Sếp.\n"
            "👉 *Ví dụ:* `/baodanh kinhdoanh09@nguonsongviet.vn`", 
            parse_mode='Markdown'
        )
        return
        
    email = args[0].strip()
    await update.message.reply_text(f"🔍 Đang tra cứu tài khoản nhân viên `{email}` trên Odoo...", parse_mode='Markdown')
    
    uid, models, err = connect_odoo()
    if not uid:
        await update.message.reply_text(f"❌ Lỗi kết nối Odoo: {err}")
        return
        
    try:
        users = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'res.users', 'search_read',
                                  [[('login', '=', email)]], {'fields': ['id', 'name'], 'limit': 1})
        if not users:
            await update.message.reply_text(f"❌ Không tìm thấy nhân viên nào sử dụng Email đăng nhập là `{email}` trên hệ thống Odoo.", parse_mode='Markdown')
            return
            
        odoo_user = users[0]
        
        # Ghi vào Mây RAM
        cloud_data['sales_mapping'][chat_id] = {
            'odoo_user_id': odoo_user['id'],
            'name': odoo_user['name'],
            'email': email
        }
        
        await update.message.reply_text(
            f"✅ **BÁO DANH THÀNH CÔNG!**\n\n"
            f"Hệ thống đã kết nối tài khoản Telegram này với hồ sơ Chuyên viên Sales: **{odoo_user['name']}** (Odoo ID: {odoo_user['id']}).\n"
            f"Từ giờ Sếp có thể dùng lệnh `/lendon` được rồi nhé!", 
            parse_mode='Markdown'
        )
        
        # Đồng bộ lưu trữ Mây
        await save_cloud_db(context, update.message.chat_id)
        
    except Exception as e:
        await update.message.reply_text(f"❌ Lỗi tra cứu: {e}")

# =====================================================================
# ---> XỬ LÝ TEXT CHÍNH: CỔNG ĐIỀU HƯỚNG AI & TÌM KẾM <---
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
            await update.message.reply_text(f"⌛️ Em đang gom số liệu tồn cho kho *{selected_loc['display_name']}*...", parse_mode='Markdown')
            await process_export_inventory(update, context, selected_loc['id'], selected_loc['display_name'])
            return
        elif user_input_lower in ['huy', 'hủy', 'cancel']:
            context.user_data['waiting_for_location'] = False
            await update.message.reply_text("✅ Đã hủy lệnh đổ tồn kho nha!")
            return
        else:
            await update.message.reply_text("❌ Mã kho không hợp lệ. NGU. Nhập đúng ID kho trong danh sách hoặc gõ 'hủy' để thoát.")
            return

    # --- 2. Báo Giá (Luồng tĩnh ưu tiên) ---
    if any(k in user_input_lower for k in ['giá', 'bao nhiêu', 'vat', 'bảng giá', 'price']):
        await update.message.reply_text("⌛️ Em đang tra bảng giá xíu...")
        answer = ask_groq_ai(user_input)
        await update.message.reply_text(answer, parse_mode='Markdown')
        return

    # --- 3. GIAO CHO AI PHÂN TÍCH Ý ĐỊNH VÀ ĐIỀU HƯỚNG ---
    ai_intent = analyze_chat_intent(user_input)
    action = ai_intent.get("action")
    
    if action == "export_customer_orders":
        customer_name = ai_intent.get("customer_name", "").strip()
        if customer_name:
            await export_customer_orders(update, context, customer_name)
        else:
            await update.message.reply_text("Sếp muốn tra đơn của khách nào ạ? Gõ tên khách cho em với nhé!")
        return
        
    elif action == "check_single_order":
        order_code = ai_intent.get("order_code", "").strip().upper()
        if order_code:
            await check_single_order(update, context, order_code)
        else:
            await update.message.reply_text("Sếp ném mã đơn (VD: SO001) đây để em check cho nóng!")
        return

    elif action == "export_report":
        start_d = ai_intent.get("start_date")
        end_d = ai_intent.get("end_date")
        await export_orders_by_date_range(update, context, start_d, end_d)
        return
        
    elif action == "weather":
        loc = ai_intent.get("location", "Hà Nội")
        await update.message.reply_text("🌤 Đang đưa mặt ra ngoài cửa sổ đo thời tiết cho Sếp...")
        weather_data = get_realtime_weather(loc)
        final_answer = generate_witty_response(user_input, f"Thời tiết tại {loc}", weather_data)
        await update.message.reply_text(final_answer)
        return
        
    elif action == "news" or action == "web_search":
        search_query = ai_intent.get("query", user_input)
        await update.message.reply_text(f"📰 Đang lướt mạng tra cứu '{search_query}' cho Sếp...")
        news_data = perform_web_search(search_query)
        final_answer = generate_witty_response(user_input, "Thông tin mạng hiện tại", news_data)
        await update.message.reply_text(final_answer)
        return
        
    elif action == "chat":
        await update.message.reply_text(ai_intent.get("response", "Lỗi rồi Sếp ơi!"))
        return

    # --- 4. LOGIC ODOO: Tra tồn kho sản phẩm (Fallback) ---
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
            f"=> Đề xuất nhập thêm {int(recommend)} sp để HN đủ tồn {TARGET_MIN_QTY} sp.\n\n"
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
    
    await update.message.reply_text("⌛️ Em đang tổng hợp dữ liệu Xuất/Nhập kho hôm nay...")
    
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

    await update.message.reply_text("⌛️ Em đang xử lý dữ liệu và tạo báo cáo Excel...")
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
        f"Chào con vợ {name}!\n"
        "1. Gõ mã sp để tra tồn.\n"
        "2. Hỏi giá sản phẩm để em báo giá.\n"
        "3. Gửi file Excel bảng giá để cập nhật.\n"
        "4. `/keohang` để tạo báo cáo Excel kéo hàng.\n"
        "5. `/checkpo` để đối chiếu tồn kho PO.\n"
        "6. `/baocaongay` để xuất báo cáo Nhập/Xuất cuối ngày.\n"
        "7. `/dotonkho <tên kho>` để xuất tồn 1 kho.\n"
        "8. `/baodanh <email>` để báo danh nhân viên Odoo.\n"
        "9. `/lendon` Form lên đơn hàng chuẩn Odoo từng bước.\n"
        "10. Gõ tên khách (VD:Đơn hàng HC) để xuất Excel đơn của khách.\n"
        "11. Gõ mã đơn (VD:Kiểm tra đơn SO001) để xem chi tiết.\n"
        "12. Hỏi bất cứ thông tin nào (World Cup, tin tức, thời tiết...).\n"
        "13. Hoặc yêu cầu: 'Tổng hợp đơn hàng từ ngày 2 đến ngày 20'\n"
        "14. `/ping` để kiểm tra kết nối Odoo.",
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
        await update.message.reply_text("⌛️ Em đang xử lý file PO, chờ em nha...")

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
                # Đồng bộ JSONBin sau khi nạp bảng giá
                await save_cloud_db(context, chat_id)
                await update.message.reply_text(f"✅ Đã nạp thành công bảng giá ({info}). Các con vợ có thể bắt đầu hỏi giá rồi nha!")
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

# =====================================================================
# ---> LOGIC TÍNH NĂNG FORM LÊN ĐƠN (NÚT BẤM) <---
# =====================================================================

def parse_order_products_ai(raw_text):
    global current_key_index
    prompt = f"""
    Văn bản sản phẩm thô: "{raw_text}"
    Nhiệm vụ: Hãy trích xuất các sản phẩm, số lượng, và phần trăm chiết khấu (nếu có) thành chuỗi JSON hợp lệ.
    Định dạng JSON trả về bắt buộc phải là một mảng Object có dạng:
    [
        {{"code": "MÃ_SP_VIẾT_HOA", "qty": SỐ_LƯỢNG_SỐ_NGUYÊN, "discount": PHẦN_TRĂM_CK_SỐ_THỰC_HOẶC_0}}
    ]
    KHÔNG GIẢI THÍCH, CHỈ TRẢ VỀ DUY NHẤT CHUỖI JSON.
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
                temperature=0.0,
                response_format={"type": "json_object"}
            )
            res_json = json.loads(completion.choices[0].message.content)
            key = list(res_json.keys())[0] if res_json.keys() else None
            if isinstance(res_json, list): return res_json
            if isinstance(res_json.get(key), list): return res_json[key]
            return []
        except Exception as e:
            logger.error(f"Lỗi AI parse hàng hóa: {e}")
            current_key_index = (current_key_index + 1) % 3
    return []

async def start_lendon_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = str(update.message.chat_id)
    register_chat_id(chat_id)
    
    if chat_id not in cloud_data.get('sales_mapping', {}):
        await update.message.reply_text(
            "❌ **Sếp chưa Báo danh Chuyên viên Sales!**\n\n"
            "Vui lòng gõ lệnh `/baodanh <email_odoo_của_sếp>` để hệ thống nhận diện danh tính trước khi lên đơn nhé.\n"
            "*(Ví dụ: /baodanh kinhdoanh09@nguonsongviet.vn)*",
            parse_mode='Markdown'
        )
        return ConversationHandler.END

    context.user_data['odoo_salesperson'] = cloud_data['sales_mapping'][chat_id]
    context.user_data['lendon_data'] = {}
    
    await update.message.reply_text(
        "📝 **[BƯỚC 1/3] - KHÁCH HÀNG**\n"
        "Sếp vui lòng gõ tên Khách Hàng hoặc chuỗi điện máy cần lên đơn nhé:\n"
        "*(Hoặc gõ /cancel để hủy bỏ Form)*",
        parse_mode='Markdown'
    )
    return LENDON_CUSTOMER

async def lendon_customer_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['lendon_data']['customer_raw'] = update.message.text.strip()
    await update.message.reply_text(
        "📝 **[BƯỚC 2/3] - THAM CHIẾU & GIAO HÀNG**\n"
        "Sếp nhập thông tin tham chiếu, địa chỉ giao hoặc lời dặn kho (Gõ `0` nếu muốn bỏ qua):",
        parse_mode='Markdown'
    )
    return LENDON_REF

async def lendon_ref_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ref_txt = update.message.text.strip()
    context.user_data['lendon_data']['ref_raw'] = "" if ref_txt == "0" else ref_txt
    await update.message.reply_text(
        "📦 **[BƯỚC 3/3] - DANH SÁCH SẢN PHẨM**\n"
        "Sếp copy paste danh sách mã hàng kèm số lượng và chiết khấu (nếu có) nhé:\n"
        "*(Ví dụ:\nI-28: 3\nAC-350: 5 ck 2%)*",
        parse_mode='Markdown'
    )
    return LENDON_PRODUCTS

async def lendon_products_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    products_raw = update.message.text.strip()
    loading_msg = await update.message.reply_text("⌛️ Em đang đối chiếu dữ liệu Odoo và bóc tách AI, sếp đợi xíu...")
    
    cust_raw = context.user_data['lendon_data']['customer_raw']
    ref_raw = context.user_data['lendon_data']['ref_raw']
    
    parsed_items = parse_order_products_ai(products_raw)
    
    uid, models, err = connect_odoo()
    partner_id, partner_name = None, cust_raw
    if uid:
        try:
            partners = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'res.partner', 'search_read', 
                                        [[('name', 'ilike', cust_raw)]], {'fields': ['id', 'name'], 'limit': 1})
            if partners:
                partner_id = partners[0]['id']
                partner_name = partners[0]['name']
        except Exception as e:
            logger.error(f"Lỗi tìm đối tác Odoo: {e}")

    context.user_data['lendon_form'] = {
        'partner_id': partner_id,
        'customer_name': partner_name,
        'ref': ref_raw,
        'products': parsed_items,
        'odoo_salesperson': context.user_data['odoo_salesperson'],
        'warehouse_id': None,
        'warehouse_name': "❌ CHƯA CHỌN",
        'channel_id': None,
        'channel_name': "❌ CHƯA CHỌN",
        'pos_id': None,
        'pos_name': "❌ CHƯA CHỌN"
    }
    
    await loading_msg.delete()
    await send_lendon_inline_form(update, context)
    return ConversationHandler.END

async def send_lendon_inline_form(update: Update, context: ContextTypes.DEFAULT_TYPE, query=None):
    form = context.user_data['lendon_form']
    
    prod_txt = ""
    for idx, p in enumerate(form['products'], 1):
        ck_txt = f" (CK: {p['discount']}% )" if p['discount'] > 0 else ""
        prod_txt += f"   {idx}. {p['code']} | SL: **{p['qty']}**{ck_txt}\n"
        
    text_form = (
        f"🧾 **FORM ĐIỀU KHIỂN LÊN ĐƠN HÀNG ODOO**\n\n"
        f"👤 **Sales:** {form['odoo_salesperson']['name']}\n"
        f"🏢 **Khách hàng:** {form['customer_name']}\n"
        f"📝 **Tham chiếu:** {form['ref'] if form['ref'] else '⚙️ Tự động'}\n"
        f"🏭 **Kho xuất:** {form['warehouse_name']}\n"
        f"🏷 **Kênh bán:** {form['channel_name']}\n"
        f"🏬 **Mã điểm POS:** {form['pos_name']}\n\n"
        f"📦 **Chi tiết hàng hóa:**\n{prod_txt}\n"
    )
    
    keyboard = []
    
    keyboard.append([
        InlineKeyboardButton("🏭 Kho HN (201)", callback_data="set_wh_201"),
        InlineKeyboardButton("🏭 Kho HCM (124)", callback_data="set_wh_124"),
        InlineKeyboardButton("🔍 Tìm kho khác...", callback_data="search_wh_open")
    ])
    
    keyboard.append([
        InlineKeyboardButton("🏷 Kênh: ĐIỆN MÁY", callback_data="set_chan_dienmay"),
        InlineKeyboardButton("🏷 Kênh: ONLINE", callback_data="set_chan_online")
    ])
    
    if form['channel_name'] == "ĐIỆN MÁY":
        keyboard.append([
            InlineKeyboardButton("🏬 POS: Điện Máy - ECO", callback_data="set_pos_eco"),
            InlineKeyboardButton("🏬 POS: Điện Máy - HC", callback_data="set_pos_hc")
        ])
    elif form['channel_name'] == "ONLINE":
        keyboard.append([
            InlineKeyboardButton("🏬 POS: Shopee/TikTok Shop", callback_data="set_pos_tmdt")
        ])
        
    if form['warehouse_id'] and form['channel_name'] != "❌ CHƯA CHỌN":
        keyboard.append([InlineKeyboardButton("✅ XÁC NHẬN - TẠO ĐƠN NHÁP ODOO", callback_data="submit_order_odoo")])
    keyboard.append([InlineKeyboardButton("❌ HỦY BỎ FORM", callback_data="cancel_lendon_form")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    if query:
        await query.edit_message_text(text_form, reply_markup=reply_markup, parse_mode='Markdown')
    else:
        await update.message.reply_text(text_form, reply_markup=reply_markup, parse_mode='Markdown')

async def lendon_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    form = context.user_data.get('lendon_form')
    
    if not form:
        if data != "back_to_form" and not data.startswith("selectwh_"):
            await query.message.edit_text("❌ Phiên làm việc đã hết hạn. Vui lòng bấm /lendon để tạo lại.")
        return

    if data == "set_wh_201":
        form['warehouse_id'] = '201' 
        form['warehouse_name'] = "201 KHO HÀ NỘI"
        await send_lendon_inline_form(update, context, query)
    elif data == "set_wh_124":
        form['warehouse_id'] = '124'
        form['warehouse_name'] = "124 KHO HỒ CHÍ MINH"
        await send_lendon_inline_form(update, context, query)
        
    elif data == "search_wh_open":
        context.user_data['lendon_msg_id'] = query.message.message_id
        await query.message.reply_text("🔍 Sếp gõ một phần tên kho hoặc mã kho cần tìm nhé (VD: gia lam, thanh hoa...):")
        context.user_data['waiting_custom_wh'] = True
        
    elif data == "set_chan_dienmay":
        form['channel_name'] = "ĐIỆN MÁY"
        form['pos_name'] = "❌ CHƯA CHỌN"
        await send_lendon_inline_form(update, context, query)
    elif data == "set_chan_online":
        form['channel_name'] = "ONLINE"
        form['pos_name'] = "❌ CHƯA CHỌN"
        await send_lendon_inline_form(update, context, query)
        
    elif data == "set_pos_eco":
        form['pos_name'] = "Điện Máy - ECO"
        await send_lendon_inline_form(update, context, query)
    elif data == "set_pos_hc":
        form['pos_name'] = "Điện Máy - HC"
        await send_lendon_inline_form(update, context, query)
    elif data == "set_pos_tmdt":
        form['pos_name'] = "Online - TMĐT"
        await send_lendon_inline_form(update, context, query)
        
    elif data == "cancel_lendon_form":
        context.user_data.pop('lendon_form', None)
        await query.message.edit_text("❌ Đã hủy bỏ biểu mẫu lên đơn hàng!")
        
    elif data == "submit_order_odoo":
        await query.message.edit_text("⌛️ Đang đẩy đơn hàng nháp trực tiếp lên hệ thống Odoo...")
        success, msg = execute_create_order_odoo(form)
        await query.message.reply_text(msg, parse_mode='Markdown')

async def lendon_warehouse_search_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyword = update.message.text.strip()
    context.user_data['waiting_custom_wh'] = False
    
    uid, models, err = connect_odoo()
    if not uid:
        await update.message.reply_text(f"❌ Không kết nối được Odoo để tìm kho: {err}")
        return
        
    try:
        locs = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'stock.location', 'search_read',
                                 [[('usage', '=', 'internal'), ('display_name', 'ilike', keyword)]],
                                 {'fields': ['id', 'display_name'], 'limit': 5})
        if not locs:
            await update.message.reply_text(f"📭 Không tìm thấy kho nào chứa chữ `{keyword}`, sếp bấm lại nút chọn kho nhé.")
            return
            
        keyboard = []
        for l in locs:
            keyboard.append([InlineKeyboardButton(f"🏭 {l['display_name']}", callback_data=f"selectwh_{l['id']}_{l['display_name'][:20]}")])
        keyboard.append([InlineKeyboardButton("❌ Hủy tìm kiếm", callback_data="back_to_form")])
        
        await update.message.reply_text("✅ Các kho phù hợp được tìm thấy, sếp bấm chọn để nạp vào Form nhé:", 
                                        reply_markup=InlineKeyboardMarkup(keyboard))
    except Exception as e:
        await update.message.reply_text(f"❌ Lỗi tìm kho động: {e}")

async def lendon_dynamic_warehouse_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    form = context.user_data.get('lendon_form')
    
    if data == "back_to_form":
        await query.message.delete()
        return

    if data.startswith("selectwh_"):
        parts = data.split("_")
        wh_id = parts[1]
        wh_name = parts[2]
        
        if form:
            form['warehouse_id'] = wh_id
            form['warehouse_name'] = wh_name
        
        await query.message.delete()
        
        bot = context.bot
        msg_id = context.user_data.get('lendon_msg_id')
        if msg_id and form:
            prod_txt = ""
            for idx, p in enumerate(form['products'], 1):
                ck_txt = f" (CK: {p['discount']}% )" if p['discount'] > 0 else ""
                prod_txt += f"   {idx}. {p['code']} | SL: **{p['qty']}**{ck_txt}\n"
            text_form = (
                f"🧾 **FORM ĐIỀU KHIỂN LÊN ĐƠN HÀNG ODOO**\n\n"
                f"👤 **Sales:** {form['odoo_salesperson']['name']}\n"
                f"🏢 **Khách hàng:** {form['customer_name']}\n"
                f"📝 **Tham chiếu:** {form['ref'] if form['ref'] else '⚙️ Tự động'}\n"
                f"🏭 **Kho xuất:** {form['warehouse_name']}\n"
                f"🏷 **Kênh bán:** {form['channel_name']}\n"
                f"🏬 **Mã điểm POS:** {form['pos_name']}\n\n"
                f"📦 **Chi tiết hàng hóa:**\n{prod_txt}\n"
            )
            keyboard = [
                [InlineKeyboardButton("🏭 Kho HN (201)", callback_data="set_wh_201"),
                 InlineKeyboardButton("🏭 Kho HCM (124)", callback_data="set_wh_124"),
                 InlineKeyboardButton("🔍 Tìm kho khác...", callback_data="search_wh_open")],
                [InlineKeyboardButton("🏷 Kênh: ĐIỆN MÁY", callback_data="set_chan_dienmay"),
                 InlineKeyboardButton("🏷 Kênh: ONLINE", callback_data="set_chan_online")]
            ]
            if form['channel_name'] == "ĐIỆN MÁY":
                keyboard.append([
                    InlineKeyboardButton("🏬 POS: Điện Máy - ECO", callback_data="set_pos_eco"),
                    InlineKeyboardButton("🏬 POS: Điện Máy - HC", callback_data="set_pos_hc")
                ])
            elif form['channel_name'] == "ONLINE":
                keyboard.append([
                    InlineKeyboardButton("🏬 POS: Shopee/TikTok Shop", callback_data="set_pos_tmdt")
                ])
            
            if form['warehouse_id'] and form['channel_name'] != "❌ CHƯA CHỌN":
                keyboard.append([InlineKeyboardButton("✅ XÁC NHẬN - TẠO ĐƠN NHÁP ODOO", callback_data="submit_order_odoo")])
            keyboard.append([InlineKeyboardButton("❌ HỦY BỎ FORM", callback_data="cancel_lendon_form")])
            
            await bot.edit_message_text(text_form, chat_id=query.message.chat_id, message_id=msg_id, 
                                        reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

def execute_create_order_odoo(form):
    uid, models, err = connect_odoo()
    if not uid:
        return False, f"❌ Lỗi kết nối Odoo Server: {err}"
        
    try:
        partner_id = form['partner_id']
        if not partner_id:
            partners = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'res.partner', 'search_read', 
                                        [[('name', 'ilike', form['customer_name'])]], {'fields': ['id'], 'limit': 1})
            if not partners:
                return False, f"❌ Thất bại: Không tìm thấy Đối tác/Khách hàng `{form['customer_name']}` trên Odoo."
            partner_id = partners[0]['id']

        order_lines = []
        for p in form['products']:
            products = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'product.product', 'search_read', 
                                        [[('default_code', '=', p['code'])]], {'fields': ['id'], 'limit': 1})
            if not products:
                return False, f"❌ Thất bại: Không tìm thấy Mã sản phẩm `{p['code']}` trên Odoo."
            
            product_id = products[0]['id']
            line_vals = {
                'product_id': product_id,
                'product_uom_qty': float(p['qty']),
            }
            if p['discount'] > 0:
                line_vals['discount'] = float(p['discount'])
                
            order_lines.append((0, 0, line_vals))

        if not order_lines:
            return False, "❌ Đơn hàng trống, không có hàng hóa hợp lệ."

        order_vals = {
            'partner_id': partner_id,
            'user_id': form['odoo_salesperson']['odoo_user_id'], 
            'client_order_ref': form['ref'] if form['ref'] else f"Bot Telegram ({form['odoo_salesperson']['name']})",
            'state': 'draft', 
            'order_line': order_lines
        }
        
        if form['warehouse_id'] and form['warehouse_id'].isdigit():
            order_vals['warehouse_id'] = int(form['warehouse_id'])

        try:
            order_vals['x_channel'] = form['channel_name']
            order_vals['x_pos_branch'] = form['pos_name']
            order_vals['x_brand'] = "NguonSongViet"
            new_order_id = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'sale.order', 'create', [order_vals])
        except Exception:
            order_vals.pop('x_channel', None)
            order_vals.pop('x_pos_branch', None)
            order_vals.pop('x_brand', None)
            new_order_id = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'sale.order', 'create', [order_vals])

        created_order = models.execute_kw(ODOO_DB, uid, ODOO_PASSWORD, 'sale.order', 'read', 
                                          [[new_order_id]], {'fields': ['name', 'amount_total']})
        
        order_name = created_order[0]['name']
        total_money = created_order[0]['amount_total']
        
        success_msg = (
            f"🎉 **ĐÃ TẠO ĐƠN HÀNG NHÁP THÀNH CÔNG!**\n\n"
            f"🔖 **Mã đơn Odoo:** `{order_name}`\n"
            f"🏢 **Khách hàng:** {form['customer_name']}\n"
            f"🏭 **Kho xuất:** {form['warehouse_name']}\n"
            f"💰 **Tổng tiền (Odoo tự áp giá chuỗi):** {total_money:,.0f} VNĐ\n"
            f"👤 **Người lập đơn:** {form['odoo_salesperson']['name']}\n\n"
            f"👉 Đơn đã nằm ở trạng thái *Báo Giá / Nháp*. Sếp có thể duyệt trên Odoo nhé!"
        )
        return True, success_msg

    except Exception as e:
        logger.error(f"Lỗi khởi tạo đơn RPC: {e}")
        return False, f"❌ **Lỗi Hệ thống Odoo:** {str(e)}"

async def cancel_lendon_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Đã hủy biểu mẫu nhập đơn hàng từng bước.")
    return ConversationHandler.END


# ---------------- MAIN INITIALIZATION ----------------
def main():
    if not TELEGRAM_TOKEN or not ODOO_URL_RAW or not ODOO_DB or not ODOO_USERNAME or not ODOO_PASSWORD:
        logger.error("Thiếu cấu hình môi trường (token, url, db, user, pass).")
        return

    load_cloud_db() # Tải dữ liệu JSONBin

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        asyncio.get_event_loop().run_until_complete(bot.delete_webhook())
        logger.info("đã xóa webhook cũ (nếu có).")
    except Exception as e:
        logger.warning(f"Lỗi xóa webhook: {e}")

    # --- ĐĂNG KÝ LUỒNG CONVERSATION CHO LÊN ĐƠN HÀNG ---
    lendon_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("lendon", start_lendon_command)],
        states={
            LENDON_CUSTOMER: [MessageHandler(filters.TEXT & ~filters.COMMAND, lendon_customer_handler)],
            LENDON_REF: [MessageHandler(filters.TEXT & ~filters.COMMAND, lendon_ref_handler)],
            LENDON_PRODUCTS: [MessageHandler(filters.TEXT & ~filters.COMMAND, lendon_products_handler)]
        },
        fallbacks=[CommandHandler("cancel", cancel_lendon_conversation)]
    )
    application.add_handler(lendon_conv_handler)

    # --- ĐĂNG KÝ BỘ ĐIỀU HƯỚNG NÚT BẤM CALLBACK FORM ---
    application.add_handler(CallbackQueryHandler(lendon_dynamic_warehouse_callback, pattern=r"^(selectwh_|back_to_form)"))
    application.add_handler(CallbackQueryHandler(lendon_callback_handler))

    # --- ĐĂNG KÝ CÁC LỆNH COMMAND CŨ (Giữ nguyên vẹn) ---
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", start_command))
    application.add_handler(CommandHandler("ping", ping_command))
    application.add_handler(CommandHandler("keohang", excel_report_command))
    application.add_handler(CommandHandler("checkpo", checkpo_command))
    application.add_handler(CommandHandler("baocaongay", daily_report_command))
    application.add_handler(CommandHandler("dotonkho", dotonkho_command))  
    application.add_handler(CommandHandler("baodanh", baodanh_command))  
    
    application.add_handler(MessageHandler(filters.Document.ALL, handle_po_file))
    
    # Bộ bắt văn bản thô rà soát kho mở rộng hoặc bóc tách cổng cũ
    async def global_text_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if context.user_data.get('waiting_custom_wh'):
            await lendon_warehouse_search_text(update, context)
        else:
            await handle_product_code(update, context)
            
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, global_text_filter))

    logger.info("Bot started!")
    application.run_polling()


if __name__ == "__main__":
    main()
