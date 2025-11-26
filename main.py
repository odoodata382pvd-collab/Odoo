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

# ---------------- Keep Render Alive ----------------
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

# ---------------- Odoo Connect ----------------
def connect_odoo():
    try:
        if not ODOO_URL_FINAL:
            return None, None, "odoo url không được thiết lập."

        common_url = f"{ODOO_URL_FINAL}/xmlrpc/2/common"
        context = ssl._create_unverified_context()
        common = xmlrpc.client.ServerProxy(common_url, context=context)

        uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD, {})
        if not uid:
            return None, None, "Đăng nhập thất bại."

        models = xmlrpc.client.ServerProxy(f"{ODOO_URL_FINAL}/xmlrpc/2/object", context=context)
        return uid, models, "kết nối thành công."
    except Exception as e:
        return None, None, str(e)

# ---------------- Helpers ----------------
def find_required_location_ids(models, uid, db, password):
    locs = {}

    def search_location(name_code):
        rec = models.execute_kw(
            db, uid, password, 'stock.location', 'search_read',
            [[('display_name', 'ilike', name_code)]],
            {'fields': ['id', 'display_name', 'complete_name']}
        )
        if not rec:
            return None
        return {'id': rec[0]['id'], 'name': rec[0].get('display_name')}

    hn = search_location(LOCATION_MAP['HN_STOCK_CODE'])
    hcm = search_location(LOCATION_MAP['HCM_STOCK_CODE'])
    transit = search_location(LOCATION_MAP['HN_TRANSIT_NAME'])

    if hn: locs['HN_STOCK'] = hn
    if hcm: locs['HCM_STOCK'] = hcm
    if transit: locs['HN_TRANSIT'] = transit

    return locs

# ---------------- /keohang Report ----------------
def get_stock_data():
    uid, models, msg = connect_odoo()
    if not uid:
        return None, 0, msg

    try:
        loc_ids = find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD)
        if len(loc_ids) < 3:
            return None, 0, "Không tìm đủ kho."

        all_loc_ids = [loc_ids[k]['id'] for k in loc_ids]
        quant_data = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'stock.quant', 'search_read',
            [[('location_id', 'in', all_loc_ids), ('quantity', '>', 0)]],
            {'fields': ['product_id', 'location_id', 'quantity']}
        )

        product_ids = list({q['product_id'][0] for q in quant_data})
        product_info = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'product.product', 'search_read',
            [[('id', 'in', product_ids)]],
            {'fields': ['display_name', PRODUCT_CODE_FIELD]}
        )
        product_map = {p['id']: p for p in product_info}

        data = {}
        for q in quant_data:
            pid = q['product_id'][0]
            loc = q['location_id'][0]
            qty = float(q['quantity'])

            if pid not in data:
                data[pid] = {
                    "Mã SP": product_map[pid].get(PRODUCT_CODE_FIELD, ''),
                    "Tên SP": product_map[pid]['display_name'],
                    "Tồn Kho HN": 0,
                    "Tồn Kho HCM": 0,
                    "Kho Nhập HN": 0,
                }

            if loc == loc_ids['HN_STOCK']['id']:
                data[pid]["Tồn Kho HN"] += qty
            elif loc == loc_ids['HCM_STOCK']['id']:
                data[pid]["Tồn Kho HCM"] += qty
            elif loc == loc_ids['HN_TRANSIT']['id']:
                data[pid]["Kho Nhập HN"] += qty

        df_out = []
        for pid, row in data.items():
            tonghn = row["Tồn Kho HN"] + row["Kho Nhập HN"]
            if tonghn < TARGET_MIN_QTY:
                need = TARGET_MIN_QTY - tonghn
                de_xuat = min(need, row["Tồn Kho HCM"])
                if de_xuat > 0:
                    row["Số Lượng Đề Xuất"] = int(de_xuat)
                    df_out.append(row)

        df = pd.DataFrame(df_out)

        buffer = io.BytesIO()
        df.to_excel(buffer, index=False)
        buffer.seek(0)
        return buffer, len(df), "OK"

    except Exception as e:
        return None, 0, str(e)

# ---------------- Handle Product Code ----------------
async def handle_product_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip().upper()
    await update.message.reply_text(f"đang tra tồn cho `{code}`, chờ xíu...", parse_mode="Markdown")

    uid, models, msg = connect_odoo()
    if not uid:
        await update.message.reply_text("❌ Không kết nối Odoo.")
        return

    try:
        locs = find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD)
        hn = locs['HN_STOCK']['id']
        hcm = locs['HCM_STOCK']['id']
        transit = locs['HN_TRANSIT']['id']

        prod = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'product.product', 'search_read',
            [[(PRODUCT_CODE_FIELD, '=', code)]],
            {'fields': ['id', 'display_name']}
        )
        if not prod:
            await update.message.reply_text("❌ Không tìm thấy mã sản phẩm.")
            return

        pid = prod[0]['id']
        pname = prod[0]['display_name']

        def qty(loc):
            r = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, 'product.product', 'read',
                [[pid]],
                {'fields': ['qty_available'], 'context': {'location': loc}}
            )
            return int(round(r[0]['qty_available']))

        q_hn = qty(hn)
        q_tran = qty(transit)
        q_hcm = qty(hcm)

        tonghn = q_hn + q_tran
        need = 0
        if tonghn < TARGET_MIN_QTY:
            need = min(TARGET_MIN_QTY - tonghn, q_hcm)

        text = f"""{code} {pname}
Tồn kho HN: {q_hn}
Tồn kho HCM: {q_hcm}
Tồn kho nhập Hà Nội: {q_tran}
Đề xuất nhập thêm: {need}
"""

        await update.message.reply_text(text)

    except Exception as e:
        await update.message.reply_text(str(e))

# ---------------- Handle Excel PO Upload ----------------
async def handle_po_excel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.document:
        await update.message.reply_text("❌ Vui lòng gửi file Excel.")
        return

    file = await update.message.document.get_file()
    content = await file.download_as_bytearray()

    try:
        df = pd.read_excel(io.BytesIO(content))
    except:
        await update.message.reply_text("❌ File Excel không hợp lệ.")
        return

    required = ['Model', 'SL', 'ĐV nhận']
    for c in required:
        if c not in df.columns:
            await update.message.reply_text(f"❌ Thiếu cột: {c}")
            return

    df['Model'] = df['Model'].astype(str).str.upper().str.strip()
    df['SL'] = df['SL'].fillna(0).astype(int)

    uid, models, msg = connect_odoo()
    if not uid:
        await update.message.reply_text("❌ Không kết nối Odoo.")
        return

    locs = find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD)
    hn = locs['HN_STOCK']['id']
    hcm = locs['HCM_STOCK']['id']
    transit = locs['HN_TRANSIT']['id']

    results = []

    for m in df['Model'].unique():
        total_required = df[df['Model'] == m]['SL'].sum()

        prod = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'product.product', 'search_read',
            [[(PRODUCT_CODE_FIELD, '=', m)]],
            {'fields': ['id']}
        )
        if not prod:
            results.append([m, total_required, "Không tìm thấy"])
            continue

        pid = prod[0]['id']

        def qty(loc):
            rec = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, 'product.product', 'read',
                [[pid]],
                {'fields': ['qty_available'], 'context': {'location': loc}}
            )
            return int(round(rec[0]['qty_available']))

        q_hn = qty(hn) + qty(transit)
        q_hcm = qty(hcm)

        if q_hn >= total_required:
            continue

        shortage = total_required - q_hn
        propose = min(shortage, q_hcm)
        results.append([m, shortage, propose])

    if not results:
        await update.message.reply_text("✅ Tất cả Model trong PO đều đủ hàng.")
        return

    df_out = pd.DataFrame(results, columns=['Model', 'SL Thiếu', 'Đề Xuất Nhập Từ HCM'])

    buf = io.BytesIO()
    df_out.to_excel(buf, index=False)
    buf.seek(0)

    await update.message.reply_document(
        buf,
        filename="ket_qua_thieu_hang.xlsx",
        caption="❗️Danh sách model thiếu"
    )

# ---------------- Telegram Handlers ----------------
async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Đang kiểm tra kết nối odoo...")
    uid, _, msg = connect_odoo()
    if uid:
        await update.message.reply_text("✅ Kết nối Odoo thành công.")
    else:
        await update.message.reply_text(f"❌ Lỗi: {msg}")

async def excel_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⌛️ Đang tạo báo cáo...")
    buf, count, msg = get_stock_data()
    if buf is None:
        await update.message.reply_text(f"❌ Lỗi: {msg}")
        return

    if count > 0:
        await update.message.reply_document(
            buf,
            filename="de_xuat_keo_hang.xlsx",
            caption=f"Đã tìm thấy {count} sản phẩm cần kéo hàng."
        )
    else:
        await update.message.reply_text("Không có sản phẩm nào cần kéo hàng.")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.from_user.first_name
    await update.message.reply_text(
        f"Chào {name}!\n"
        "Gõ mã sản phẩm để tra tồn.\n"
        "/keohang để tạo báo cáo.\n"
        "Gửi file Excel PO để kiểm tra hàng."
    )

# ---------------- Main ----------------
def main():
    if not TELEGRAM_TOKEN:
        logger.error("Thiếu TOKEN")
        return

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # remove webhook
    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        asyncio.get_event_loop().run_until_complete(bot.delete_webhook())
    except:
        pass

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", start_command))
    app.add_handler(CommandHandler("ping", ping_command))
    app.add_handler(CommandHandler("keohang", excel_report_command))

    # TEXT → tra mã sản phẩm
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_product_code))

    # FILE EXCEL → xử lý PO
    app.add_handler(MessageHandler(filters.Document.ALL, handle_po_excel))

    logger.info("Bot đang chạy…")
    app.run_polling()

# ---------------- HTTP Server (Render keep alive) ----------------
class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

    def log_message(self, *a):
        return

def start_http():
    try:
        server = HTTPServer(("0.0.0.0", 10001), PingHandler)
        server.serve_forever()
    except Exception as e:
        logger.error(e)

threading.Thread(target=start_http, daemon=True).start()

# ---------------- RUN ----------------
if __name__ == "__main__":
    main()
