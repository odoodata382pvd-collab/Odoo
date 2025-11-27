# main.py - Phiên bản phục hồi + tích hợp thêm tính năng /checkpo
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
from http.server import BaseHTTPRequestHandler, HTTPServer

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
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
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
    except Exception as e:
        logger.error(f"Lỗi khi giữ port mở: {e}")

threading.Thread(target=keep_port_open, daemon=True).start()

# ---------------- Odoo Connect ----------------
def connect_odoo():
    try:
        if not ODOO_URL_FINAL:
            return None, None, "Thiếu ODOO_URL"

        parsed = urlparse(ODOO_URL_FINAL)
        scheme = parsed.scheme or "https"
        netloc = parsed.netloc or parsed.path
        base_url = f"{scheme}://{netloc}"

        common_url = f"{base_url}/xmlrpc/2/common"
        object_url = f"{base_url}/xmlrpc/2/object"

        context = ssl._create_unverified_context()
        common = xmlrpc.client.ServerProxy(common_url, context=context)
        uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD, {})

        if not uid:
            return None, None, "không authenticate được user/password trên Odoo"

        models = xmlrpc.client.ServerProxy(object_url, context=context)
        return uid, models, None
    except Exception as e:
        return None, None, f"lỗi khi kết nối odoo xml-rpc: {e}"

# ---------------- Find Required Locations ----------------
def find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWD):
    """
    Trả về dict:
    {
        'HN_STOCK': {'id': ..., 'name': ..., 'complete_name': ...},
        'HCM_STOCK': {...},
        'HN_TRANSIT': {...},
    }
    """
    try:
        all_locations = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWD,
            'stock.location', 'search_read',
            [[('usage', '=', 'internal')]],
            {'fields': ['id', 'display_name', 'name', 'complete_name']}
        )
    except Exception as e:
        logger.error(f"lỗi khi đọc stock.location: {e}")
        return None

    def search_location(code_fragment=None, name_fragment=None):
        for loc in all_locations:
            cname = loc.get('complete_name') or ''
            name = loc.get('name') or ''
            if code_fragment and code_fragment in cname:
                return loc
            if name_fragment and name_fragment == name:
                return loc
        return None

    hn_stock = search_location(code_fragment=LOCATION_MAP['HN_STOCK_CODE'])
    hcm_stock = search_location(code_fragment=LOCATION_MAP['HCM_STOCK_CODE'])
    hn_transit = search_location(name_fragment=LOCATION_MAP['HN_TRANSIT_NAME'])

    result = {}
    if hn_stock:
        result['HN_STOCK'] = hn_stock
    if hcm_stock:
        result['HCM_STOCK'] = hcm_stock
    if hn_transit:
        result['HN_TRANSIT'] = hn_transit

    return result

# ---------------- Stock Detail For Product ----------------
def get_stock_detail_for_product(models, uid, product_id):
    """
    Trả về:
    - summary: dict tồn tại HN (201/201), HCM (124/124), Kho nhập HN
    - detail_rows: list chứa các dòng chi tiết (kho có hàng)
    """
    location_ids = find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD)
    if not location_ids or len(location_ids) < 1:
        raise ValueError("không tìm thấy location nội bộ nào.")

    hn_stock_id = location_ids.get('HN_STOCK', {}).get('id')
    hcm_stock_id = location_ids.get('HCM_STOCK', {}).get('id')
    hn_transit_id = location_ids.get('HN_TRANSIT', {}).get('id')

    summary = {
        'hn': 0.0,
        'hcm': 0.0,
        'hn_transit': 0.0,
    }

    quant_domain = [
        ('product_id', '=', product_id),
        ('quantity', '>', 0),
    ]
    quant_data = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        'stock.quant', 'search_read',
        [quant_domain],
        {'fields': ['location_id', 'quantity']}
    )

    detail_rows = []
    for q in quant_data:
        loc_id, loc_name = q['location_id']
        qty = float(q.get('quantity', 0.0))

        if hn_stock_id and loc_id == hn_stock_id:
            summary['hn'] += qty
        elif hcm_stock_id and loc_id == hcm_stock_id:
            summary['hcm'] += qty
        elif hn_transit_id and loc_id == hn_transit_id:
            summary['hn_transit'] += qty

        detail_rows.append({
            'location_id': loc_id,
            'location_name': loc_name,
            'available_quantity': qty
        })

    return summary, detail_rows

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
                    'Tồn Kho HN': 0.0,
                    'Tồn Kho HCM': 0.0,
                    'Kho Nhập HN': 0.0,
                    'Tổng Tồn HN': 0.0,
                    'Số Lượng Đề Xuất': 0.0
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

    code_col = find_col(['mã sp', 'mã hàng', 'mã sản phẩm', 'mã hh'])
    qty_col = find_col(['sl ', 'sl_', ' sl', 'số lượng', 'so luong', 's.l', 's.lượng'])
    recv_col = find_col(['đv nhận', 'dv nhận', 'đơn vị nhận', 'don vi nhan',
                         'đv nhận hàng', 'cửa hàng nhận', 'cua hang nhan'])

    return code_col, qty_col, recv_col


def _get_stock_for_products(models, uid, product_ids):
    """
    Lấy tồn kho tại 3 kho:
    - HN_STOCK (201/201)
    - HN_TRANSIT (Kho nhập Hà Nội)
    - HCM_STOCK (124/124)

    Trả về:
    - stock_map: {product_id: {'hn': x, 'hn_transit': y, 'hcm': z}}
    - location_ids: map thông tin kho (tái sử dụng nếu cần)
    """
    stock_map = {pid: {'hn': 0.0, 'hn_transit': 0.0, 'hcm': 0.0} for pid in product_ids}

    location_ids = find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD)
    if not location_ids or len(location_ids) < 3:
        raise ValueError(f"không tìm thấy đủ 3 kho cần thiết: {list(location_ids.keys()) if location_ids else 'NONE'}")

    hn_stock_id = location_ids.get('HN_STOCK', {}).get('id')
    hcm_stock_id = location_ids.get('HCM_STOCK', {}).get('id')
    hn_transit_id = location_ids.get('HN_TRANSIT', {}).get('id')

    all_loc_ids = [loc_id for loc_id in [hn_stock_id, hcm_stock_id, hn_transit_id] if loc_id]

    if not all_loc_ids or not product_ids:
        return stock_map, location_ids

    quant_domain = [
        ('product_id', 'in', list(product_ids)),
        ('location_id', 'in', all_loc_ids),
        ('quantity', '>', 0),
    ]
    quant_data = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        'stock.quant', 'search_read',
        [quant_domain],
        {'fields': ['product_id', 'location_id', 'quantity']}
    )

    for q in quant_data:
        prod_id = q['product_id'][0]
        loc_id = q['location_id'][0]
        qty = float(q.get('quantity', 0.0))
        if prod_id not in stock_map:
            continue
        if loc_id == hn_stock_id:
            stock_map[prod_id]['hn'] += qty
        elif loc_id == hn_transit_id:
            stock_map[prod_id]['hn_transit'] += qty
        elif loc_id == hcm_stock_id:
            stock_map[prod_id]['hcm'] += qty

    return stock_map, location_ids


def process_po_and_build_report(file_bytes: bytes):
    """
    Đọc file PO Excel, đối chiếu tồn kho và sinh file Excel kết quả.

    Trả về:
    - excel_buffer (io.BytesIO) nếu OK
    - error_msg (str) nếu lỗi (excel_buffer = None)
    """
    try:
        df_raw = pd.read_excel(io.BytesIO(file_bytes))
    except Exception as e:
        return None, f"Không đọc được file Excel PO: {e}"

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
        unique_codes = sorted(df['Mã SP'].unique().tolist())

        products = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'product.product', 'search_read',
            [[(PRODUCT_CODE_FIELD, 'in', unique_codes)]],
            {'fields': ['id', 'display_name', PRODUCT_CODE_FIELD]}
        )

        code_to_product = {}
        product_ids = []
        for p in products:
            code_val = str(p.get(PRODUCT_CODE_FIELD) or "").strip().upper()
            if not code_val:
                continue
            code_to_product[code_val] = p
            product_ids.append(p['id'])

        stock_map, location_ids = _get_stock_for_products(models, uid, product_ids)

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
                    'Tồn Kho HN (201/201)': 0,
                    'Tồn Kho Nhập HN': 0,
                    'Tổng tồn HN': 0,
                    'Tồn Kho HCM (124/124)': 0,
                    'Trạng thái': 'KHÔNG TÌM THẤY MÃ SẢN PHẨM',
                    'SL cần kéo từ HCM': 0,
                    'SL còn thiếu': int(round(qty_need))
                })
                continue

            prod_id = prod['id']
            prod_name = prod.get('display_name', '')

            stock = stock_map.get(prod_id, {'hn': 0.0, 'hn_transit': 0.0, 'hcm': 0.0})
            hn_stock = float(stock.get('hn', 0.0))
            hn_transit = float(stock.get('hn_transit', 0.0))
            hcm_stock = float(stock.get('hcm', 0.0))

            total_hn = hn_stock + hn_transit
            qty_need_int = int(round(qty_need))
            hn_stock_int = int(round(hn_stock))
            hn_transit_int = int(round(hn_transit))
            hcm_stock_int = int(round(hcm_stock))
            total_hn_int = int(round(total_hn))

            status = ""
            pull_from_hcm = 0
            shortage = 0

            # 1. Nếu SL cần giao <= tồn kho 201/201 -> đủ
            if qty_need <= hn_stock:
                status = "ĐỦ tại kho HN (201/201)"
            # 2. Nếu thiếu 201/201 nhưng tổng HN (HN + Kho nhập HN) vẫn đủ
            elif qty_need <= total_hn:
                status = "ĐỦ (HN + Kho nhập HN)"
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
                'Tồn Kho HN (201/201)': hn_stock_int,
                'Tồn Kho Nhập HN': hn_transit_int,
                'Tổng tồn HN': total_hn_int,
                'Tồn Kho HCM (124/124)': hcm_stock_int,
                'Trạng thái': status,
                'SL cần kéo từ HCM': pull_from_hcm,
                'SL còn thiếu': shortage
            })

        result_df = pd.DataFrame(rows)
        COLUMNS_ORDER = [
            'Mã SP',
            'Tên SP',
            'ĐV nhận',
            'SL cần giao',
            'Tồn Kho HN (201/201)',
            'Tồn Kho Nhập HN',
            'Tổng tồn HN',
            'Tồn Kho HCM (124/124)',
            'Trạng thái',
            'SL cần kéo từ HCM',
            'SL còn thiếu',
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
    await update.message.reply_text(f"đang tra tồn cho `{product_code}`, vui lòng chờ!", parse_mode="Markdown")

    uid, models, error_msg = connect_odoo()
    if not uid:
        await update.message.reply_text(f"❌ Lỗi kết nối Odoo: {error_msg}")
        return

    try:
        products = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'product.product', 'search_read',
            [[(PRODUCT_CODE_FIELD, '=', product_code)]],
            {'fields': ['id', 'display_name', PRODUCT_CODE_FIELD]}
        )

        if not products:
            await update.message.reply_text(f"Không tìm thấy sản phẩm với mã `{product_code}` trên Odoo.", parse_mode="Markdown")
            return

        product = products[0]
        prod_id = product['id']
        prod_name = product['display_name']
        prod_code = product.get(PRODUCT_CODE_FIELD, 'N/A')

        summary, detail_rows = get_stock_detail_for_product(models, uid, prod_id)

        hn_qty = summary['hn']
        hcm_qty = summary['hcm']
        hn_transit_qty = summary['hn_transit']

        total_hn = hn_qty + hn_transit_qty

        msg_lines = []
        msg_lines.append(f"*Kết quả tra tồn cho:* `{prod_code}` - *{prod_name}*")
        msg_lines.append("")
        msg_lines.append("*1/ Tồn kho tổng quan:*")
        msg_lines.append(f"- Kho HN (201/201): *{int(round(hn_qty))}*")
        msg_lines.append(f"- Kho nhập HN: *{int(round(hn_transit_qty))}*")
        msg_lines.append(f"- Tổng tồn HN (HN + nhập): *{int(round(total_hn))}*")
        msg_lines.append(f"- Kho HCM (124/124): *{int(round(hcm_qty))}*")

        if total_hn < TARGET_MIN_QTY and hcm_qty > 0:
            need = TARGET_MIN_QTY - total_hn
            suggest = min(need, hcm_qty)
            msg_lines.append("")
            msg_lines.append(
                f"➡ Đề xuất kéo *{int(round(suggest))}* sp từ HCM về HN "
                f"để đạt mức tối thiểu *{TARGET_MIN_QTY}*."
            )

        msg_lines.append("")
        msg_lines.append("*2/ Tồn kho chi tiết(Có hàng):*")

        prioritized_names = []
        for p in PRIORITY_LOCATIONS:
            prioritized_names.append(p)

        def get_priority(loc_name):
            for idx, key in enumerate(prioritized_names):
                if key in loc_name:
                    return idx
            return len(prioritized_names) + 1

        detail_rows_filtered = [r for r in detail_rows if r['available_quantity'] > 0]
        detail_rows_sorted = sorted(detail_rows_filtered, key=lambda r: get_priority(r['location_name']))

        if not detail_rows_sorted:
            msg_lines.append("_Không có kho nào còn hàng._")
        else:
            for r in detail_rows_sorted:
                msg_lines.append(
                    f"- {r['location_name']}: *{int(round(r['available_quantity']))}*"
                )

        await update.message.reply_text("\n".join(msg_lines), parse_mode="Markdown")
    except Exception as e:
        logger.error(f"lỗi khi tra tồn sản phẩm: {e}")
        await update.message.reply_text(f"❌ Lỗi khi tra tồn sản phẩm: {e}")


# ---------------- Telegram Handlers ----------------
async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Đang kiểm tra kết nối odoo, xin chờ...")
    uid, _, error_msg = connect_odoo()
    if uid:
        await update.message.reply_text(
            f"✅ Thành công! kết nối odoo db: {ODOO_DB} tại {ODOO_URL_RAW}. user id: {uid}"
        )
    else:
        await update.message.reply_text(f"❌ Lỗi! chi tiết: {error_msg}")


async def excel_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "⌛️ Iem đang xử lý dữ liệu và tạo báo cáo Excel. Chờ em xíu xìu xiu nhá..."
    )
    excel_buffer, item_count, error_msg = get_stock_data()
    if excel_buffer is None:
        await update.message.reply_text(
            f"❌ Lỗi kết nối odoo hoặc lỗi nghiệp vụ. chi tiết: {error_msg}"
        )
        return
    if item_count > 0:
        await update.message.reply_document(
            document=excel_buffer,
            filename='de_xuat_keo_hang.xlsx',
            caption=f"✅ iem đây! đã tìm thấy {item_count} sản phẩm cần kéo hàng."
        )
    else:
        await update.message.reply_text(
            f"✅ Tất cả sản phẩm đã đạt mức tồn kho tối thiểu {TARGET_MIN_QTY} tại kho HN."
        )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.message.from_user.first_name
    welcome_message = (
        f"Chào mừng {user_name} đến với cuộc đời iem!\n\n"
        "1. Gõ mã sp (vd: I-78) để tra tồn.\n"
        "2. Dùng lệnh /keohang để tạo báo cáo excel.\n"
        "3. Dùng lệnh /ping để kiểm tra kết nối.\n"
        "4. Không có nhu cầu thì đừng phiền iem!"
    )
    await update.message.reply_text(welcome_message)


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
    application.add_handler(CommandHandler("checkpo", checkpo_command))
    application.add_handler(MessageHandler(filters.Document.ALL, handle_po_file))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_product_code))

    logger.info("bot đang chạy...")
    application.run_polling()


# ---------------- HTTP server để ping bot (giữ bot tỉnh) ----------------
class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Bot is alive!")


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
