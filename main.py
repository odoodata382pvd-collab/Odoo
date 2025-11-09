import threading
import datetime
import time
from telegram import Bot
from telegram.ext import CommandHandler, MessageHandler, filters, Application
import logging

# Giữ nguyên toàn bộ phần cấu hình Odoo, token Telegram và các hàm connect_odoo() của bạn.

# ===== THÊM LOGGING (nếu chưa có) =====
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

# ===== BIẾN GLOBALES =====
active_users = set()  # danh sách các user đã tương tác, giữ nguyên logic gốc
# ==========================================================
# === HÀM MỚI: CẢNH BÁO PHIẾU NHẬP/XUẤT KHO 201/201 =======
# ==========================================================

def auto_move_alert_task():
    """
    Theo dõi các phiếu nhập hoặc xuất liên quan đến kho '201/201 Kho Hà Nội'
    Cứ mỗi 5 phút sẽ kiểm tra lại và gửi cảnh báo nếu phát sinh phiếu mới.
    """
    bot = Bot(token=TELEGRAM_TOKEN)
    logger.info("🔁 Bắt đầu theo dõi phiếu chuyển kho 201/201 Kho Hà Nội (5 phút/lần)")
    last_check = datetime.datetime.utcnow() - datetime.timedelta(minutes=5)
    notified = set()

    while True:
        try:
            uid, models, msg = connect_odoo()
            if not uid:
                logger.error(f"[MOVE ALERT] Không kết nối được Odoo: {msg}")
                time.sleep(300)
                continue

            now = datetime.datetime.utcnow()

            # Tìm các phiếu chuyển kho hoàn thành (done) trong 5 phút gần nhất
            pickings = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                "stock.picking", "search_read",
                [[
                    ("state", "=", "done"),
                    ("write_date", ">=", (last_check - datetime.timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")),
                ]],
                {"fields": ["name", "location_id", "location_dest_id", "move_ids_without_package", "write_date"]}
            )

            for p in pickings:
                name = p.get("name", "")
                if not name.startswith("201/OUT") and not name.startswith("201/IN"):
                    continue
                if name in notified:
                    continue
                notified.add(name)

                src = p.get("location_id", ["", ""])[1]
                dest = p.get("location_dest_id", ["", ""])[1]

                # Lấy danh sách sản phẩm trong phiếu
                moves = models.execute_kw(
                    ODOO_DB, uid, ODOO_PASSWORD,
                    "stock.move", "read",
                    [p["move_ids_without_package"]],
                    {"fields": ["product_id", "product_uom_qty"]}
                )

                for mv in moves:
                    product_id = mv["product_id"][0]
                    product_name = mv["product_id"][1]
                    qty = mv["product_uom_qty"]

                    # Lấy tồn "Có hàng" còn lại tại kho 201/201
                    quants = models.execute_kw(
                        ODOO_DB, uid, ODOO_PASSWORD,
                        "stock.quant", "search_read",
                        [[
                            ("product_id", "=", product_id),
                            ("location_id.complete_name", "ilike", "201/201")
                        ]],
                        {"fields": ["available_quantity"]}
                    )
                    current_stock = sum(q["available_quantity"] for q in quants)

                    # Soạn nội dung tin nhắn
                    if name.startswith("201/OUT"):
                        direction = "🔻 *Xuất khỏi kho 201/201 Kho Hà Nội*"
                        target = dest
                    else:
                        direction = "🔺 *Nhập vào kho 201/201 Kho Hà Nội*"
                        target = src

                    text = (
                        f"📦 *Cập nhật chuyển kho*\n"
                        f"Phiếu: `{name}`\n"
                        f"{direction}\n\n"
                        f"*Tên SP:* {product_name}\n"
                        f"*Số lượng:* {qty}\n"
                        f"*Địa điểm đích:* {target}\n"
                        f"*Tồn 'Có hàng' còn lại tại 201/201:* {current_stock}"
                    )

                    # Gửi tới tất cả user đã từng dùng bot
                    for chat_id in active_users:
                        try:
                            bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
                        except Exception as e:
                            logger.error(f"Lỗi gửi cảnh báo: {e}")

            last_check = now
            time.sleep(300)  # Lặp lại sau 5 phút

        except Exception as e:
            logger.error(f"[MOVE ALERT] Lỗi vòng lặp: {e}")
            time.sleep(300)
# ==========================================================
# 🚀 KHỞI ĐỘNG CHƯƠNG TRÌNH CHÍNH
# ==========================================================
if __name__ == "__main__":
    logger.info("🚀 Khởi động hệ thống BOT kiểm tra tồn kho Odoo...")

    # Giữ nguyên các thread cũ của bạn (nếu có)
    # Chỉ thêm dòng dưới để khởi chạy cảnh báo chuyển kho 201/201 mỗi 5 phút
    threading.Thread(target=auto_move_alert_task, daemon=True).start()
    logger.info("✅ Đã khởi chạy auto_move_alert_task (cảnh báo chuyển kho 201/201).")

    # Nếu đã có keep_port_open, auto_alert_task... thì giữ nguyên như cũ
    # Ví dụ:
    # threading.Thread(target=auto_alert_task, daemon=True).start()
    # threading.Thread(target=keep_port_open, daemon=True).start()

    # Chạy bot Telegram chính (polling)
    try:
        application = Application.builder().token(TELEGRAM_TOKEN).build()

        # Đăng ký các handler cũ của bạn (start, ping, keohang, check_stock, ...)
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("ping", ping))
        application.add_handler(CommandHandler("keohang", keohang))
        application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, check_stock))

        # Xóa webhook cũ nếu có để tránh xung đột
        bot = Bot(token=TELEGRAM_TOKEN)
        bot.delete_webhook(drop_pending_updates=True)
        logger.info("✅ Đã xóa webhook cũ (nếu có).")

        logger.info("🚀 Bot khởi động ở chế độ polling...")
        application.run_polling(stop_signals=None)
    except Exception as e:
        logger.error(f"Lỗi khi chạy bot Telegram: {e}")
