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
from telegram import Update, Bot, InputFile
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
            return None, None, "Đăng nhập thất bại."
        models = xmlrpc.client.ServerProxy(f'{ODOO_URL_FINAL}/xmlrpc/2/object', context=context)
        return uid, models, "ok"
    except Exception as e:
        return None, None, str(e)

# ---------------- Helpers ----------------
def find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD):
    location_ids = {}
    def search_location(name_code):
        loc_data = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'stock.location', 'search_read',
            [[('display_name', 'ilike', name_code)]],
            {'fields': ['id', 'display_name', 'complete_name']}
        )
        if not loc_data: return None
        preferred = next((l for l in loc_data if name_code.lower() in l['display_name'].lower()), loc_data[0])
        return {'id': preferred['id'], 'name': preferred.get('display_name')}

    hn_stock = search_location(LOCATION_MAP['HN_STOCK_CODE'])
    if hn_stock: location_ids['HN_STOCK'] = hn_stock
    hcm_stock = search_location(LOCATION_MAP['HCM_STOCK_CODE'])
    if hcm_stock: location_ids['HCM_STOCK'] = hcm_stock
    hn_transit = search_location(LOCATION_MAP['HN_TRANSIT_NAME'])
    if hn_transit: location_ids['HN_TRANSIT'] = hn_transit
    return location_ids

def escape_markdown(text):
    for c in ['\\', '_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']:
        text = str(text).replace(c, f"\\{c}")
    return text.replace('\\`', '`')

# ---------------- Report /keohang ----------------
def get_stock_data():
    uid, models, msg = connect_odoo()
    if not uid: return None, 0, msg
    try:
        locs = find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD)
        if len(locs) < 3:
            return None, 0, "thiếu kho"

        all_ids = [v['id'] for v in locs.values()]
        quant = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'stock.quant', 'search_read',
            [[('location_id', 'in', all_ids), ('quantity', '>', 0)]],
            {'fields': ['product_id', 'location_id', 'quantity']}
        )

        product_ids = list(set([q['product_id'][0] for q in quant]))
        info = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'product.product', 'search_read',
            [[('id', 'in', product_ids)]],
            {'fields': ['display_name', PRODUCT_CODE_FIELD]}
        )
        pmap = {p['id']: p for p in info}

        data = {}
        for q in quant:
            pid = q['product_id'][0]
            loc = q['location_id'][0]
            qty = float(q['quantity'])

            if pid not in data:
                data[pid] = {
                    'Mã SP': pmap[pid].get(PRODUCT_CODE_FIELD),
                    'Tên SP': pmap[pid]['display_name'],
                    'Tồn Kho HN': 0, 'Tồn Kho HCM': 0, 'Kho Nhập HN': 0,
                    'Tổng Tồn HN': 0, 'Số Lượng Đề Xuất': 0
                }

            if loc == locs['HN_STOCK']['id']: data[pid]['Tồn Kho HN'] += qty
            elif loc == locs['HCM_STOCK']['id']: data[pid]['Tồn Kho HCM'] += qty
            elif loc == locs['HN_TRANSIT']['id']: data[pid]['Kho Nhập HN'] += qty

        out = []
        for pid, v in data.items():
            v['Tổng Tồn HN'] = v['Tồn Kho HN'] + v['Kho Nhập HN']
            if v['Tổng Tồn HN'] < TARGET_MIN_QTY:
                need = TARGET_MIN_QTY - v['Tổng Tồn HN']
                v['Số Lượng Đề Xuất'] = min(need, v['Tồn Kho HCM'])
                if v['Số Lượng Đề Xuất'] > 0:
                    out.append(v)

        df = pd.DataFrame(out)
        buf = io.BytesIO()
        df.to_excel(buf, index=False)
        buf.seek(0)
        return buf, len(out), "ok"
    except Exception as e:
        return None, 0, str(e)

# ---------------- Handle product code ----------------
async def handle_product_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    product_code = update.message.text.strip().upper()
    await update.message.reply_text(f"đang tra tồn cho `{product_code}`, vui lòng chờ!", parse_mode='Markdown')

    uid, models, msg = connect_odoo()
    if not uid:
        await update.message.reply_text(f"❌ lỗi: `{escape_markdown(msg)}`", parse_mode='Markdown')
        return

    try:
        loc = find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD)
        hn_stock = loc['HN_STOCK']['id']
        hcm_stock = loc['HCM_STOCK']['id']
        hn_transit = loc['HN_TRANSIT']['id']

        prod = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, "product.product", "search_read",
            [[(PRODUCT_CODE_FIELD, '=', product_code)]],
            {"fields": ["display_name", "id"]}
        )
        if not prod:
            await update.message.reply_text("❌ Không tìm thấy SP")
            return

        pid = prod[0]['id']
        name = prod[0]['display_name']

        def g(locid):
            d = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, "product.product", "read",
                [[pid]],
                {'fields': ['qty_available'], 'context': {'location': locid}}
            )
            return int(d[0]['qty_available']) if d else 0

        hn = g(hn_stock)
        hnt = g(hn_transit)
        hcm = g(hcm_stock)

        total = hn + hnt
        rec = 0
        if total < TARGET_MIN_QTY:
            need = TARGET_MIN_QTY - total
            rec = min(need, hcm)

        msg = f"""{product_code} {name}
Tồn kho HN: {hn}
Tồn kho HCM: {hcm}
Tồn kho nhập HN: {hnt}
Đề xuất: {rec}
"""
        await update.message.reply_text(msg.strip())

    except Exception as e:
        await update.message.reply_text(f"❌ lỗi: {e}")

# ---------------- NEW: /checkexcel ----------------
async def checkexcel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["waiting_for_excel"] = True
    await update.message.reply_text("📄 Gửi file Excel (.xlsx) để iem kiểm tra tồn theo Model + SL.")

# ---------------- NEW: Excel handler ----------------
async def excel_file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("waiting_for_excel"):
        return

    context.user_data["waiting_for_excel"] = False

    doc = update.message.document
    if not doc.file_name.lower().endswith(".xlsx"):
        await update.message.reply_text("❌ File phải là .xlsx")
        return

    f = await doc.get_file()
    df = pd.read_excel(io.BytesIO(await f.download_as_bytearray()))

    required = ["Model", "SL", "ĐV nhận"]
    for c in required:
        if c not in df.columns:
            await update.message.reply_text(f"❌ Thiếu cột: {c}")
            return

    uid, models, msg = connect_odoo()
    if not uid:
        await update.message.reply_text(f"❌ Lỗi Odoo: {msg}")
        return

    loc = find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD)
    hn = loc['HN_STOCK']['id']
    nhap = loc['HN_TRANSIT']['id']
    hcm = loc['HCM_STOCK']['id']

    results = []
    for _, r in df.iterrows():
        model = str(r["Model"]).strip()
        sl = int(r["SL"])

        prod = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, "product.product", "search_read",
            [[("default_code", "=", model)]],
            {"fields": ["id"]}
        )

        if not prod:
            results.append({
                "Model": model,
                "SL yêu cầu": sl,
                "Trạng thái": "Không tìm thấy",
                "Đề xuất HCM": 0,
                "ĐV nhận": r["ĐV nhận"]
            })
            continue

        pid = prod[0]['id']

        def qty(locid):
            d = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, "product.product", "read",
                [[pid]],
                {"fields": ["qty_available"], "context": {"location": locid}}
            )
            return int(d[0]["qty_available"]) if d else 0

        q_hn = qty(hn)
        q_np = qty(nhap)
        q_hcm = qty(hcm)
        total = q_hn + q_np

        if total >= sl:
            status = "Đủ"
            suggest = 0
        else:
            need = sl - total
            suggest = min(need, q_hcm)
            status = f"Thiếu {need}"

        results.append({
            "Model": model,
            "SL yêu cầu": sl,
            "Tồn HN(HN+Nhập)": total,
            "Tồn HCM": q_hcm,
            "Trạng thái": status,
            "Đề xuất HCM": suggest,
            "ĐV nhận": r["ĐV nhận"]
        })

    out = pd.DataFrame(results)
    buf = io.BytesIO()
    out.to_excel(buf, index=False)
    buf.seek(0)

    await update.message.reply_document(
        document=InputFile(buf, filename="kiem_tra_ton.xlsx"),
        caption="✔ Hoàn tất kiểm tra tồn file Excel!"
    )

# ---------------- Telegram Commands ----------------
async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Đang kiểm tra kết nối odoo...")
    uid, _, msg = connect_odoo()
    if uid:
        await update.message.reply_text("OK")
    else:
        await update.message.reply_text(f"Lỗi: {msg}")

async def excel_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Đang tạo file...")
    buf, count, msg = get_stock_data()
    if buf is None:
        await update.message.reply_text(f"Lỗi: {msg}")
        return
    if count > 0:
        await update.message.reply_document(document=buf, filename="de_xuat.xlsx")
    else:
        await update.message.reply_text("Tồn kho đủ.")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Bot sẵn sàng.")

# ---------------- Main ----------------
def main():
    if not TELEGRAM_TOKEN:
        logger.error("Thiếu biến môi trường.")
        return

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        asyncio.get_event_loop().run_until_complete(bot.delete_webhook())
    except:
        pass

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", start_command))
    app.add_handler(CommandHandler("ping", ping_command))
    app.add_handler(CommandHandler("keohang", excel_report_command))
    app.add_handler(CommandHandler("checkexcel", checkexcel_command))

    app.add_handler(MessageHandler(filters.Document.ALL, excel_file_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_product_code))

    logger.info("Bot running...")
    app.run_polling()

# ---------------- HTTP server keep alive ----------------
from http.server import BaseHTTPRequestHandler, HTTPServer

class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type","text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

    def log_message(self, format, *args):
        return

def start_http_server():
    try:
        server = HTTPServer(("0.0.0.0", 10001), PingHandler)
        server.serve_forever()
    except:
        pass

threading.Thread(target=start_http_server, daemon=True).start()

# ---------------- Run Main ----------------
if __name__ == "__main__":
    main()
