# main.py – Final stable version (no errors, no algorithm changes)
import os
import io
import logging
import pandas as pd
import ssl
import xmlrpc.client
import asyncio
import socket
import threading
from telegram import Update, Bot, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from http.server import BaseHTTPRequestHandler, HTTPServer

# ================= CONFIG =================
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
PRODUCT_CODE_FIELD = "default_code"

LOCATION_MAP = {
    "HN_STOCK_CODE": "201/201",
    "HCM_STOCK_CODE": "124/124",
    "HN_TRANSIT_NAME": "Kho nhập Hà Nội",
}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ================= KEEP PORT OPEN =================
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

# ================= ODOO CONNECT =================
def connect_odoo():
    try:
        if not ODOO_URL_FINAL:
            return None, None, "Thiếu ODOO_URL"
        common = xmlrpc.client.ServerProxy(
            f"{ODOO_URL_FINAL}/xmlrpc/2/common",
            context=ssl._create_unverified_context()
        )
        uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD, {})
        if not uid:
            return None, None, "Sai user / password / db"
        models = xmlrpc.client.ServerProxy(
            f"{ODOO_URL_FINAL}/xmlrpc/2/object",
            context=ssl._create_unverified_context()
        )
        return uid, models, "OK"
    except Exception as e:
        return None, None, str(e)

# ================= HELPERS =================
def find_required_location_ids(models, uid):
    loc_ids = {}

    def find(name):
        data = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, "stock.location", "search_read",
            [[("display_name", "ilike", name)]],
            {"fields": ["id", "display_name"]}
        )
        return data[0] if data else None

    loc_ids["HN_STOCK"] = find(LOCATION_MAP["HN_STOCK_CODE"])
    loc_ids["HCM_STOCK"] = find(LOCATION_MAP["HCM_STOCK_CODE"])
    loc_ids["HN_TRANSIT"] = find(LOCATION_MAP["HN_TRANSIT_NAME"])
    return loc_ids

# ================= /keohang (giữ nguyên) =================
def get_stock_data():
    uid, models, err = connect_odoo()
    if not uid:
        return None, 0, err

    try:
        loc = find_required_location_ids(models, uid)
        if not all(loc.values()):
            return None, 0, "Không tìm đủ 3 kho"

        ids = [loc[k]["id"] for k in loc]
        quants = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, "stock.quant", "search_read",
            [[("location_id", "in", ids), ("quantity", ">", 0)]],
            {"fields": ["product_id", "location_id", "quantity"]},
        )

        prod_ids = list({q["product_id"][0] for q in quants})
        prod_info = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, "product.product", "search_read",
            [[("id", "in", prod_ids)]],
            {"fields": ["display_name", PRODUCT_CODE_FIELD]},
        )
        prod_map = {p["id"]: p for p in prod_info}

        data = {}

        for q in quants:
            pid = q["product_id"][0]
            loc_id = q["location_id"][0]
            qty = float(q["quantity"])

            if pid not in data:
                data[pid] = {
                    "Mã SP": prod_map[pid].get(PRODUCT_CODE_FIELD, ""),
                    "Tên SP": prod_map[pid]["display_name"],
                    "Tồn Kho HN": 0,
                    "Tồn Kho HCM": 0,
                    "Kho Nhập HN": 0,
                    "Số Lượng Đề Xuất": 0,
                }

            if loc_id == loc["HN_STOCK"]["id"]:
                data[pid]["Tồn Kho HN"] += qty
            elif loc_id == loc["HCM_STOCK"]["id"]:
                data[pid]["Tồn Kho HCM"] += qty
            elif loc_id == loc["HN_TRANSIT"]["id"]:
                data[pid]["Kho Nhập HN"] += qty

        output = []

        for pid, v in data.items():
            total_hn = v["Tồn Kho HN"] + v["Kho Nhập HN"]
            if total_hn < TARGET_MIN_QTY:
                need = TARGET_MIN_QTY - total_hn
                suggest = min(need, v["Tồn Kho HCM"])
                if suggest > 0:
                    v["Số Lượng Đề Xuất"] = suggest
                    output.append(v)

        df = pd.DataFrame(output)
        buf = io.BytesIO()
        df.to_excel(buf, index=False)
        buf.seek(0)
        return buf, len(output), "OK"

    except Exception as e:
        return None, 0, str(e)

# ================= HANDLE PRODUCT CODE (giữ nguyên) =================
async def handle_product_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip().upper()
    await update.message.reply_text(f"Đang tra tồn mã {code}...")

    uid, models, err = connect_odoo()
    if not uid:
        await update.message.reply_text(f"Lỗi: {err}")
        return

    try:
        loc = find_required_location_ids(models, uid)
        hn = loc["HN_STOCK"]["id"]
        nhap = loc["HN_TRANSIT"]["id"]
        hcm = loc["HCM_STOCK"]["id"]

        prod = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, "product.product", "search_read",
            [[(PRODUCT_CODE_FIELD, "=", code)]],
            {"fields": ["id", "display_name"]},
        )
        if not prod:
            await update.message.reply_text("Không tìm thấy sản phẩm.")
            return

        pid = prod[0]["id"]
        name = prod[0]["display_name"]

        def q(loc_id):
            d = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, "product.product", "read",
                [[pid]],
                {"fields": ["qty_available"], "context": {"location": loc_id}},
            )
            return int(d[0]["qty_available"]) if d else 0

        q_hn, q_nhap, q_hcm = q(hn), q(nhap), q(hcm)

        msg = (
            f"{code} {name}\n"
            f"Tồn kho HN: {q_hn}\n"
            f"Tồn kho nhập HN: {q_nhap}\n"
            f"Tồn kho HCM: {q_hcm}\n"
        )

        await update.message.reply_text(msg)

    except Exception as e:
        await update.message.reply_text(f"Lỗi: {e}")

# ================= NEW FEATURE /checkexcel =================
async def checkexcel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["waiting_for_excel"] = True
    await update.message.reply_text("Gửi file Excel (.xlsx) để kiểm tra tồn.")

async def excel_file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("waiting_for_excel"):
        return

    context.user_data["waiting_for_excel"] = False

    doc = update.message.document
    if not doc.file_name.lower().endswith(".xlsx"):
        await update.message.reply_text("❌ File phải là .xlsx")
        return

    file = await doc.get_file()
    df = pd.read_excel(io.BytesIO(await file.download_as_bytearray()))

    required = ["Model", "SL", "ĐV nhận"]
    for c in required:
        if c not in df.columns:
            await update.message.reply_text(f"Thiếu cột: {c}")
            return

    uid, models, err = connect_odoo()
    if not uid:
        await update.message.reply_text(f"Lỗi Odoo: {err}")
        return

    loc = find_required_location_ids(models, uid)
    hn, nhap, hcm = loc["HN_STOCK"]["id"], loc["HN_TRANSIT"]["id"], loc["HCM_STOCK"]["id"]

    results = []

    for _, r in df.iterrows():
        model = str(r["Model"]).strip()
        sl_req = int(r["SL"])

        prod = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, "product.product", "search_read",
            [[(PRODUCT_CODE_FIELD, "=", model)]],
            {"fields": ["id"]},
        )
        if not prod:
            results.append({
                "Model": model,
                "SL yêu cầu": sl_req,
                "Trạng thái": "Không tìm thấy",
                "Đề xuất nhập": 0,
                "ĐV nhận": r["ĐV nhận"]
            })
            continue

        pid = prod[0]["id"]

        def q(loc_id):
            d = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, "product.product", "read",
                [[pid]],
                {"fields": ["qty_available"], "context": {"location": loc_id}},
            )
            return int(d[0]["qty_available"]) if d else 0

        q_hn, q_nhap, q_hcm = q(hn), q(nhap), q(hcm)
        total_hn = q_hn + q_nhap

        if total_hn >= sl_req:
            status = "Đủ"
            suggest = 0
        else:
            need = sl_req - total_hn
            suggest = min(need, q_hcm)
            status = f"Thiếu {need}"

        results.append({
            "Model": model,
            "SL yêu cầu": sl_req,
            "Tồn HN(HN+Nhập)": total_hn,
            "Tồn HCM": q_hcm,
            "Trạng thái": status,
            "Đề xuất nhập HCM": suggest,
            "ĐV nhận": r["ĐV nhận"],
        })

    out = pd.DataFrame(results)
    buf = io.BytesIO()
    out.to_excel(buf, index=False)
    buf.seek(0)

    await update.message.reply_document(
        InputFile(buf, "ket_qua_kiem_tra_ton.xlsx"),
        caption="✔ Hoàn tất kiểm tra tồn."
    )

# ================= REPORT =================
async def excel_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Đang tạo báo cáo đề xuất…")

    buffer, n, msg = get_stock_data()

    if buffer is None:
        await update.message.reply_text(f"❌ Lỗi: {msg}")
        return

    if n > 0:
        await update.message.reply_document(
            document=buffer,
            filename="de_xuat_keo_hang.xlsx",
            caption=f"Có {n} sản phẩm cần kéo hàng."
        )
    else:
        await update.message.reply_text("Không có sản phẩm cần kéo hàng.")

# ================= START / PING =================
async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid, _, err = connect_odoo()
    await update.message.reply_text("Kết nối OK" if uid else f"Lỗi: {err}")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Bot sẵn sàng.\n"
        "- /checkexcel để kiểm tra tồn file Excel\n"
        "- /keohang để xuất báo cáo\n"
        "- Gõ mã SP để tra tồn"
    )

# ================= MAIN =================
def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Xóa webhook
    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        asyncio.get_event_loop().run_until_complete(bot.delete_webhook())
    except:
        pass

    # Command handlers
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", start_command))
    app.add_handler(CommandHandler("ping", ping_command))
    app.add_handler(CommandHandler("keohang", excel_report_command))

    # Excel mode BEFORE text handler
    app.add_handler(CommandHandler("checkexcel", checkexcel_command))
    app.add_handler(MessageHandler(filters.Document.ALL & ~filters.TEXT, excel_file_handler))

    # Text handler (mã SP)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_product_code))

    logger.info("Bot running…")
    app.run_polling()

# ================= HTTP KEEPALIVE =================
class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

def start_http_server():
    try:
        server = HTTPServer(("0.0.0.0", 10001), PingHandler)
        server.serve_forever()
    except:
        pass

threading.Thread(target=start_http_server, daemon=True).start()

if __name__ == "__main__":
    main()
