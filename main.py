import os
import logging
import pandas as pd
import asyncio
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from datetime import datetime
import xmlrpc.client

# ==========================================================
# 🔧 Cấu hình Logging
# ==========================================================
logging.basicConfig(
    format="%(asctime)s [%(levelname)s]: %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ==========================================================
# 🔧 Biến môi trường
# ==========================================================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ODOO_URL_RAW = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USERNAME = os.getenv("ODOO_USERNAME")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")
USER_ID_TO_SEND_REPORT = os.getenv("USER_ID_TO_SEND_REPORT")

# ==========================================================
# 🔧 Hàm Kết nối Odoo
# ==========================================================
def odoo_connect():
    try:
        url = ODOO_URL_RAW.rstrip('/')
        common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common")
        uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD, {})
        models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object")
        return uid, models
    except Exception as e:
        logger.error(f"Lỗi khi kết nối Odoo: {e}")
        return None, None


# ==========================================================
# 🔧 Lệnh /start
# ==========================================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bot đã sẵn sàng. Gõ mã sản phẩm để tra tồn hoặc /keohang để xuất Excel.")


# ==========================================================
# 🔧 Lệnh /ping
# ==========================================================
async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bot đang hoạt động bình thường.")


# ==========================================================
# 🔧 Lệnh /keohang – Xuất báo cáo Excel
# ==========================================================
async def excel_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Đang tạo file Excel...")

    uid, models = odoo_connect()
    if not uid:
        await update.message.reply_text("❌ Không thể kết nối Odoo.")
        return

    try:
        products = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'product.product', 'search_read',
            [[['type', '=', 'product']]],
            {'fields': ['default_code', 'name', 'qty_available', 'virtual_available', 'uom_id'], 'limit': 200}
        )
        df = pd.DataFrame(products)
        filename = f"tonkho_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        df.to_excel(filename, index=False)
        await update.message.reply_document(open(filename, "rb"))
    except Exception as e:
        logger.error(f"Lỗi khi tạo Excel: {e}")
        await update.message.reply_text("❌ Lỗi khi tạo file Excel.")


# ==========================================================
# 🔧 Xử lý tra mã sản phẩm
# ==========================================================
async def handle_product_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        product_code = update.message.text.strip().upper()
        logger.info(f"Tra mã sản phẩm: {product_code}")

        uid, models = odoo_connect()
        if not uid:
            await update.message.reply_text("❌ Không thể kết nối Odoo.")
            return

        product_data = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'product.product', 'search_read',
            [[['default_code', '=', product_code]]],
            {'fields': ['id', 'name', 'default_code']}
        )
        if not product_data:
            await update.message.reply_text("❌ Không tìm thấy sản phẩm này.")
            return

        product_id = product_data[0]['id']
        product_name = product_data[0]['name']

        # 🔸 Lấy thông tin tồn kho chi tiết
        quant_data_all = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'stock.quant', 'search_read',
            [[['product_id', '=', product_id]]],
            {'fields': ['location_id', 'quantity']}
        )

        # 🔸 Lấy danh sách các kho (để map tên)
        location_ids = list({q['location_id'][0] for q in quant_data_all})
        location_data = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'stock.location', 'read',
            [location_ids],
            {'fields': ['id', 'name', 'usage', 'complete_name']}
        )
        location_map = {l['id']: l for l in location_data}

        # ==========================================================
        # ✅ FIX CHUẨN: Tính đúng cột "Có hàng" (stock.quant.quantity)
        # ==========================================================
        stock_by_loc_id = {}
        for q in quant_data_all:
            qty = float(q.get('quantity', 0.0))
            loc_id = q['location_id'][0]
            loc_data = location_map.get(loc_id, {})
            loc_usage = loc_data.get('usage', 'internal')

            # Chỉ tính kho nội bộ, không cộng transit hoặc ảo
            if qty > 0 and loc_usage == 'internal':
                stock_by_loc_id[loc_id] = stock_by_loc_id.get(loc_id, 0.0) + qty

        total_stock = sum(stock_by_loc_id.values())

        # 🔸 Tạo báo cáo chi tiết
        lines = [f"📦 <b>{product_name}</b> ({product_code})",
                 f"Tổng có hàng: <b>{total_stock:.2f}</b>",
                 "",
                 "📍 <b>Chi tiết tồn kho:</b>"]
        for loc_id, qty in stock_by_loc_id.items():
            loc_name = location_map[loc_id]['complete_name']
            lines.append(f"- {loc_name}: {qty:.2f}")

        msg = "\n".join(lines)
        await update.message.reply_html(msg)

    except Exception as e:
        logger.error(f"Lỗi xử lý tra mã: {e}")
        await update.message.reply_text("❌ Có lỗi xảy ra khi xử lý.")


# ==========================================================
# 🔧 Hàm main()
# ==========================================================
def main():
    """
    Phiên bản main() có logging chẩn đoán để Render logs rõ ràng biến môi trường nào có/không.
    Không thay đổi logic nghiệp vụ.
    """
    missing_vars = []
    if not TELEGRAM_TOKEN:
        missing_vars.append('TELEGRAM_TOKEN')
    if not ODOO_URL_RAW:
        missing_vars.append('ODOO_URL')
    if not ODOO_DB:
        missing_vars.append('ODOO_DB')
    if not ODOO_USERNAME:
        missing_vars.append('ODOO_USERNAME')
    if not ODOO_PASSWORD:
        missing_vars.append('ODOO_PASSWORD')

    logger.info("=== Env check (ẩn giá trị nhạy cảm) ===")
    logger.info(f"TELEGRAM_TOKEN: {'OK' if TELEGRAM_TOKEN else '❌'}")
    logger.info(f"ODOO_URL: {'OK' if ODOO_URL_RAW else '❌'}")
    logger.info(f"ODOO_DB: {'OK' if ODOO_DB else '❌'}")
    logger.info(f"ODOO_USERNAME: {'OK' if ODOO_USERNAME else '❌'}")
    logger.info(f"USER_ID_TO_SEND_REPORT: {'OK' if USER_ID_TO_SEND_REPORT else '❌'}")
    logger.info("=======================================")

    if missing_vars:
        logger.error(f"❌ Thiếu biến môi trường: {missing_vars}")
        return

    try:
        application = Application.builder().token(TELEGRAM_TOKEN).build()

        bot = Bot(token=TELEGRAM_TOKEN)
        try:
            bot.delete_webhook()
            logger.info("✅ Đã xóa webhook cũ (nếu có).")
        except Exception as ex:
            logger.warning(f"⚠️ Không thể xóa webhook: {ex}")

        application.add_handler(CommandHandler("start", start_command))
        application.add_handler(CommandHandler("help", start_command))
        application.add_handler(CommandHandler("ping", ping_command))
        application.add_handler(CommandHandler("keohang", excel_report_command))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_product_code))

        logger.info("🚀 Bot khởi động ở chế độ polling (Render sẽ giữ tiến trình chạy).")
        application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

    except Exception as e:
        logger.error(f"Lỗi khởi tạo bot: {e}")
        raise


# ==========================================================
# 🔧 Chạy ứng dụng
# ==========================================================
if __name__ == "__main__":
    main()
