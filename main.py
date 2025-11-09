import os
import logging
import xmlrpc.client
from telegram import Update, Bot
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
import pandas as pd
import socket
import threading

# ------------------ Logging ------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s]: %(message)s")

# ------------------ Environment Variables ------------------
ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USERNAME = os.getenv("ODOO_USERNAME")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
USER_ID_TO_SEND_REPORT = int(os.getenv("USER_ID_TO_SEND_REPORT", "0"))

# ------------------ Keep port open for Render Free Plan ------------------
def keep_port_open():
    s = socket.socket()
    s.bind(("0.0.0.0", 10000))
    s.listen(1)
    while True:
        conn, _ = s.accept()
        conn.close()

threading.Thread(target=keep_port_open, daemon=True).start()

# ------------------ Kết nối Odoo ------------------
def odoo_connect():
    try:
        common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
        uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD, {})
        models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
        logging.info("✅ Kết nối Odoo thành công.")
        return uid, models
    except Exception as e:
        logging.error(f"Lỗi khi kết nối Odoo: {e}")
        return None, None

# ------------------ Hàm xử lý mã sản phẩm ------------------
async def handle_product_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    product_code = update.message.text.strip()
    logging.info(f"Tra mã sản phẩm: {product_code}")

    uid, models = odoo_connect()
    if not uid:
        await update.message.reply_text("❌ Không thể kết nối đến Odoo. Vui lòng kiểm tra cấu hình.")
        return

    try:
        # Tìm sản phẩm
        product_ids = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, "product.product", "search",
            [[["default_code", "=", product_code]]]
        )
        if not product_ids:
            await update.message.reply_text(f"❌ Không tìm thấy sản phẩm với mã: {product_code}")
            return

        product_data = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, "product.product", "read",
            [product_ids], {"fields": ["name", "default_code"]}
        )[0]

        product_name = product_data["name"]
        product_display = f"{product_data['default_code']} {product_name}"

        # ------------------ Tồn kho tổng hợp ------------------
        quant_domain = [["product_id", "in", product_ids]]
        quants = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, "stock.quant", "read_group",
            [quant_domain, ["quantity", "location_id"], ["location_id"]],
            {"lazy": False}
        )

        total_by_location = {}
        for q in quants:
            loc_name = q["location_id"][1] if q["location_id"] else "Chưa rõ"
            total_by_location[loc_name] = q["quantity"]

        # ------------------ Tồn kho chi tiết (Có hàng) ------------------
        quant_domain_all = [["product_id", "in", product_ids]]

        # ✅ ĐÃ SỬA 1️⃣ — dùng available_quantity thay vì quantity
        quant_data_all = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, "stock.quant", "search_read",
            [quant_domain_all],
            {"fields": ["location_id", "available_quantity"]}
        )

        # ✅ ĐÃ SỬA 2️⃣ — cộng dồn theo available_quantity
        stock_by_loc_id = {}
        for q in quant_data_all:
            loc = q["location_id"][1] if q["location_id"] else "Chưa rõ vị trí"
            qty = float(q.get("available_quantity", 0.0))
            if loc not in stock_by_loc_id:
                stock_by_loc_id[loc] = 0.0
            stock_by_loc_id[loc] += qty

        # ------------------ Format kết quả ------------------
        stock_detail = "\n".join(
            [f"{loc}: {qty:.0f}" for loc, qty in stock_by_loc_id.items()]
        )

        msg = (
            f"{product_display}\n"
            f"Tồn kho chi tiết (có hàng):\n{stock_detail}"
        )

        await update.message.reply_text(msg)

    except Exception as e:
        logging.error(f"Lỗi xử lý: {e}")
        await update.message.reply_text("❌ Đã xảy ra lỗi khi lấy dữ liệu từ Odoo.")

# ------------------ Lệnh /start ------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👋 Nhập mã sản phẩm để tra tồn kho Odoo (VD: I-78)")

# ------------------ Main ------------------
if __name__ == "__main__":
    logging.info("=== Env check (ẩn giá trị nhạy cảm) ===")
    for k in ["TELEGRAM_TOKEN", "ODOO_URL", "ODOO_DB", "ODOO_USERNAME", "USER_ID_TO_SEND_REPORT"]:
        logging.info(f"{k}: {'OK' if os.getenv(k) else 'MISSING'}")
    logging.info("=======================================")

    bot = Bot(token=TELEGRAM_TOKEN)
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_product_code))

    logging.info("🚀 Bot khởi động ở chế độ polling.")
    app.run_polling()
