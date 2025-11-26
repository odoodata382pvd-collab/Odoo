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
        common_url = f'{ODOO_URL_FINAL}/xmlrpc/2/common'
        context = ssl._create_unverified_context()
        common = xmlrpc.client.ServerProxy(common_url, context=context)
        uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD, {})
        if not uid:
            error_message = "Đăng nhập thất bại (uid=0). kiểm tra lại user/pass/db."
            return None, None, error_message
        models = xmlrpc.client.ServerProxy(f'{ODOO_URL_FINAL}/xmlrpc/2/object', context=context)
        return uid, models, "kết nối thành công."
    except Exception as e:
        return None, None, f"lỗi kết nối odoo xml-rpc: {e}"

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
        if preferred_loc:
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
    for char in special_chars:
        text = text.replace(char, f'\\{char}')
    return text

# ---------------- Report /keohang ----------------
# (GIỮ NGUYÊN — KHÔNG CHẠM VÀO)

# ---------------- Handle Product Code ----------------
# (GIỮ NGUYÊN — KHÔNG CHẠM VÀO)

# ===================================================================
# ✅ THÊM MỚI: HANDLE FILE PO EXCEL
# ===================================================================
async def handle_po_excel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.document:
        await update.message.reply_text("❌ Vui lòng gửi đúng file Excel PO.")
        return

    file = await update.message.document.get_file()
    file_bytes = await file.download_as_bytearray()

    try:
        df = pd.read_excel(io.BytesIO(file_bytes))
    except:
        await update.message.reply_text("❌ Không đọc được file. Yêu cầu file Excel đúng định dạng.")
        return

    required_cols = ['Model', 'SL', 'ĐV nhận']
    for c in required_cols:
        if c not in df.columns:
            await update.message.reply_text(f"❌ File thiếu cột bắt buộc: {c}")
            return

    df['Model'] = df['Model'].astype(str).str.upper().str.strip()
    df['SL'] = df['SL'].fillna(0).astype(int)

    # Kết nối Odoo
    uid, models, error_msg = connect_odoo()
    if not uid:
        await update.message.reply_text("❌ Không kết nối được Odoo.")
        return

    # Lấy ID kho
    location_ids = find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD)
    hn_id = location_ids.get('HN_STOCK', {}).get('id')
    hcm_id = location_ids.get('HCM_STOCK', {}).get('id')
    hn_transit_id = location_ids.get('HN_TRANSIT', {}).get('id')

    result_missing = []

    for model_code in df['Model'].unique():
        sl_required = df[df['Model'] == model_code]['SL'].sum()

        # tìm product trong Odoo
        product = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'product.product', 'search_read',
            [[(PRODUCT_CODE_FIELD, '=', model_code)]],
            {'fields': ['id', 'display_name']}
        )

        if not product:
            result_missing.append([model_code, sl_required, "Không tìm thấy sản phẩm"])
            continue

        pid = product[0]['id']

        # tồn kho từng kho
        def get_qty(pid, loc):
            if not loc: return 0
            r = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, 'product.product', 'read',
                [[pid]],
                {'fields': ['qty_available'], 'context': {'location': loc}}
            )
            return int(round(r[0].get('qty_available', 0)))

        qty_hn = get_qty(pid, hn_id)
        qty_transit = get_qty(pid, hn_transit_id)
        qty_hcm = get_qty(pid, hcm_id)

        total_hn = qty_hn + qty_transit

        if total_hn >= sl_required:
            continue  # đủ hàng → bỏ qua

        shortage = sl_required - total_hn
        import_qty = min(shortage, qty_hcm)

        result_missing.append([model_code, shortage, import_qty])

    # Xuất kết quả
    if not result_missing:
        await update.message.reply_text("✅ Tất cả Model trong PO đều ĐỦ HÀNG.")
        return

    result_df = pd.DataFrame(result_missing, columns=['Model', 'SL Thiếu', 'Đề Xuất Nhập Từ HCM'])
    buffer = io.BytesIO()
    result_df.to_excel(buffer, index=False)
    buffer.seek(0)

    await update.message.reply_document(
        buffer,
        filename="ket_qua_thieu_hang.xlsx",
        caption="❗️Danh sách model thiếu & đề xuất nhập từ HCM"
    )
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

# ---------------- Main ----------------
def main():
    if not TELEGRAM_TOKEN or not ODOO_URL_RAW or not ODOO_DB or not ODOO_USERNAME or not ODOO_PASSWORD:
        logger.error("vui lòng thiết lập tất cả các biến môi trường cần thiết (token, url, db, user, pass).")
        return

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # xóa webhook (gọi đồng bộ tránh warning)
    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        try:
            asyncio.get_event_loop().run_until_complete(bot.delete_webhook())
            logger.info("đã xóa webhook cũ (nếu có).")
        except Exception as e:
            logger.warning(f"lỗi khi xóa webhook (không ảnh hưởng): {e}")
    except Exception as e:
        logger.warning(f"lỗi khi tạo Bot object: {e}")

    # =============================
    # 🔥 Handler cũ — giữ nguyên
    # =============================
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", start_command))
    application.add_handler(CommandHandler("ping", ping_command))
    application.add_handler(CommandHandler("keohang", excel_report_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_product_code))

    # =============================
    # 🔥 THÊM MỚI HANDLER FILE EXCEL
    # =============================
    application.add_handler(MessageHandler(filters.Document.ALL, handle_po_excel))

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
        return  # tắt log cho sạch terminal

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
