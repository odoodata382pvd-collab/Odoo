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
# Normalise ODOO URL (remove trailing / and optional /odoo)
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

# ---------------- Keep port open (Render free) ----------------
# Mở port giả để Render (Web Service free) không báo timeout.
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
            error_message = f"Đăng nhập thất bại (uid=0). kiểm tra lại user/pass/db."
            return None, None, error_message
        models = xmlrpc.client.ServerProxy(f'{ODOO_URL_FINAL}/xmlrpc/2/object', context=context)
        return uid, models, "kết nối thành công."
    except xmlrpc.client.ProtocolError as pe:
        error_message = f"lỗi giao thức odoo: {pe}"
        return None, None, error_message
    except Exception as e:
        error_message = f"lỗi kết nối odoo xml-rpc: {e}"
        return None, None, error_message

# ---------------- Helpers ----------------
def find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD):
    location_ids = {}
    def search_location(name_code):
        loc_data = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'stock.location', 'search_read',
            [[('display_name', 'ilike', name_code)]],
            {'fields': ['id', 'display_name', 'complete_name']}
        )
        if not loc_data:
            return None
        preferred_loc = next((l for l in loc_data if name_code.lower() in l['display_name'].lower()), loc_data[0])
        if preferred_loc and 'id' in preferred_loc and 'display_name' in preferred_loc:
            return {'id': preferred_loc['id'], 'name': preferred_loc.get('display_name') or preferred_loc.get('complete_name')}
        return None

    hn_stock = search_location(LOCATION_MAP['HN_STOCK_CODE'])
    if hn_stock: location_ids['HN_STOCK'] = hn_stock
    hcm_stock = search_location(LOCATION_MAP['HCM_STOCK_CODE'])
    if hcm_stock: location_ids['HCM_STOCK'] = hcm_stock
    hn_transit = search_location(LOCATION_MAP['HN_TRANSIT_NAME'])
    if hn_transit: location_ids['HN_TRANSIT'] = hn_transit
    return location_ids

def escape_markdown(text):
    special_chars = ['\\', '_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    text = str(text)
    for char in special_chars:
        text = text.replace(char, f'\\{char}')
    return text.replace('\\`', '`')

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

        all_locations_ids = [v['id'] for v in location_ids.values()]
        quant_domain = [('location_id', 'in', all_locations_ids), ('quantity', '>', 0)]
        quant_data = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'stock.quant', 'search_read',
            [quant_domain],
            {'fields': ['product_id', 'location_id', 'quantity']}
        )

        product_ids = list(set([q['product_id'][0] for q in quant_data]))
        product_info = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'product.product', 'search_read',
            [[('id', 'in', product_ids)]],
            {'fields': ['display_name', PRODUCT_CODE_FIELD]}
        )
        product_map = {p['id']: p for p in product_info}

        data = {}
        for q in quant_data:
            prod_id = q['product_id'][0]
            loc_id = q['location_id'][0]
            qty = float(q['quantity'])
            if prod_id not in data and prod_id in product_map:
                data[prod_id] = {
                    'Mã SP': product_map[prod_id].get(PRODUCT_CODE_FIELD, 'N/A'),
                    'Tên SP': product_map[prod_id]['display_name'],
                    'Tồn Kho HN': 0.0, 'Tồn Kho HCM': 0.0, 'Kho Nhập HN': 0.0, 'Tổng Tồn HN': 0.0, 'Số Lượng Đề Xuất': 0.0
                }
            if loc_id == location_ids.get('HN_STOCK', {}).get('id'):
                data[prod_id]['Tồn Kho HN'] += qty
            elif loc_id == location_ids.get('HCM_STOCK', {}).get('id'):
                data[prod_id]['Tồn Kho HCM'] += qty
            elif loc_id == location_ids.get('HN_TRANSIT', {}).get('id'):
                data[prod_id]['Kho Nhập HN'] += qty

        report_data = []
        for prod_id, info in data.items():
            info['Tổng Tồn HN'] = info['Tồn Kho HN'] + info['Kho Nhập HN']
            if info['Tổng Tồn HN'] < TARGET_MIN_QTY:
                qty_needed = TARGET_MIN_QTY - info['Tổng Tồn HN']
                info['Số Lượng Đề Xuất'] = min(qty_needed, info['Tồn Kho HCM'])
                if info['Số Lượng Đề Xuất'] > 0:
                    report_data.append(info)

        df = pd.DataFrame(report_data)
        COLUMNS_ORDER = ['Mã SP', 'Tên SP', 'Tồn Kho HN', 'Tồn Kho HCM', 'Kho Nhập HN', 'Số Lượng Đề Xuất']
        if not df.empty:
            df = df[COLUMNS_ORDER]
            for col in ['Tồn Kho HN', 'Tồn Kho HCM', 'Kho Nhập HN', 'Số Lượng Đề Xuất']:
                df[col] = df[col].apply(lambda x: int(round(x)))
        else:
            df = pd.DataFrame(columns=COLUMNS_ORDER)

        excel_buffer = io.BytesIO()
        df.to_excel(excel_buffer, index=False, sheet_name='DeXuatKeoHang')
        excel_buffer.seek(0)
        return excel_buffer, len(report_data), "thành công"
    except Exception as e:
        error_msg = f"lỗi khi truy vấn dữ liệu odoo xml-rpc: {e}"
        return None, 0, error_msg

# ---------------- PO /checkpo helpers ----------------
def _read_po_with_auto_header(file_bytes: bytes):
    """
    Đọc file PO, tự động dò dòng header (do file VHC có thể có title phía trên).
    Trả về df_raw (DataFrame đã có header đúng) hoặc (None, error_msg) nếu lỗi.
    """
    try:
        df_tmp = pd.read_excel(io.BytesIO(file_bytes), header=None)
    except Exception as e:
        return None, f"Không đọc được file Excel PO: {e}"

    header_row_idx = None
    for idx in range(len(df_tmp)):
        row_values = df_tmp.iloc[idx].astype(str).str.lower()
        row_text = " ".join(row_values)
        if any(key in row_text for key in ["mã sp", "ma sp", "mã hàng", "ma hang", "mã sản phẩm", "ma san pham", "mã hh", "ma hh"]):
            header_row_idx = idx
            break

    if header_row_idx is None:
        # nếu không tìm thấy, dùng luôn dòng đầu tiên làm header (giống cách read_excel mặc định)
        header_row_idx = 0

    try:
        df_raw = pd.read_excel(io.BytesIO(file_bytes), header=header_row_idx)
        return df_raw, None
    except Exception as e:
        return None, f"Không đọc được file Excel PO với header tại dòng {header_row_idx + 1}: {e}"

def _detect_po_columns(df: pd.DataFrame):
    """
    Tự động dò các cột: Mã SP, SL cần giao, ĐV nhận
    Dựa theo header trong file PO đối tác gửi.
    """
    cols_lower = {col: str(col).strip().lower() for col in df.columns}

    def find_col(candidates):
        for col, lower in cols_lower.items():
            for key in candidates:
                if key in lower:
                    return col
        return None

    code_col = find_col(['mã sp', 'ma sp', 'mã hàng', 'ma hang', 'mã sản phẩm', 'ma san pham', 'mã hh', 'ma hh'])
    qty_col = find_col(['sl', 'số lượng', 'so luong', 's.l', 'sl đặt', 'sl dat'])
    recv_col = find_col(['đv nhận', 'dv nhận', 'đơn vị nhận', 'don vi nhan', 'đv nhận hàng', 'dv nhận hang',
                         'cửa hàng nhận', 'cua hang nhan'])

    return code_col, qty_col, recv_col

def _get_stock_for_product_with_cache(models, uid, product_id, location_ids, cache):
    """
    Lấy tồn kho theo đúng logic cũ:
    - Dùng product.product 'read' với context {'location': LOCATION_ID}
    Trả về dict {'hn': int, 'transit': int, 'hcm': int}
    Có cache để không gọi lại nhiều lần.
    """
    if product_id in cache:
        return cache[product_id]

    hn_id = location_ids.get('HN_STOCK', {}).get('id')
    transit_id = location_ids.get('HN_TRANSIT', {}).get('id')
    hcm_id = location_ids.get('HCM_STOCK', {}).get('id')

    def _get_qty(location_id):
        if not location_id:
            return 0
        stock_product_info = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'product.product', 'read',
            [[product_id]],
            {'fields': ['qty_available'], 'context': {'location': location_id}}
        )
        if stock_product_info and stock_product_info[0]:
            return int(round(stock_product_info[0].get('qty_available', 0.0)))
        return 0

    res = {
        'hn': _get_qty(hn_id),
        'transit': _get_qty(transit_id),
        'hcm': _get_qty(hcm_id),
    }
    cache[product_id] = res
    return res

def process_po_and_build_report(file_bytes: bytes):
    """
    Đọc file PO Excel, đối chiếu tồn kho và sinh file Excel kết quả.

    Cột kết quả theo format chị đã OKE1:
    ['Mã SP','Tên SP','ĐV nhận','SL cần giao',
     'Tồn HN','Tồn Kho Nhập','Tổng tồn HN','Tồn HCM',
     'Trạng thái','SL cần kéo từ HCM','SL thiếu']
    """
    df_raw, err = _read_po_with_auto_header(file_bytes)
    if df_raw is None:
        return None, err

    if df_raw.empty:
        return None, "File PO không có dữ liệu."

    code_col, qty_col, recv_col = _detect_po_columns(df_raw)
    if not code_col or not qty_col or not recv_col:
        return None, (
            "Không xác định được đủ 3 cột [Mã SP, Số lượng, ĐV nhận].\n"
            f"Các cột hiện có: {list(df_raw.columns)}"
        )

    df = df_raw[[code_col, qty_col, recv_col]].copy()
    df.columns = ['Mã SP', 'SL cần giao', 'ĐV nhận']

    df['Mã SP'] = df['Mã SP'].astype(str).str.strip().str.upper()
    df['SL cần giao'] = pd.to_numeric(df['SL cần giao'], errors='coerce').fillna(0)
    df = df[df['Mã SP'] != ""]
    df = df[df['SL cần giao'] > 0]

    if df.empty:
        return None, "Không tìm thấy dòng nào có Mã SP và SL cần giao > 0."

    uid, models, error_msg = connect_odoo()
    if not uid:
        return None, f"Lỗi kết nối Odoo: {error_msg}"

    try:
        # map mã sp -> product
        unique_codes = sorted(df['Mã SP'].unique().tolist())
        products = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'product.product', 'search_read',
            [[(PRODUCT_CODE_FIELD, 'in', unique_codes)]],
            {'fields': ['id', 'display_name', PRODUCT_CODE_FIELD]}
        )
        code_to_product = {}
        for p in products:
            code_val = str(p.get(PRODUCT_CODE_FIELD) or "").strip().upper()
            if not code_val:
                continue
            code_to_product[code_val] = p

        # lấy location_id bằng đúng helper cũ
        location_ids = find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD)
        if len(location_ids) < 2:
            return None, f"không tìm thấy đủ kho để đối chiếu: {list(location_ids.keys())}"

        stock_cache = {}
        rows = []

        for _, row in df.iterrows():
            code = str(row['Mã SP']).strip().upper()
            qty_need = float(row['SL cần giao'])
            receiver = str(row['ĐV nhận'])

            prod = code_to_product.get(code)
            if not prod:
                rows.append({
                    'Mã SP': code,
                    'Tên SP': 'KHÔNG TÌM THẤY TRÊN ODOO',
                    'ĐV nhận': receiver,
                    'SL cần giao': int(round(qty_need)),
                    'Tồn HN': 0,
                    'Tồn Kho Nhập': 0,
                    'Tổng tồn HN': 0,
                    'Tồn HCM': 0,
                    'Trạng thái': 'KHÔNG TÌM THẤY MÃ SẢN PHẨM',
                    'SL cần kéo từ HCM': 0,
                    'SL thiếu': int(round(qty_need)),
                })
                continue

            prod_id = prod['id']
            prod_name = prod.get('display_name', '')

            stock = _get_stock_for_product_with_cache(models, uid, prod_id, location_ids, stock_cache)
            hn_stock = float(stock.get('hn', 0))
            hn_transit = float(stock.get('transit', 0))
            hcm_stock = float(stock.get('hcm', 0))

            total_hn = hn_stock + hn_transit

            qty_need_int = int(round(qty_need))
            hn_stock_int = int(round(hn_stock))
            hn_transit_int = int(round(hn_transit))
            hcm_stock_int = int(round(hcm_stock))
            total_hn_int = int(round(total_hn))

            status = ""
            pull_from_hcm = 0
            shortage = 0

            # 1. Nếu SL cần giao <= tồn kho 201/201 -> đủ tại HN
            if qty_need <= hn_stock:
                status = "ĐỦ tại kho HN (201/201)"
            # 2. Nếu thiếu HN nhưng tổng HN (HN + Kho nhập) vẫn đủ
            elif qty_need <= total_hn:
                status = "ĐỦ (HN + Kho nhập HN)"
            # 3. Tổng HN không đủ -> cần kéo từ HCM
            else:
                need_from_hcm = qty_need - total_hn
                if need_from_hcm <= hcm_stock:
                    pull_from_hcm = int(round(need_from_hcm))
                    status = "CẦN KÉO HÀNG TỪ HCM"
                else:
                    pull_from_hcm = hcm_stock_int
                    shortage = int(round(need_from_hcm - hcm_stock))
                    status = "THIẾU DÙ ĐÃ KÉO TỐI ĐA TỪ HCM"

            rows.append({
                'Mã SP': code,
                'Tên SP': prod_name,
                'ĐV nhận': receiver,
                'SL cần giao': qty_need_int,
                'Tồn HN': hn_stock_int,
                'Tồn Kho Nhập': hn_transit_int,
                'Tổng tồn HN': total_hn_int,
                'Tồn HCM': hcm_stock_int,
                'Trạng thái': status,
                'SL cần kéo từ HCM': pull_from_hcm,
                'SL thiếu': shortage,
            })

        result_df = pd.DataFrame(rows)
        COLUMNS_ORDER = [
            'Mã SP',
            'Tên SP',
            'ĐV nhận',
            'SL cần giao',
            'Tồn HN',
            'Tồn Kho Nhập',
            'Tổng tồn HN',
            'Tồn HCM',
            'Trạng thái',
            'SL cần kéo từ HCM',
            'SL thiếu',
        ]
        for col in COLUMNS_ORDER:
            if col not in result_df.columns:
                result_df[col] = ""
        result_df = result_df[COLUMNS_ORDER]

        excel_buffer = io.BytesIO()
        result_df.to_excel(excel_buffer, index=False, sheet_name='KiemTraPO')
        excel_buffer.seek(0)
        return excel_buffer, None

    except Exception as e:
        logger.error(f"Lỗi khi xử lý PO /checkpo: {e}")
        return None, f"Lỗi khi xử lý PO: {e}"

# ---------------- Handle product code ----------------
async def handle_product_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    product_code = update.message.text.strip().upper()
    await update.message.reply_text(f"đang tra tồn cho `{product_code}`, vui lòng chờ!", parse_mode='Markdown')

    uid, models, error_msg = connect_odoo()
    if not uid:
        await update.message.reply_text(f"❌ lỗi kết nối odoo. chi tiết: `{escape_markdown(error_msg)}`", parse_mode='Markdown')
        return

    try:
        location_ids = find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD)
        hn_transit_id = location_ids.get('HN_TRANSIT', {}).get('id')
        hn_stock_id = location_ids.get('HN_STOCK', {}).get('id')
        hcm_stock_id = location_ids.get('HCM_STOCK', {}).get('id')

        product_domain = [(PRODUCT_CODE_FIELD, '=', product_code)]
        products = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'product.product', 'search_read',
            [product_domain],
            {'fields': ['display_name', 'id']}
        )
        if not products:
            await update.message.reply_text(f"❌ Không tìm thấy sản phẩm nào có mã `{product_code}`")
            return
        product = products[0]
        product_id = product['id']
        product_name = product['display_name']

        def get_qty_available(location_id):
            if not location_id: return 0
            stock_product_info = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, 'product.product', 'read',
                [[product_id]],
                {'fields': ['qty_available'], 'context': {'location': location_id}}
            )
            return int(round(stock_product_info[0].get('qty_available', 0.0))) if stock_product_info and stock_product_info[0] else 0

        hn_stock_qty = get_qty_available(hn_stock_id)
        hn_transit_qty = get_qty_available(hn_transit_id)
        hcm_stock_qty = get_qty_available(hcm_stock_id)

        quant_domain_all = [('product_id', '=', product_id), ('available_quantity', '>', 0)]
        quant_data_all = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'stock.quant', 'search_read',
            [quant_domain_all],
            {'fields': ['location_id', 'available_quantity']}
        )

        location_ids_all = list({q['location_id'][0] for q in quant_data_all if q.get('location_id')})
        if location_ids_all:
            location_info = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, 'stock.location', 'read',
                [location_ids_all],
                {'fields': ['id', 'display_name', 'complete_name', 'usage']}
            )
        else:
            location_info = []
        location_map = {loc['id']: loc for loc in location_info}

        stock_by_loc_id = {}
        for q in quant_data_all:
            loc_field = q.get('location_id')
            if not loc_field:
                continue
            loc_id = loc_field[0]
            qty = float(q.get('available_quantity', 0.0))
            if qty <= 0:
                continue
            stock_by_loc_id[loc_id] = stock_by_loc_id.get(loc_id, 0.0) + qty

        all_stock_details = {}
        for loc_id, qty in stock_by_loc_id.items():
            display_name = location_map.get(loc_id, {}).get('complete_name') or location_map.get(loc_id, {}).get('display_name') or f"ID:{loc_id}"
            qty_int = int(qty)
            if qty_int > 0:
                all_stock_details[display_name] = qty_int

        total_hn_stock = hn_stock_qty + hn_transit_qty
        recommendation_qty = 0
        if total_hn_stock < TARGET_MIN_QTY:
            qty_needed = TARGET_MIN_QTY - total_hn_stock
            recommendation_qty = min(qty_needed, hcm_stock_qty)
        recommendation_text = f"=> đề xuất nhập thêm `{int(recommendation_qty)}` sp để hn đủ tồn `{TARGET_MIN_QTY}` sản phẩm." if recommendation_qty > 0 else f"=> tồn kho hn đã đủ (`{int(total_hn_stock)}`/{TARGET_MIN_QTY} sp)."

        header_line = f"{product_code} {product_name}"
        summary_lines = [
            f"Tồn kho HN: {int(hn_stock_qty)}",
            f"Tồn kho HCM: {int(hcm_stock_qty)}",
            f"Tồn kho nhập Hà Nội: {int(hn_transit_qty)}",
            recommendation_text.replace('`', '')
        ]

        priority_items = []
        other_items = []
        used_names = set()
        for code in PRIORITY_LOCATIONS:
            for name, qty in all_stock_details.items():
                if code.lower() in name.lower() and name not in used_names:
                    priority_items.append((name, qty))
                    used_names.add(name)
                    break
        for name, qty in sorted(all_stock_details.items()):
            if name not in used_names:
                other_items.append((name, qty))
                used_names.add(name)

        detail_lines = []
        for name, qty in priority_items + other_items:
            detail_lines.append(f"{name}: {qty}")

        detail_content = "\n".join(detail_lines) if detail_lines else "Không có tồn kho chi tiết lớn hơn 0."

        message = f"""{header_line}
{summary_lines[0]}
{summary_lines[1]}
{summary_lines[2]}
{summary_lines[3]}

2/ Tồn kho chi tiết(Có hàng):
{detail_content}
"""
        await update.message.reply_text(message.strip())

    except Exception as e:
        logger.error(f"Lỗi khi tra cứu sản phẩm xml-rpc: {e}")
        await update.message.reply_text(f"❌ Có lỗi xảy ra khi truy vấn odoo: {str(e)}")

# ---------------- Telegram Handlers ----------------
async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Đang kiểm tra kết nối odoo, xin chờ...")
    uid, _, error_msg = connect_odoo()
    if uid:
        await update.message.reply_text(f"✅ Thành công! kết nối odoo db: {ODOO_DB} tại {ODOO_URL_RAW}. user id: {uid}")
    else:
        await update.message.reply_text(f"❌ Lỗi! chi tiết: {error_msg}")

async def excel_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⌛️ Iem đang xử lý dữ liệu và tạo báo cáo Excel. Chờ em xíu xìu xiu nhá...")
    excel_buffer, item_count, error_msg = get_stock_data()
    if excel_buffer is None:
        await update.message.reply_text(f"❌ Lỗi kết nối odoo hoặc lỗi nghiệp vụ. chi tiết: {error_msg}")
        return
    if item_count > 0:
        await update.message.reply_document(document=excel_buffer, filename='de_xuat_keo_hang.xlsx', caption=f"✅ iem đây! đã tìm thấy {item_count} sản phẩm cần kéo hàng.")
    else:
        await update.message.reply_text(f"✅ Tất cả sản phẩm đã đạt mức tồn kho tối thiểu {TARGET_MIN_QTY} tại kho HN.")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.message.from_user.first_name
    welcome_message = (
        f"Chào mừng {user_name} đến với cuộc đời iem!\n\n"
        "1. Gõ mã sp (vd: I-78) để tra tồn.\n"
        "2. Dùng lệnh /keohang để tạo báo cáo excel.\n"
        "3. Dùng lệnh /ping để kiểm tra kết nối."
        "4. Không có nhu cầu thì đừng phiền iem!"
    )
    await update.message.reply_text(welcome_message)

# ---------------- Telegram Handlers cho /checkpo ----------------
async def checkpo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Lệnh /checkpo:
    - Bật trạng thái chờ file PO cho user hiện tại
    - Hướng dẫn gửi file Excel
    """
    context.user_data['waiting_for_po'] = True
    await update.message.reply_text(
        "Ok, gửi cho iem file PO Excel (.xlsx) theo mẫu đối tác gửi hàng tuần nha.\n"
        "Iem sẽ tự động đối chiếu tồn kho HN / Kho nhập HN / HCM và trả lại file kết quả."
    )

async def handle_po_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Nhận file Excel từ user, chỉ xử lý nếu trước đó user đã gọi /checkpo.
    Không ảnh hưởng tới các tính năng khác.
    """
    if not context.user_data.get('waiting_for_po'):
        # Không ở chế độ /checkpo -> bỏ qua
        return

    context.user_data['waiting_for_po'] = False

    document = update.message.document
    if not document:
        await update.message.reply_text("File không hợp lệ, vui lòng gửi lại file Excel (.xlsx) giúp iem.")
        return

    file_name = (document.file_name or "").lower()
    if not file_name.endswith(".xlsx"):
        await update.message.reply_text("Hiện tại iem chỉ hỗ trợ file Excel định dạng .xlsx thôi nha.")
        return

    await update.message.reply_text("⌛️ Iem đang đọc PO và đối chiếu tồn kho, chờ em xíu xìu xiu nha...")

    try:
        file = await document.get_file()
        file_bytes = await file.download_as_bytearray()
    except Exception as e:
        logger.error(f"Lỗi khi tải file PO từ Telegram: {e}")
        await update.message.reply_text(f"❌ Lỗi khi tải file PO từ Telegram: {e}")
        return

    excel_buffer, error_msg = process_po_and_build_report(bytes(file_bytes))
    if excel_buffer is None:
        await update.message.reply_text(f"❌ Có lỗi khi xử lý PO: {error_msg}")
        return

    await update.message.reply_document(
        document=excel_buffer,
        filename='kiem_tra_po.xlsx',
        caption="✅ Iem gửi chị file kiểm tra PO và đề xuất kéo hàng rồi nè."
    )

# ---------------- Main ----------------
def main():
    if not TELEGRAM_TOKEN or not ODOO_URL_RAW or not ODOO_DB or not ODOO_USERNAME or not ODOO_PASSWORD:
        logger.error("vui lòng thiết lập tất cả các biến môi trường cần thiết (token, url, db, user, pass).")
        return

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # xóa webhook (gọi đồng bộ để tránh warning)
    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        try:
            asyncio.get_event_loop().run_until_complete(bot.delete_webhook())
            logger.info("đã xóa webhook cũ (nếu có).")
        except Exception as e:
            logger.warning(f"lỗi khi xóa webhook (không ảnh hưởng): {e}")
    except Exception as e:
        logger.warning(f"lỗi khi tạo Bot object: {e}")

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", start_command))
    application.add_handler(CommandHandler("ping", ping_command))
    application.add_handler(CommandHandler("keohang", excel_report_command))
    # handler mới cho /checkpo + nhận file, KHÔNG ảnh hưởng handler cũ
    application.add_handler(CommandHandler("checkpo", checkpo_command))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_po_file))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_product_code))

    logger.info("bot đang chạy...")
    application.run_polling()

# ---------------- HTTP server để ping bot (giữ bot tỉnh) ----------------
from http.server import BaseHTTPRequestHandler, HTTPServer

class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

    def log_message(self, format, *args):
        return

def start_http_server():
    try:
        server = HTTPServer(("0.0.0.0", 10001), PingHandler)
        logger.info("HTTP ping server đang chạy trên port 10001")
        server.serve_forever()
    except Exception as e:
        logger.error(f"Lỗi khi chạy HTTP ping server: {e}")

threading.Thread(target=start_http_server, daemon=True).start()

# ---------------- Run Main ----------------
if __name__ == "__main__":
    main()
