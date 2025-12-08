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
from datetime import datetime, timedelta
from urllib.parse import urlparse
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ---------------- CONFIG & ENVIRONMENT ----------------
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")

ODOO_URL_RAW = os.environ.get("ODOO_URL").rstrip("/") if os.environ.get("ODOO_URL") else None
if ODOO_URL_RAW and ODOO_URL_RAW.lower().endswith("/odoo"):
    ODOO_URL_FINAL = ODOO_URL_RAW[:-len("/odoo")]
else:
    ODOO_URL_FINAL = ODOO_URL_RAW

ODOO_DB = os.environ.get("ODOO_DB")
ODOO_USERNAME = os.environ.get("ODOO_USERNAME")
ODOO_PASSWORD = os.environ.get("ODOO_PASSWORD")

TARGET_MIN_QTY = 50

LOCATION_MAP = {
    "HN_STOCK_CODE": "201/201",              # Kho HN
    "HCM_STOCK_CODE": "124/124",            # Kho HCM
    "HN_TRANSIT_NAME": "Kho nhập Hà Nội",   # Kho Nhập HN (Transit)
}

PRIORITY_LOCATIONS = [
    LOCATION_MAP["HN_STOCK_CODE"],
    LOCATION_MAP["HN_TRANSIT_NAME"],
    LOCATION_MAP["HCM_STOCK_CODE"],
]

PRODUCT_CODE_FIELD = "default_code"

# ---------------- LOGGING ----------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ---------------- KEEP RENDER PORT ALIVE ----------------
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

# ---------------- ODOO CONNECTION ----------------
def connect_odoo():
    try:
        if not ODOO_URL_FINAL:
            return None, None, "Thiếu URL Odoo."

        common = xmlrpc.client.ServerProxy(
            f"{ODOO_URL_FINAL}/xmlrpc/2/common",
            context=ssl._create_unverified_context()
        )

        uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD, {})
        if not uid:
            return None, None, "Sai DB/User/Pass khi đăng nhập Odoo."

        models = xmlrpc.client.ServerProxy(
            f"{ODOO_URL_FINAL}/xmlrpc/2/object",
            context=ssl._create_unverified_context()
        )

        return uid, models, "OK"

    except Exception as e:
        return None, None, f"Lỗi kết nối Odoo: {e}"


def get_odoo_url_components():
    if not ODOO_URL_FINAL:
        return None, None

    parsed = urlparse(ODOO_URL_FINAL)
    scheme = parsed.scheme
    netloc = parsed.netloc

    if scheme == "http":
        port = parsed.port or 80
    elif scheme == "https":
        port = parsed.port or 443
    else:
        port = None

    return netloc, port

# ---------------- LOCATION DETECTION ----------------
def find_required_location_ids(models, uid, db, password):
    out = {}

    def search(key):
        locs = models.execute_kw(
            db, uid, password,
            "stock.location", "search_read",
            [[("display_name", "ilike", key)]],
            {"fields": ["id", "display_name", "complete_name"]}
        )
        if not locs:
            return None

        for l in locs:
            if key.lower() in (l["display_name"] or "").lower():
                return {"id": l["id"], "name": l["display_name"]}

        return {"id": locs[0]["id"], "name": locs[0]["display_name"]}

    out["HN_STOCK"] = search(LOCATION_MAP["HN_STOCK_CODE"])
    out["HCM_STOCK"] = search(LOCATION_MAP["HCM_STOCK_CODE"])
    out["HN_TRANSIT"] = search(LOCATION_MAP["HN_TRANSIT_NAME"])

    return out

# ---------------- FIX: KHO NHẬP HN = quantity (HIỆN CÓ) ----------------
def get_transit_quantity(models, uid, product_id, transit_location_id):
    """
    Lấy tồn Kho Nhập Hà Nội đúng theo cột 'Hiện có' (quantity).
    """
    if not transit_location_id:
        return 0

    quant_data = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "stock.quant", "search_read",
        [[("product_id", "=", product_id),
          ("location_id", "=", transit_location_id)]],
        {"fields": ["quantity"]}
    )

    total = 0
    for q in quant_data:
        total += int(q.get("quantity") or 0)

    return total

# ---------------- MISC HELPERS ----------------
def escape_markdown(text):
    chars = ['\\','_','*','[',']','(',')','~','`','>','#','+','-','=','|','{','}','.','!']
    text = str(text)
    for c in chars:
        text = text.replace(c, f"\\{c}")
    return text

# ---------------- STORE CHAT IDS FOR WATCHDOG ----------------
REGISTERED_CHAT_IDS = set()
CHAT_IDS_LOCK = threading.Lock()

def register_chat_id(chat_id):
    if chat_id is None:
        return
    try:
        cid = int(chat_id)
    except:
        cid = chat_id

    with CHAT_IDS_LOCK:
        REGISTERED_CHAT_IDS.add(cid)

def get_registered_chat_ids():
    with CHAT_IDS_LOCK:
        return list(REGISTERED_CHAT_IDS)
# ---------------- REPORT /keohang ----------------
def get_stock_data():
    """
    Báo cáo kéo hàng:
    - HN & HCM = qty_available (Có hàng)
    - Kho Nhập Hà Nội = quantity (Hiện có)
    """
    uid, models, error_msg = connect_odoo()
    if not uid:
        return None, 0, error_msg

    try:
        location_ids = find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD)
        if len(location_ids) < 3:
            msg = f"Không tìm đủ 3 kho: {list(location_ids.keys())}"
            logger.error(msg)
            return None, 0, msg

        hn_id = location_ids["HN_STOCK"]["id"]
        hcm_id = location_ids["HCM_STOCK"]["id"]
        tran_id = location_ids["HN_TRANSIT"]["id"]

        # Lấy toàn bộ stock.quant của 3 kho
        quant_raw = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "stock.quant", "search_read",
            [[("location_id", "in", [hn_id, hcm_id, tran_id])]],
            {"fields": ["product_id", "location_id",
                        "quantity", "reserved_quantity", "available_quantity"]}
        )

        stock_map = {}

        for q in quant_raw:
            pid = q["product_id"][0]
            loc = q["location_id"][0]

            # FIX: Kho Nhập Hà Nội dùng "Hiện có" (quantity)
            if loc == tran_id:
                qty = float(q.get("quantity") or 0)

            # HN & HCM: dùng available_quantity
            else:
                if q.get("available_quantity") is not None:
                    qty = float(q.get("available_quantity") or 0)
                else:
                    qty = float(q.get("quantity") or 0) - float(q.get("reserved_quantity") or 0)

            if qty <= 0:
                continue

            if pid not in stock_map:
                stock_map[pid] = {"hn": 0, "tran": 0, "hcm": 0}

            if loc == hn_id:
                stock_map[pid]["hn"] += qty
            elif loc == tran_id:
                stock_map[pid]["tran"] += qty
            elif loc == hcm_id:
                stock_map[pid]["hcm"] += qty

        if not stock_map:
            df_empty = pd.DataFrame(columns=[
                "Mã SP", "Tên SP", "Tồn Kho HN", "Tồn Kho HCM",
                "Kho Nhập HN", "Số Lượng Đề Xuất"
            ])
            buf = io.BytesIO()
            df_empty.to_excel(buf, index=False, sheet_name="DeXuatKeoHang")
            buf.seek(0)
            return buf, 0, "Không có sản phẩm cần kéo."

        # Lấy tên SP
        pids = list(stock_map.keys())
        info = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "product.product", "search_read",
            [[("id", "in", pids)]],
            {"fields": ["display_name", PRODUCT_CODE_FIELD]}
        )
        pmap = {p["id"]: p for p in info}

        report = []

        for pid, item in stock_map.items():
            prod = pmap.get(pid)
            if not prod:
                continue

            code = prod.get(PRODUCT_CODE_FIELD, "")
            name = prod.get("display_name", "")

            ton_hn   = int(item["hn"])
            ton_tran = int(item["tran"])
            ton_hcm  = int(item["hcm"])

            tong_hn = ton_hn + ton_tran

            if tong_hn < TARGET_MIN_QTY:
                need = TARGET_MIN_QTY - tong_hn
                de_xuat = min(need, ton_hcm)

                if de_xuat > 0:
                    report.append({
                        "Mã SP": code,
                        "Tên SP": name,
                        "Tồn Kho HN": ton_hn,
                        "Tồn Kho HCM": ton_hcm,
                        "Kho Nhập HN": ton_tran,
                        "Số Lượng Đề Xuất": de_xuat
                    })

        df = pd.DataFrame(report)

        cols = [
            "Mã SP", "Tên SP", "Tồn Kho HN", "Tồn Kho HCM",
            "Kho Nhập HN", "Số Lượng Đề Xuất"
        ]

        if not df.empty:
            df = df[cols]
        else:
            df = pd.DataFrame(columns=cols)

        buffer = io.BytesIO()
        df.to_excel(buffer, index=False, sheet_name="DeXuatKeoHang")
        buffer.seek(0)

        return buffer, len(df), "OK"

    except Exception as e:
        return None, 0, f"Lỗi xử lý kéo hàng: {e}"


# ---------------- PO HELPERS ----------------

def _read_po_with_auto_header(file_bytes: bytes):
    try:
        df_tmp = pd.read_excel(io.BytesIO(file_bytes), header=None)
    except Exception as e:
        return None, f"Lỗi đọc file PO: {e}"

    header_idx = None
    for idx in range(len(df_tmp)):
        row = df_tmp.iloc[idx].astype(str).str.lower()
        row_text = " ".join(row)
        if any(k in row_text for k in
               ["model", "mã sp", "ma sp", "mã hàng", "ma hang", "mã sản phẩm", "ma san pham"]):
            header_idx = idx
            break

    if header_idx is None:
        header_idx = 0

    try:
        df_raw = pd.read_excel(io.BytesIO(file_bytes), header=header_idx)
        return df_raw, None
    except Exception as e:
        return None, f"Lỗi đọc file PO với header dòng {header_idx+1}: {e}"


def _detect_po_columns(df: pd.DataFrame):
    cols = {col: str(col).lower().strip() for col in df.columns}

    # Tìm cột mã SP
    code_col = None
    for col, l in cols.items():
        if l == "model":
            code_col = col
            break
    if not code_col:
        for col, l in cols.items():
            if "model" == l:
                code_col = col
                break

    def find(candidates):
        for col, l in cols.items():
            for c in candidates:
                if c in l:
                    return col
        return None

    if not code_col:
        code_col = find(["mã sp", "ma sp", "mã hàng", "ma hang", "mã sản phẩm", "ma san pham"])

    qty_col = find(["sl", "số lượng", "so luong", "sl đặt", "sl dat"])
    recv_col = find(["đv nhận", "dv nhận", "đơn vị nhận", "don vi nhan", "cửa hàng nhận"])

    return code_col, qty_col, recv_col


# ---------------- CACHE STOCK FOR PO ----------------
def _get_stock_for_product_with_cache(models, uid, product_id, location_ids, cache):
    """
    HN & HCM = qty_available
    Transit = LẤY LẠI bằng get_transit_quantity(), không lấy ở đây.
    """
    if product_id in cache:
        return cache[product_id]

    hn_id   = location_ids["HN_STOCK"]["id"]
    hcm_id  = location_ids["HCM_STOCK"]["id"]

    def get_qty(location_id):
        if not location_id:
            return 0
        data = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "product.product", "read",
            [[product_id]],
            {"fields": ["qty_available"], "context": {"location": location_id}}
        )
        if data and data[0]:
            return int(data[0].get("qty_available", 0))
        return 0

    result = {
        "hn": get_qty(hn_id),
        "transit": 0,     # Không dùng, transit sẽ tính đúng bằng quantity
        "hcm": get_qty(hcm_id),
    }

    cache[product_id] = result
    return result
# ---------------- PROCESS PO AND BUILD REPORT ----------------
def process_po_and_build_report(file_bytes: bytes):
    df_raw, err = _read_po_with_auto_header(file_bytes)
    if df_raw is None:
        return None, err

    if df_raw.empty:
        return None, "File PO không có dữ liệu."

    code_col, qty_col, recv_col = _detect_po_columns(df_raw)
    if not code_col or not qty_col or not recv_col:
        return None, (
            "Không xác định được các cột Model – Số lượng – ĐV nhận.\n"
            f"Các cột hiện có: {list(df_raw.columns)}"
        )

    df = df_raw[[code_col, qty_col, recv_col]].copy()
    df.columns = ["Mã SP", "SL cần giao", "ĐV nhận"]

    df["Mã SP"] = df["Mã SP"].astype(str).str.strip().upper()
    df["SL cần giao"] = pd.to_numeric(df["SL cần giao"], errors="coerce").fillna(0)
    df = df[(df["Mã SP"] != "") & (df["SL cần giao"] > 0)]

    if df.empty:
        return None, "Không có dòng hợp lệ để xử lý."

    uid, models, error_msg = connect_odoo()
    if not uid:
        return None, error_msg

    try:
        codes = sorted(df["Mã SP"].unique().tolist())

        products = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "product.product", "search_read",
            [[(PRODUCT_CODE_FIELD, "in", codes)]],
            {"fields": ["id", "display_name", PRODUCT_CODE_FIELD]}
        )

        code_map = {}
        for p in products:
            c = str(p.get(PRODUCT_CODE_FIELD) or "").strip().upper()
            code_map[c] = p

        # Lấy ID kho
        location_ids = find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD)

        stock_cache = {}
        rows = []

        for _, r in df.iterrows():
            code = r["Mã SP"]
            need_qty = int(r["SL cần giao"])
            receiver = r["ĐV nhận"]

            prod = code_map.get(code)

            if not prod:
                rows.append({
                    "Mã SP": code,
                    "Tên SP": "KHÔNG TÌM THẤY",
                    "ĐV nhận": receiver,
                    "SL cần giao": need_qty,
                    "Tồn HN": 0,
                    "Tồn Kho Nhập": 0,
                    "Tổng tồn HN": 0,
                    "Tồn HCM": 0,
                    "Trạng thái": "KHÔNG TÌM THẤY MÃ",
                    "SL cần kéo từ HCM": 0,
                    "SL thiếu": need_qty,
                })
                continue

            pid = prod["id"]
            name = prod["display_name"]

            # Dữ liệu cache dùng qty_available (HN & HCM)
            stock = _get_stock_for_product_with_cache(
                models, uid, pid, location_ids, stock_cache
            )

            hn  = stock["hn"]
            hcm = stock["hcm"]

            # FIX: Kho Nhập Hà Nội = quantity (HIỆN CÓ)
            tr = get_transit_quantity(
                models, uid, pid,
                location_ids["HN_TRANSIT"]["id"]
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
                "Mã SP": code,
                "Tên SP": name,
                "ĐV nhận": receiver,
                "SL cần giao": need_qty,
                "Tồn HN": hn,
                "Tồn Kho Nhập": tr,
                "Tổng tồn HN": total_hn,
                "Tồn HCM": hcm,
                "Trạng thái": status,
                "SL cần kéo từ HCM": pull,
                "SL thiếu": shortage,
            })

        df_out = pd.DataFrame(rows)

        cols = [
            "Mã SP", "Tên SP", "ĐV nhận", "SL cần giao",
            "Tồn HN", "Tồn Kho Nhập", "Tổng tồn HN", "Tồn HCM",
            "Trạng thái", "SL cần kéo từ HCM", "SL thiếu"
        ]

        df_out = df_out[cols]

        buffer = io.BytesIO()
        df_out.to_excel(buffer, index=False, sheet_name="KiemTraPO")
        buffer.seek(0)

        return buffer, None

    except Exception as e:
        return None, f"Lỗi xử lý PO: {e}"


# ---------------- HANDLE PRODUCT CODE (TRA TỒN) ----------------
async def handle_product_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    register_chat_id(chat_id)

    product_code = update.message.text.strip().upper()
    await update.message.reply_text(
        f"đang tra tồn cho `{product_code}`, vui lòng chờ…`",
        parse_mode="Markdown"
    )

    uid, models, error_msg = connect_odoo()
    if not uid:
        await update.message.reply_text(
            f"❌ Không kết nối được Odoo: `{escape_markdown(error_msg)}`",
            parse_mode="Markdown"
        )
        return

    try:
        locs = find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD)
        hn_id = locs["HN_STOCK"]["id"]
        hcm_id = locs["HCM_STOCK"]["id"]
        tran_id = locs["HN_TRANSIT"]["id"]

        # Lấy sản phẩm
        product = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "product.product", "search_read",
            [[(PRODUCT_CODE_FIELD, "=", product_code)]],
            {"fields": ["id", "display_name"]}
        )

        if not product:
            await update.message.reply_text(f"❌ Không tìm thấy mã `{product_code}`")
            return

        product = product[0]
        pid = product["id"]
        product_name = product["display_name"]

        # Lấy tồn HN & HCM = qty_available
        def get_qty_available(loc_id):
            if not loc_id:
                return 0
            res = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                "product.product", "read",
                [[pid]],
                {"fields": ["qty_available"], "context": {"location": loc_id}}
            )
            if res and res[0]:
                return int(res[0].get("qty_available", 0))
            return 0

        hn_qty = get_qty_available(hn_id)
        hcm_qty = get_qty_available(hcm_id)

        # FIX: Kho Nhập Hà Nội = quantity
        tran_qty = get_transit_quantity(models, uid, pid, tran_id)

        # Lấy tồn chi tiết (available_quantity)
        quant_data = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "stock.quant", "search_read",
            [[("product_id", "=", pid),
              ("available_quantity", ">", 0)]],
            {"fields": ["location_id", "available_quantity"]}
        )

        # Lấy tên kho
        if quant_data:
            loc_ids = list({q["location_id"][0] for q in quant_data})
            loc_info = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                "stock.location", "read",
                [loc_ids],
                {"fields": ["id", "display_name", "complete_name"]}
            )
            loc_map = {l["id"]: l for l in loc_info}
        else:
            loc_map = {}

        # Gom tồn chi tiết
        detail = {}
        for q in quant_data:
            loc_id = q["location_id"][0]
            qty = int(q.get("available_quantity") or 0)

            name = (
                loc_map.get(loc_id, {}).get("complete_name")
                or loc_map.get(loc_id, {}).get("display_name")
                or f"ID:{loc_id}"
            )

            detail[name] = detail.get(name, 0) + qty

        total_hn = hn_qty + tran_qty

        recommend = 0
        if total_hn < TARGET_MIN_QTY:
            recommend = min(TARGET_MIN_QTY - total_hn, hcm_qty)

        # Ưu tiên kho
        priority = []
        others = []
        used = set()

        for key in PRIORITY_LOCATIONS:
            for name, qty in detail.items():
                if key.lower() in name.lower() and name not in used:
                    priority.append((name, qty))
                    used.add(name)

        for name, qty in sorted(detail.items()):
            if name not in used:
                others.append((name, qty))
                used.add(name)

        detail_list = priority + others

        msg = (
            f"{product_code} {product_name}\n"
            f"Tồn kho HN: {hn_qty}\n"
            f"Tồn kho HCM: {hcm_qty}\n"
            f"Tồn kho nhập Hà Nội: {tran_qty}\n"
            f"=> đề xuất nhập thêm {recommend} SP để đủ tồn {TARGET_MIN_QTY}.\n\n"
            f"2/ Tồn kho chi tiết (Có hàng):"
        )

        if detail_list:
            for name, qty in detail_list:
                msg += f"\n{name}: {qty}"
        else:
            msg += "\nKhông có tồn chi tiết."

        await update.message.reply_text(msg)

    except Exception as e:
        logger.error(f"Lỗi tra tồn: {e}")
        await update.message.reply_text(f"❌ Lỗi: {e}")
# ---------------- HANDLE FILE PO (UPLOAD) ----------------
async def handle_po_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    register_chat_id(chat_id)

    # Kiểm tra xem người dùng có đang trong chế độ gửi file PO không
    if not context.user_data.get("waiting_for_po"):
        return

    context.user_data["waiting_for_po"] = False

    document = update.message.document
    if not document:
        await update.message.reply_text("❌ Không nhận được file, vui lòng gửi lại file Excel (.xlsx).")
        return

    filename = (document.file_name or "").lower()
    if not filename.endswith(".xlsx"):
        await update.message.reply_text("❌ File không đúng định dạng .xlsx.")
        return

    await update.message.reply_text("⌛ Iem đang xử lý file PO, chị đợi xíu nha...")

    try:
        file = await document.get_file()
        file_bytes = await file.download_as_bytearray()
    except Exception as e:
        await update.message.reply_text(f"❌ Lỗi tải file: {e}")
        return

    excel_buffer, error_msg = process_po_and_build_report(bytes(file_bytes))

    if excel_buffer is None:
        await update.message.reply_text(f"❌ Lỗi khi xử lý file: {error_msg}")
        return

    await update.message.reply_document(
        document=excel_buffer,
        filename="kiem_tra_po.xlsx",
        caption="❤️ Iem gửi chị file kiểm tra PO đây ạ!"
    )

# ---------------- HTTP SERVER 10001 (GIỮ BOT SỐNG) ----------------
class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

    def log_message(self, format, *args):
        return  # Tắt log console

def start_http():
    try:
        server = HTTPServer(("0.0.0.0", 10001), PingHandler)
        logger.info("HTTP keep-alive server đang chạy trên port 10001")
        server.serve_forever()
    except Exception as e:
        logger.error(f"Lỗi HTTP server: {e}")

threading.Thread(target=start_http, daemon=True).start()

# ---------------- AUTO-PING (KHÔNG DÙNG requests) ----------------
PING_URL = "https://google.com"

def keep_alive_ping():
    """
    Ping ra ngoài mỗi 5 phút để Render không sleep.
    """
    while True:
        try:
            urllib.request.urlopen(PING_URL, timeout=10)
            logger.info("Cron-ping sent.")
        except Exception as e:
            logger.warning(f"Cron-ping failed: {e}")
        time.sleep(300)

threading.Thread(target=keep_alive_ping, daemon=True).start()
# ---------------- WATCHDOG KHO 201/201 (CẬP NHẬT REALTIME) ----------------

WATCH_INTERVAL = 60  # kiểm tra mỗi 60 giây
previous_snapshot = {}

def watchdog_201():
    """
    Theo dõi kho 201/201 theo CÓ HÀNG (available_quantity).
    Khi có biến động: nhập / xuất => Gửi thông báo chi tiết.
    """
    global previous_snapshot

    while True:
        try:
            uid, models, err = connect_odoo()
            if not uid:
                logger.error(f"Watchdog không kết nối được Odoo: {err}")
                time.sleep(WATCH_INTERVAL)
                continue

            # Lấy ID kho 201/201
            locs = find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD)
            hn_id = locs["HN_STOCK"]["id"]

            # Lấy toàn bộ quant tại kho 201/201
            quant_data = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                "stock.quant", "search_read",
                [[("location_id", "=", hn_id)]],
                {"fields": ["product_id", "available_quantity"]}
            )

            # Snapshot hiện tại
            current_snapshot = {}
            for q in quant_data:
                pid = q["product_id"][0]
                qty = int(q.get("available_quantity") or 0)
                current_snapshot[pid] = qty

            # Snapshot đầu tiên → lưu nhưng KHÔNG gửi thông báo
            if not previous_snapshot:
                previous_snapshot = current_snapshot
                time.sleep(WATCH_INTERVAL)
                continue

            # So sánh snapshot để tìm SP có biến động
            for pid, new_qty in current_snapshot.items():
                old_qty = previous_snapshot.get(pid, 0)
                if new_qty == old_qty:
                    continue  # không biến động → bỏ qua

                diff = new_qty - old_qty  # >0 nhập; <0 xuất

                # Lấy thông tin SP
                prod = models.execute_kw(
                    ODOO_DB, uid, ODOO_PASSWORD,
                    "product.product", "read",
                    [[pid]],
                    {"fields": ["display_name", PRODUCT_CODE_FIELD]}
                )[0]

                sp_code = prod.get(PRODUCT_CODE_FIELD, "???")
                sp_name = prod.get("display_name", "Không tên")

                # ------------------- LẤY MÃ LỆNH CHUẨN -------------------
                # Tìm stock.move mới nhất của sản phẩm này
                move_data = models.execute_kw(
                    ODOO_DB, uid, ODOO_PASSWORD,
                    "stock.move", "search_read",
                    [[("product_id", "=", pid)]],
                    {"fields": ["picking_id"], "limit": 1, "order": "id desc"}
                )

                move_id_str = "N/A"

                if move_data and move_data[0].get("picking_id"):
                    picking_id = move_data[0]["picking_id"][0]

                    picking_info = models.execute_kw(
                        ODOO_DB, uid, ODOO_PASSWORD,
                        "stock.picking", "read",
                        [[picking_id]],
                        {"fields": ["name"]}
                    )

                    move_id_str = picking_info[0]["name"]

                # ------------------- THỜI GIAN VN (+7) -------------------
                now_vn = datetime.utcnow() + timedelta(hours=7)
                time_str = now_vn.strftime("%H:%M %d/%m/%Y")

                # ------------------- NHẬP / XUẤT -------------------
                status = "NHẬP KHO" if diff > 0 else "XUẤT KHO"

                # ------------------- FORMAT TIN NHẮN -------------------
                msg = (
                    f"📦 Cập nhật tồn kho 201/201 – {status}\n\n"
                    f"Mã SP: {sp_code}\n"
                    f"Tên SP: {sp_name}\n"
                    f"Biến động: {'+' if diff > 0 else ''}{diff} SP\n"
                    f"Tổng tồn sau biến động (có hàng): {new_qty} SP\n\n"
                    f"Thời gian: {time_str}\n"
                    f"Mã lệnh / ID giao dịch: {move_id_str}"
                )

                # ------------------- GỬI CHO TẤT CẢ CHAT ID -------------------
                for chat_id in get_registered_chat_ids():
                    try:
                        bot = Bot(token=TELEGRAM_TOKEN)
                        asyncio.run(bot.send_message(chat_id, msg))
                    except Exception as e:
                        logger.error(f"Lỗi gửi thông báo cho {chat_id}: {e}")

            previous_snapshot = current_snapshot
            time.sleep(WATCH_INTERVAL)

        except Exception as e:
            logger.error(f"Lỗi watchdog: {e}")
            time.sleep(WATCH_INTERVAL)



# ---------------- BOT MAIN ----------------
def main():
    if not TELEGRAM_TOKEN:
        logger.error("Thiếu TELEGRAM_TOKEN")
        return

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Xóa webhook cũ nếu có
    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        asyncio.get_event_loop().run_until_complete(bot.delete_webhook())
    except:
        pass

    # HANDLERS
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
