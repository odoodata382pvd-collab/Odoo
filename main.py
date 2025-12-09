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

# ================== CONFIG ==================
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
    "HN_STOCK_CODE": "201/201",
    "HCM_STOCK_CODE": "124/124",
    "HN_TRANSIT_NAME": "Kho nhập Hà Nội",
}

PRIORITY_LOCATIONS = [
    LOCATION_MAP["HN_STOCK_CODE"],
    LOCATION_MAP["HN_TRANSIT_NAME"],
    LOCATION_MAP["HCM_STOCK_CODE"],
]

PRODUCT_CODE_FIELD = "default_code"

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ================== KEEP PORT 10000 OPEN ==================
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

# ================== ODOO CONNECTION ==================
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

# ================== LOCATION HELPERS ==================
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

    out["HN_STOCK"]  = search(LOCATION_MAP["HN_STOCK_CODE"])
    out["HCM_STOCK"] = search(LOCATION_MAP["HCM_STOCK_CODE"])
    out["HN_TRANSIT"] = search(LOCATION_MAP["HN_TRANSIT_NAME"])
    return out

# ================== KHO NHẬP HN = quantity (HIỆN CÓ) ==================
def get_transit_quantity(models, uid, product_id, transit_location_id):
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

# ================== MISC HELPERS ==================
def escape_markdown(text):
    chars = ['\\','_','*','[',']','(',')','~','`','>','#','+','-','=','|','{','}','.','!']
    text = str(text)
    for c in chars:
        text = text.replace(c, f"\\{c}")
    return text

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
# ================== /KEOHANG REPORT ==================
def get_stock_data():
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

            # HN & HCM dùng available_quantity (tồn có hàng)
            if loc == tran_id:
                qty = float(q.get("quantity") or 0)
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

        # Không có sản phẩm cần kéo
        if not stock_map:
            df_empty = pd.DataFrame(columns=[
                "Mã SP", "Tên SP", "Tồn Kho HN", "Tồn Kho HCM",
                "Kho Nhập HN", "Số Lượng Đề Xuất"
            ])
            buf = io.BytesIO()
            df_empty.to_excel(buf, index=False, sheet_name="DeXuatKeoHang")
            buf.seek(0)
            return buf, 0, "Không có sản phẩm cần kéo."

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


# ================== PO HELPERS ==================
def _read_po_with_auto_header(file_bytes: bytes):
    try:
        df_tmp = pd.read_excel(io.BytesIO(file_bytes), header=None)
    except Exception as e:
        return None, f"Lỗi đọc file PO: {e}"

    header_idx = None
    for idx in range(len(df_tmp)):
        row = df_tmp.iloc[idx].astype(str).str.lower()
        row_text = " ".join(row)
        if any(k in row_text for k in ["model", "mã sp", "ma sp", "mã hàng", "ma hang", "mã sản phẩm", "ma san pham"]):
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

    code_col = None
    for col, v in cols.items():
        if v == "model":
            code_col = col
            break
    if not code_col:
        for col, v in cols.items():
            if "model" == v:
                code_col = col
                break

    def find(candidates):
        for col, v in cols.items():
            for c in candidates:
                if c in v:
                    return col
        return None

    if not code_col:
        code_col = find(["mã sp", "ma sp", "mã hàng", "ma hang", "mã sản phẩm"])

    qty_col  = find(["sl", "số lượng", "so luong", "sl đặt", "sl dat"])
    recv_col = find(["đv nhận", "dv nhận", "đơn vị nhận", "don vi nhan"])

    return code_col, qty_col, recv_col
def _get_stock_for_product_with_cache(models, uid, product_id, location_ids, cache):
    if product_id in cache:
        return cache[product_id]

    hn_id = location_ids["HN_STOCK"]["id"]
    hcm_id = location_ids["HCM_STOCK"]["id"]

    # Lấy tồn kho theo available_quantity (tồn có hàng) — giữ nguyên thuật toán cũ
    quant_data = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD,
        "stock.quant", "search_read",
        [[("product_id", "=", product_id),
          ("location_id", "in", [hn_id, hcm_id])]],
        {"fields": ["location_id", "available_quantity", "quantity", "reserved_quantity"]}
    )

    hn_qty = 0
    hcm_qty = 0

    for q in quant_data:
        loc = q["location_id"][0]

        if q.get("available_quantity") is not None:
            qty = float(q.get("available_quantity") or 0)
        else:
            qty = float(q.get("quantity") or 0) - float(q.get("reserved_quantity") or 0)

        if qty <= 0:
            continue

        if loc == hn_id:
            hn_qty += qty
        elif loc == hcm_id:
            hcm_qty += qty

    result = {
        "hn": int(hn_qty),
        "transit": 0,
        "hcm": int(hcm_qty)
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
            "Không xác định được các cột Model – Số lượng – ĐV nhận.\n"
            f"Các cột hiện có: {list(df_raw.columns)}"
        )

    df = df_raw[[code_col, qty_col, recv_col]].copy()
    df.columns = ["Mã SP", "SL cần giao", "ĐV nhận"]

    # ===== FIX LỖI CHUẨN: upper() => str.upper() =====
    df["Mã SP"] = df["Mã SP"].astype(str).str.strip().str.upper()

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
                    "SL thiếu": need_qty
                })
                continue

            pid = prod["id"]
            name = prod["display_name"]

            stock = _get_stock_for_product_with_cache(models, uid, pid, location_ids, stock_cache)
            hn = stock["hn"]
            hcm = stock["hcm"]
            tr = get_transit_quantity(models, uid, pid, location_ids["HN_TRANSIT"]["id"])
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
                "SL thiếu": shortage
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
# ================== HTTP SERVER (KEEP ALIVE) ==================
class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

    def log_message(self, format, *args):
        return  # Tắt log rác


def start_http_server():
    try:
        server = HTTPServer(("0.0.0.0", 10001), PingHandler)
        logger.info("HTTP keep-alive server đang chạy trên port 10001")
        server.serve_forever()
    except Exception as e:
        logger.error(f"Lỗi HTTP server: {e}")


threading.Thread(target=start_http_server, daemon=True).start()


# ================== AUTO-PING ==================
PING_URL = "https://google.com"

def auto_ping():
    while True:
        try:
            urllib.request.urlopen(PING_URL, timeout=10)
            logger.info("Cron-ping sent.")
        except Exception as e:
            logger.warning(f"Auto-ping lỗi: {e}")
        time.sleep(300)


threading.Thread(target=auto_ping, daemon=True).start()


# ================== WATCHDOG KHO 201/201 ==================
WATCH_INTERVAL = 60
previous_snapshot = {}


def watchdog_201():
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

            # Lấy tồn kho có hàng (available_quantity)
            quant_data = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                "stock.quant", "search_read",
                [[("location_id", "=", hn_id)]],
                {"fields": ["product_id", "available_quantity"]}
            )

            # Build snapshot mới
            current_snapshot = {}
            for q in quant_data:
                pid = q["product_id"][0]
                qty = int(q.get("available_quantity") or 0)
                current_snapshot[pid] = qty

            # Lần chạy đầu không thông báo
            if not previous_snapshot:
                previous_snapshot = current_snapshot
                time.sleep(WATCH_INTERVAL)
                continue

            # So sánh thay đổi
            for pid, new_qty in current_snapshot.items():
                old_qty = previous_snapshot.get(pid, 0)
                if new_qty == old_qty:
                    continue

                diff = new_qty - old_qty
                status = "NHẬP KHO" if diff > 0 else "XUẤT KHO"

                # Thông tin sản phẩm
                prod = models.execute_kw(
                    ODOO_DB, uid, ODOO_PASSWORD,
                    "product.product", "read",
                    [[pid]],
                    {"fields": ["display_name", PRODUCT_CODE_FIELD]}
                )[0]

                sp_code = prod.get(PRODUCT_CODE_FIELD, "???")
                sp_name = prod.get("display_name", "Không tên")

                # Lấy mã picking_id để làm mã lệnh
                move_data = models.execute_kw(
                    ODOO_DB, uid, ODOO_PASSWORD,
                    "stock.move", "search_read",
                    [[("product_id", "=", pid)]],
                    {"fields": ["picking_id"], "order": "id desc", "limit": 1}
                )

                if move_data and move_data[0]["picking_id"]:
                    picking_id = move_data[0]["picking_id"][0]
                    picking_info = models.execute_kw(
                        ODOO_DB, uid, ODOO_PASSWORD,
                        "stock.picking", "read",
                        [[picking_id]],
                        {"fields": ["name"]}
                    )
                    transaction_id = picking_info[0]["name"]
                else:
                    transaction_id = "N/A"

                # Giờ Việt Nam chuẩn
                now_vn = datetime.utcnow() + timedelta(hours=7)
                time_str = now_vn.strftime("%H:%M %d/%m/%Y")

                # Message hoàn chỉnh
                msg = (
                    f"📦 Cập nhật tồn kho 201/201 – {status}\n\n"
                    f"Mã SP: {sp_code}\n"
                    f"Tên SP: {sp_name}\n"
                    f"Biến động: {'+' if diff > 0 else ''}{diff} SP\n"
                    f"Tổng tồn sau biến động (có hàng): {new_qty} SP\n\n"
                    f"Thời gian: {time_str}\n"
                    f"Mã lệnh / ID giao dịch: {transaction_id}"
                )

                # Gửi thông báo an toàn (không dùng asyncio.run để tránh treo bot)
                bot = Bot(token=TELEGRAM_TOKEN)
                for chat_id in get_registered_chat_ids():
                    try:
                        bot.send_message(chat_id=chat_id, text=msg)
                    except Exception as e:
                        logger.error(f"Lỗi gửi thông báo tới {chat_id}: {e}")

            previous_snapshot = current_snapshot
            time.sleep(WATCH_INTERVAL)

        except Exception as e:
            logger.error(f"Lỗi watchdog: {e}")
            time.sleep(WATCH_INTERVAL)


threading.Thread(target=watchdog_201, daemon=True).start()


# ================== MAIN ==================
def main():
    if not TELEGRAM_TOKEN:
        logger.error("Thiếu TELEGRAM_TOKEN")
        return

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        asyncio.get_event_loop().run_until_complete(bot.delete_webhook())
        logger.info("đã xóa webhook cũ (nếu có).")
    except Exception as e:
        logger.warning(f"Lỗi xóa webhook: {e}")

    # Các lệnh giữ nguyên 100%
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", start_command))
    application.add_handler(CommandHandler("ping", ping_command))
    application.add_handler(CommandHandler("keohang", excel_report_command))
    application.add_handler(CommandHandler("checkpo", checkpo_command))

    # Auto xử lý file PO
    application.add_handler(MessageHandler(filters.Document.ALL, handle_po_file))

    # Tra tồn theo mã
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_product_code))

    logger.info("Bot started!")
    application.run_polling()


if __name__ == "__main__":
    main()
