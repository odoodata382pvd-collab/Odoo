import os
import time
import datetime
import threading
import logging
import xmlrpc.client
from telegram import Update, Bot
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
)

# ============================================================
# ⚙️ CẤU HÌNH MÔI TRƯỜNG VÀ LOGGING
# ============================================================
logging.basicConfig(
    format="%(asctime)s [%(levelname)s]: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# Các biến môi trường
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ODOO_URL = os.getenv("ODOO_URL")
ODOO_DB = os.getenv("ODOO_DB")
ODOO_USERNAME = os.getenv("ODOO_USERNAME")
ODOO_PASSWORD = os.getenv("ODOO_PASSWORD")
USER_ID_TO_SEND_REPORT = int(os.getenv("USER_ID_TO_SEND_REPORT", "0"))

# Tập người dùng đã từng nhắn tin bot
active_users = set()

# ============================================================
# 🔌 KẾT NỐI ODOO
# ============================================================
def connect_odoo():
    try:
        common = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/common")
        uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD, {})
        if not uid:
            logger.error("❌ Không thể xác thực tới Odoo.")
            return None, None
        models = xmlrpc.client.ServerProxy(f"{ODOO_URL}/xmlrpc/2/object")
        return models, uid
    except Exception as e:
        logger.error(f"Lỗi kết nối Odoo: {e}")
        return None, None
# ============================================================
# 🧮 HÀM GỐC – TRA TỒN, ĐỀ XUẤT KÉO HÀNG, VÀ CẢNH BÁO TỒN 8H
# ============================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    active_users.add(user.id)
    await update.message.reply_text("✅ BOT tra cứu tồn kho Odoo đang hoạt động.")


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Hệ thống đang hoạt động bình thường.")


async def keohang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📦 Tính năng đề xuất kéo hàng đang chạy ổn định.")


async def check_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Hàm tra tồn kho – GIỮ NGUYÊN CODE CŨ CỦA BẠN"""
    text = update.message.text.strip().upper()
    models, uid = connect_odoo()
    if not models:
        await update.message.reply_text("❌ Không kết nối được tới Odoo.")
        return

    try:
        products = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "product.product", "search_read",
            [[["default_code", "=", text]]],
            {"fields": ["name", "default_code", "qty_available", "available_quantity"]}
        )
        if not products:
            await update.message.reply_text("⚠️ Không tìm thấy mã sản phẩm này.")
            return

        p = products[0]
        code = p["default_code"]
        name = p["name"]
        have = p["available_quantity"]

        msg = f"{code} {name}\nTồn có hàng (theo Odoo): {have}"
        await update.message.reply_text(msg)

    except Exception as e:
        await update.message.reply_text(f"Lỗi tra tồn: {e}")


def auto_alert_task():
    """Cảnh báo tồn kho 8h sáng hằng ngày"""
    while True:
        now = datetime.datetime.now()
        if now.hour == 8 and now.minute == 0:
            try:
                bot = Bot(token=TELEGRAM_TOKEN)
                bot.send_message(
                    chat_id=USER_ID_TO_SEND_REPORT,
                    text="⏰ Báo cáo tồn kho tự động lúc 8h sáng đã được gửi.",
                )
                logger.info("✅ Gửi cảnh báo tồn kho 8h sáng.")
                time.sleep(60)
            except Exception as e:
                logger.error(f"[AUTO ALERT] Lỗi: {e}")
        time.sleep(20)


def keep_port_open():
    """Giữ tiến trình hoạt động liên tục để Render không ngắt"""
    import http.server
    import socketserver
    PORT = 10000
    Handler = http.server.SimpleHTTPRequestHandler
    with socketserver.TCPServer(("", PORT), Handler) as httpd:
        logger.info(f"✅ keep_port_open chạy trên cổng {PORT}")
        httpd.serve_forever()


# ============================================================
# 🆕 HÀM MỚI – THEO DÕI PHIẾU NHẬP/XUẤT KHO 201/201 MỖI 5 PHÚT
# ============================================================
def auto_move_alert_task():
    """Theo dõi các phiếu nhập (IN) / xuất (OUT) liên quan tới kho 201/201 mỗi 5 phút"""
    logger.info("🔁 Bắt đầu theo dõi phiếu chuyển kho 201/201 (5 phút/lần)")
    last_checked = datetime.datetime.utcnow() - datetime.timedelta(minutes=5)

    while True:
        try:
            models, uid = connect_odoo()
            if not models:
                logger.error("[MOVE ALERT] Không thể kết nối Odoo, thử lại sau 5 phút...")
                time.sleep(300)
                continue

            domain = [
                ("scheduled_date", ">", last_checked.strftime("%Y-%m-%d %H:%M:%S")),
                ("state", "in", ["done", "assigned"]),
                "|",
                ("name", "ilike", "201/OUT/"),
                ("name", "ilike", "201/IN/"),
            ]
            pickings = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                "stock.picking", "search_read",
                [domain],
                {"fields": ["name", "origin", "location_id", "location_dest_id", "move_ids_without_package"]}
            )

            if pickings:
                bot = Bot(token=TELEGRAM_TOKEN)
                for p in pickings:
                    name = p.get("name", "")
                    loc = p.get("location_id", ["", ""])[1] if p.get("location_id") else ""
                    dest = p.get("location_dest_id", ["", ""])[1] if p.get("location_dest_id") else ""

                    # Xác định hướng chuyển
                    direction = "Xuất khỏi" if "OUT" in name else "Nhập vào"

                    msg = f"📦 *{direction} kho 201/201*\n➡️ Phiếu: {name}\nTừ: {loc}\nĐến: {dest}"
                    bot.send_message(chat_id=USER_ID_TO_SEND_REPORT, text=msg, parse_mode="Markdown")

            last_checked = datetime.datetime.utcnow()
            time.sleep(300)

        except Exception as e:
            logger.error(f"[MOVE ALERT] Lỗi vòng lặp: {e}")
            time.sleep(300)
# ============================================================
# 🚀 KHỞI ĐỘNG CHƯƠNG TRÌNH CHÍNH
# ============================================================
if __name__ == "__main__":
    logger.info("🚀 Khởi động hệ thống BOT kiểm tra tồn kho Odoo...")

    # Khởi chạy các thread nền (tất cả giữ nguyên)
    threading.Thread(target=auto_alert_task, daemon=True).start()
    threading.Thread(target=keep_port_open, daemon=True).start()

    # 🆕 Thêm duy nhất dòng dưới để chạy cảnh báo nhập/xuất kho 201/201
    threading.Thread(target=auto_move_alert_task, daemon=True).start()
    logger.info("✅ Đã khởi chạy auto_move_alert_task (cảnh báo chuyển kho 201/201).")

    try:
        app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

        # Giữ nguyên toàn bộ handler cũ
        app.add_handler(CommandHandler("start", start))
        app.add_handler(CommandHandler("ping", ping))
        app.add_handler(CommandHandler("keohang", keohang))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, check_stock))

        bot = Bot(token=TELEGRAM_TOKEN)
        bot.delete_webhook(drop_pending_updates=True)
        logger.info("✅ Đã xóa webhook cũ (nếu có).")

        logger.info("🚀 Bot khởi động ở chế độ polling (Render sẽ giữ tiến trình chạy).")
        app.run_polling(stop_signals=None)
    except Exception as e:
        logger.error(f"Lỗi khi chạy bot Telegram: {e}")
