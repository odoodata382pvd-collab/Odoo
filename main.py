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
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# ---------------- Keep port open (Render free) ----------------
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

# ---------------- Odoo connect ----------------
def connect_odoo():
    try:
        if not ODOO_URL_FINAL:
            return None, None, "odoo url không được thiết lập."

        common = xmlrpc.client.ServerProxy(
            f"{ODOO_URL_FINAL}/xmlrpc/2/common",
            context=ssl._create_unverified_context()
        )

        uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD, {})

        if not uid:
            return None, None, "Đăng nhập thất bại. Kiểm tra DB/user/pass."

        models = xmlrpc.client.ServerProxy(
            f"{ODOO_URL_FINAL}/xmlrpc/2/object",
            context=ssl._create_unverified_context()
        )

        return uid, models, "OK"

    except Exception as e:
        return None, None, f"Lỗi kết nối: {e}"
# ---------------- Helpers ----------------

# LẤY TỒN TRANSIT = quantity (HIỆN CÓ)
def get_transit_qty(models, uid, product_id, transit_id):
    quant_data = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        'stock.quant', 'search_read',
        [[('product_id', '=', product_id), ('location_id', '=', transit_id)]],
        {'fields': ['quantity']}
    )
    total = 0
    for q in quant_data:
        total += int(q.get('quantity') or 0)
    return total


def find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD):
    out = {}

    def search_by_code_or_name(keys):
        locs = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'stock.location', 'search_read',
            [[('usage', '=', 'internal')]],
            {'fields': ['id', 'display_name', 'complete_name']}
        )
        if not locs:
            return None

        for key in keys:
            key_low = key.lower()
            for l in locs:
                full = (l['complete_name'] or "").lower()
                disp = (l['display_name'] or "").lower()
                if key_low in disp or key_low in full:
                    return {'id': l['id'], 'name': l['display_name']}
        return None

    out['HN_STOCK'] = search_by_code_or_name([LOCATION_MAP['HN_STOCK_CODE']])
    out['HCM_STOCK'] = search_by_code_or_name([LOCATION_MAP['HCM_STOCK_CODE']])

    # ƯU TIÊN ĐÚNG KHO NHẬP HÀ NỘI
    out['HN_TRANSIT'] = search_by_code_or_name([
        "kho nhập hà nội",
        "kho nhap ha noi",
        LOCATION_MAP['HN_TRANSIT_NAME'],
        "hn transit",
        "hn nhập",
        "kho nhập"
    ])

    return out


def escape_markdown(text):
    chars = ['\\','_','*','[',']','(',')','~','`','>','#','+','-','=','|','{','}','.','!']
    text = str(text)
    for c in chars:
        text = text.replace(c, f"\\{c}")
    return text.replace('\\`', '`')


# ---------------- Report /keohang ----------------
def get_stock_data():
    uid, models, error_msg = connect_odoo()
    if not uid:
        return None, 0, error_msg

    try:
        location_ids = find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD)
        hn_id   = location_ids.get('HN_STOCK', {}).get('id')
        tran_id = location_ids.get('HN_TRANSIT', {}).get('id')
        hcm_id  = location_ids.get('HCM_STOCK', {}).get('id')

        quant_data_raw = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'stock.quant', 'search_read',
            [[('location_id', 'in', [hn_id, tran_id, hcm_id])]],
            {'fields': ['product_id', 'location_id', 'quantity', 'available_quantity']}
        )

        stock_map = {}

        for q in quant_data_raw:
            pid = q['product_id'][0]
            loc = q['location_id'][0]

            # HN + HCM = available_quantity (GIỮ NGUYÊN)
            if loc == hn_id or loc == hcm_id:
                qty = float(q.get('available_quantity') or 0)

            # TRANSIT = quantity (HIỆN CÓ)
            elif loc == tran_id:
                qty = float(q.get('quantity') or 0)

            else:
                continue

            if qty <= 0:
                continue

            if pid not in stock_map:
                stock_map[pid] = {'hn': 0, 'tran': 0, 'hcm': 0}

            if loc == hn_id:
                stock_map[pid]['hn'] += qty
            elif loc == tran_id:
                stock_map[pid]['tran'] += qty
            elif loc == hcm_id:
                stock_map[pid]['hcm'] += qty
        if not stock_map:
            df_empty = pd.DataFrame(columns=[
                'Mã SP', 'Tên SP', 'Tồn Kho HN',
                'Tồn Kho HCM', 'Kho Nhập HN', 'Số Lượng Đề Xuất'
            ])
            buf = io.BytesIO()
            df_empty.to_excel(buf, index=False, sheet_name='DeXuatKeoHang')
            buf.seek(0)
            return buf, 0, "không có SP nào cần kéo"

        # --------------------------
        # Lấy tên sản phẩm
        # --------------------------
        pids = list(stock_map.keys())
        product_info = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'product.product', 'search_read',
            [[('id', 'in', pids)]],
            {'fields': ['display_name', PRODUCT_CODE_FIELD]}
        )
        product_map = {p['id']: p for p in product_info}

        # --------------------------
        # Build báo cáo kéo hàng
        # --------------------------
        report = []

        for pid, qtys in stock_map.items():
            prod = product_map.get(pid)
            if not prod:
                continue

            code = prod.get(PRODUCT_CODE_FIELD, '')
            name = prod.get('display_name', '')

            ton_hn   = int(round(qtys['hn']))      # available_quantity
            ton_tran = int(round(qtys['tran']))    # quantity (HIỆN CÓ)
            ton_hcm  = int(round(qtys['hcm']))     # available_quantity

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
        cols = ['Mã SP', 'Tên SP', 'Tồn Kho HN', 'Tồn Kho HCM', 'Kho Nhập HN', 'Số Lượng Đề Xuất']

        if not df.empty:
            df = df[cols]
        else:
            df = pd.DataFrame(columns=cols)

        buf = io.BytesIO()
        df.to_excel(buf, index=False, sheet_name="DeXuatKeoHang")
        buf.seek(0)

        return buf, len(df), "thành công"

    except Exception as e:
        error_msg = f"lỗi khi xử lý kéo hàng: {e}"
        logger.error(error_msg)
        return None, 0, error_msg


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
        if any(key in row_text for key in ["model", "mã sp", "ma sp", "mã hàng", "ma hang", "mã sản phẩm", "ma san pham"]):
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

    # Ưu tiên cột "Model"
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
        code_col = find_col(['mã sp', 'ma sp', 'mã hàng', 'ma hang', 'mã sản phẩm', 'ma san pham'])

    qty_col = find_col(['sl', 'số lượng', 'so luong', 's.l', 'sl đặt', 'sl dat'])
    recv_col = find_col([
        'đv nhận', 'dv nhận', 'đơn vị nhận', 'don vi nhan',
        'đv nhận hàng', 'dv nhận hang', 'cửa hàng nhận', 'cua hang nhan'
    ])

    return code_col, qty_col, recv_col
def _get_stock_for_product_with_cache(models, uid, product_id, location_ids, cache):
    """
    GIỮ NGUYÊN – KHÔNG ĐỘNG VÀO.
    HN & HCM vẫn lấy qty_available như cũ (đúng).
    Transit được xử lý RIÊNG bằng get_transit_qty() tại nơi gọi.
    """
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
        'transit': _get_qty(transit_id),  # Không dùng giá trị này
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
                    'Tên SP': "KHÔNG TÌM THẤY",
                    'ĐV nhận': receiver,
                    'SL cần giao': need_qty,
                    'Tồn HN': 0,
                    'Tồn Kho Nhập': 0,
                    'Tổng tồn HN': 0,
                    'Tồn HCM': 0,
                    'Trạng thái': "KHÔNG TÌM THẤY MẪU",
                    'SL cần kéo từ HCM': 0,
                    'SL thiếu': need_qty,
                })
                continue

            pid = prod['id']
            name = prod['display_name']

            stock = _get_stock_for_product_with_cache(models, uid, pid, location_ids, stock_cache)

            hn  = stock['hn']
            hcm = stock['hcm']

            # NEW — Transit = quantity (HIỆN CÓ)
            tr = get_transit_qty(models, uid, pid, location_ids.get('HN_TRANSIT').get('id'))

            total_hn = hn + tr
            pull = 0
            shortage = 0

            if need_qty <= hn:
                status = "ĐỦ tại kho HN"
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
        df_out.to_excel(buf, index=False, sheet_name="KiemTraPO")
        buf.seek(0)
        return buf, None

    except Exception as e:
        return None, f"Lỗi khi xử lý PO: {e}"
# ---------------- Handle product code ----------------
async def handle_product_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    product_code = update.message.text.strip().upper()
    await update.message.reply_text(
        f"đang tra tồn cho `{product_code}`, vui lòng chờ!",
        parse_mode='Markdown'
    )

    uid, models, error_msg = connect_odoo()
    if not uid:
        await update.message.reply_text(
            f"❌ lỗi kết nối odoo. chi tiết: `{escape_markdown(error_msg)}`",
            parse_mode='Markdown'
        )
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

        # HN & HCM = qty_available — giữ nguyên
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

        hn_stock_qty = get_qty_available(hn_stock_id)
        hcm_stock_qty = get_qty_available(hcm_stock_id)

        # NEW — Transit = quantity (HIỆN CÓ)
        hn_transit_qty = get_transit_qty(models, uid, product_id, hn_transit_id)

        # Tồn chi tiết: GIỮ NGUYÊN
        quant_domain = [('product_id', '=', product_id), ('available_quantity', '>', 0)]
        quant_data = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'stock.quant','search_read',
            [quant_domain],
            {'fields': ['location_id','available_quantity']}
        )

        location_ids_list = list({q['location_id'][0] for q in quant_data if q.get('location_id')})
        if location_ids_list:
            location_info = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                'stock.location','read',
                [location_ids_list],
                {'fields':['id','display_name','complete_name','usage']}
            )
        else:
            location_info = []

        loc_map = {l['id']:l for l in location_info}
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
                loc_map.get(loc_id,{}).get('complete_name')
                or loc_map.get(loc_id,{}).get('display_name')
                or f"ID:{loc_id}"
            )
            stock_details[name_loc] = stock_details.get(name_loc,0) + int(qty)

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
            f"=> đề xuất nhập thêm {int(recommend)} sp để hn đủ tồn {TARGET_MIN_QTY} sản phẩm.\n\n"
            f"2/ Tồn kho chi tiết(Có hàng):"
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
async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Đang kiểm tra kết nối Odoo...")
    uid, _, error_msg = connect_odoo()
    if uid:
        await update.message.reply_text(f"✅ Thành công! Kết nối Odoo DB: {ODOO_DB}")
    else:
        await update.message.reply_text(f"❌ Lỗi: {error_msg}")


async def excel_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⌛ Iem đang xử lý báo cáo kéo hàng...")
    excel_buffer, item_count, error_msg = get_stock_data()

    if excel_buffer is None:
        await update.message.reply_text(f"❌ Lỗi: {error_msg}")
        return

    if item_count > 0:
        await update.message.reply_document(
            document=excel_buffer,
            filename="de_xuat_keo_hang.xlsx",
            caption=f"Tìm thấy {item_count} sản phẩm cần kéo hàng."
        )
    else:
        await update.message.reply_text(
            f"Không có sản phẩm nào cần kéo hàng (đủ tồn {TARGET_MIN_QTY})."
        )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.from_user.first_name
    await update.message.reply_text(
        f"Chào {name}!\n"
        "1. Gõ mã SP để tra tồn.\n"
        "2. /keohang → tạo báo cáo kéo hàng.\n"
        "3. /ping → kiểm tra kết nối Odoo."
    )


async def checkpo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['waiting_for_po'] = True
    await update.message.reply_text("Gửi file PO (.xlsx) vào đây để kiểm tra tồn kho nha!")


async def handle_po_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('waiting_for_po'):
        return

    context.user_data['waiting_for_po'] = False

    document = update.message.document
    if not document:
        await update.message.reply_text("Không nhận được file, vui lòng gửi lại.")
        return

    file_name = (document.file_name or "").lower()
    if not file_name.endswith(".xlsx"):
        await update.message.reply_text("Chỉ hỗ trợ file Excel (.xlsx).")
        return

    await update.message.reply_text("⏳ Đang xử lý PO...")

    try:
        file = await document.get_file()
        file_bytes = await file.download_as_bytearray()
    except Exception as e:
        await update.message.reply_text(f"❌ Lỗi tải file: {e}")
        return

    excel_buffer, error_msg = process_po_and_build_report(bytes(file_bytes))
    if excel_buffer is None:
        await update.message.reply_text(f"❌ Lỗi xử lý PO: {error_msg}")
        return

    await update.message.reply_document(
        document=excel_buffer,
        filename="kiem_tra_po.xlsx",
        caption="✔ File kiểm tra PO đây ạ!"
    )


# ---------------- HTTP server giữ bot sống ----------------
from http.server import BaseHTTPRequestHandler, HTTPServer

class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type","text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

    def log_message(self, format, *args):
        return


def start_http():
    try:
        server = HTTPServer(("0.0.0.0", 10001), PingHandler)
        logger.info("HTTP ping server đã chạy trên port 10001")
        server.serve_forever()
    except Exception as e:
        logger.error(f"Lỗi HTTP server: {e}")

threading.Thread(target=start_http, daemon=True).start()


# ---------------- AUTO PING GIỮ SỐNG BOT (KHÔNG DÙNG requests) ----------------
import urllib.request
import time

PING_URL = "https://google.com"   # Ping ra ngoài để Render không tắt bot

def keep_alive_ping():
    while True:
        try:
            urllib.request.urlopen(PING_URL, timeout=10)
            logger.info("Keep-alive ping sent.")
        except Exception as e:
            logger.warning(f"Keep-alive ping failed: {e}")
        time.sleep(300)  # Ping mỗi 5 phút

threading.Thread(target=keep_alive_ping, daemon=True).start()


# ---------------- Run ----------------
def main():
    if not TELEGRAM_TOKEN or not ODOO_URL_RAW or not ODOO_DB or not ODOO_USERNAME or not ODOO_PASSWORD:
        logger.error("Thiếu cấu hình môi trường!")
        return

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        asyncio.get_event_loop().run_until_complete(bot.delete_webhook())
        logger.info("Đã xoá webhook cũ (nếu có).")
    except Exception as e:
        logger.warning(f"Lỗi xoá webhook: {e}")

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", start_command))
    application.add_handler(CommandHandler("ping", ping_command))
    application.add_handler(CommandHandler("keohang", excel_report_command))
    application.add_handler(CommandHandler("checkpo", checkpo_command))

    application.add_handler(MessageHandler(filters.Document.ALL, handle_po_file))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_product_code))

    logger.info("Bot started!")
    application.run_polling()


if __name__ == "__main__":
    main()
