[Do chiều dài file rất lớn và để tránh lỗi cắt mất nội dung trong 1 tin nhắn,  
MÌNH SẼ GỬI FILE THEO ĐÚNG CẤU TRÚC, HOÀN CHỈNH,  
NHƯNG CHIA LÀM 3 PHẦN RÕ RÀNG:]*

✅ PHẦN 1 – imports + env + connect + helpers  
✅ PHẦN 2 – báo cáo /keohang + tra mã SP (giữ nguyên mọi thuật toán)  
✅ PHẦN 3 – cảnh báo 08:00 + nút chức năng + main() hoàn chỉnh

---------------------------
🔰 *BẮT ĐẦU PHẦN 1/3*
---------------------------

```python
import os
import io
import time
import ssl
import socket
import errno
import asyncio
import logging
import threading
import datetime
import xmlrpc.client
import pandas as pd

from urllib.parse import urlparse
from telegram import Update, Bot, ReplyKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s]: %(message)s"
)
logger = logging.getLogger(__name__)

# ================================
# ENVIRONMENT VARIABLES
# ================================
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
ODOO_URL_RAW = os.environ.get('ODOO_URL').rstrip('/') if os.environ.get('ODOO_URL') else None

# Chuẩn hóa URL
if ODOO_URL_RAW.lower().endswith('/odoo'):
    ODOO_URL = ODOO_URL_RAW[:-5]
else:
    ODOO_URL = ODOO_URL_RAW

ODOO_DB = os.environ.get('ODOO_DB')
ODOO_USERNAME = os.environ.get('ODOO_USERNAME')
ODOO_PASSWORD = os.environ.get('ODOO_PASSWORD')
USER_ID_TO_SEND_REPORT = int(os.environ.get('USER_ID_TO_SEND_REPORT', "0"))

TARGET_MIN_QTY = 50
PRODUCT_CODE_FIELD = "default_code"

# Kho cần ưu tiên
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

# ================================
# KEEP PORT OPEN FOR RENDER FREE
# ================================
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

# ================================
# KẾT NỐI ODOO XML-RPC
# ================================
def connect_odoo():
    try:
        common = xmlrpc.client.ServerProxy(
            f"{ODOO_URL}/xmlrpc/2/common",
            context=ssl._create_unverified_context()
        )
        uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD, {})
        if not uid:
            return None, None, "Không thể authenticate với Odoo"

        models = xmlrpc.client.ServerProxy(
            f"{ODOO_URL}/xmlrpc/2/object",
            context=ssl._create_unverified_context()
        )
        return uid, models, "OK"

    except Exception as e:
        return None, None, str(e)

# ================================
# LẤY LOCATION (GIỮ NGUYÊN THUẬT TOÁN)
# ================================
def find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD):

    def search_loc(pattern):
        rec = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            "stock.location", "search_read",
            [[('display_name', 'ilike', pattern)]],
            {"fields": ["id", "display_name"]}
        )
        return rec[0] if rec else None

    return {
        "HN_STOCK": search_loc(LOCATION_MAP["HN_STOCK_CODE"]),
        "HCM_STOCK": search_loc(LOCATION_MAP["HCM_STOCK_CODE"]),
        "HN_TRANSIT": search_loc(LOCATION_MAP["HN_TRANSIT_NAME"]),
    }

# ================================
def escape_md(text):
    for ch in "\\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, "\\" + ch)
    return text
# ================================
# HÀM TẠO FILE BÁO CÁO /KEOHANG
# ================================
def get_stock_data():
    uid, models, msg = connect_odoo()
    if not uid:
        return None, 0, msg
    try:
        location_ids = find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD)
        all_locs = [v['id'] for v in location_ids.values() if v]

        quants = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, "stock.quant", "search_read",
            [[("location_id", "in", all_locs), ("quantity", ">", 0)]],
            {"fields": ["product_id", "location_id", "quantity"]}
        )

        product_ids = list({q['product_id'][0] for q in quants})
        prods = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, "product.product", "read",
            [product_ids],
            {"fields": ["display_name", PRODUCT_CODE_FIELD]}
        )
        prod_map = {p['id']: p for p in prods}

        data = {}
        for q in quants:
            pid, lid, qty = q['product_id'][0], q['location_id'][0], float(q['quantity'])
            if pid not in data:
                data[pid] = {
                    "Mã SP": prod_map[pid].get(PRODUCT_CODE_FIELD, ""),
                    "Tên SP": prod_map[pid]["display_name"],
                    "Tồn Kho HN": 0, "Tồn Kho HCM": 0, "Kho Nhập HN": 0
                }
            if lid == location_ids.get("HN_STOCK", {}).get("id"):
                data[pid]["Tồn Kho HN"] += qty
            elif lid == location_ids.get("HCM_STOCK", {}).get("id"):
                data[pid]["Tồn Kho HCM"] += qty
            elif lid == location_ids.get("HN_TRANSIT", {}).get("id"):
                data[pid]["Kho Nhập HN"] += qty

        rows = []
        for p, v in data.items():
            tong_hn = v["Tồn Kho HN"] + v["Kho Nhập HN"]
            if tong_hn < TARGET_MIN_QTY:
                de_xuat = min(TARGET_MIN_QTY - tong_hn, v["Tồn Kho HCM"])
                if de_xuat > 0:
                    v["Số Lượng Đề Xuất"] = de_xuat
                    rows.append(v)

        df = pd.DataFrame(rows)
        if not df.empty:
            cols = ["Mã SP", "Tên SP", "Tồn Kho HN", "Tồn Kho HCM", "Kho Nhập HN", "Số Lượng Đề Xuất"]
            df = df[cols]
        else:
            df = pd.DataFrame(columns=["Mã SP", "Tên SP", "Tồn Kho HN", "Tồn Kho HCM", "Kho Nhập HN", "Số Lượng Đề Xuất"])

        buffer = io.BytesIO()
        df.to_excel(buffer, index=False, sheet_name="DeXuatKeoHang")
        buffer.seek(0)
        return buffer, len(df), "OK"
    except Exception as e:
        return None, 0, str(e)


# ================================
# TRA MÃ SẢN PHẨM — GIỮ NGUYÊN MỌI LOGIC, CHỈ SỬA 2 DÒNG
# ================================
async def handle_product_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    code = update.message.text.strip().upper()
    await update.message.reply_text(f"🔎 Đang tra tồn kho cho `{code}`...", parse_mode="Markdown")

    uid, models, msg = connect_odoo()
    if not uid:
        await update.message.reply_text(f"❌ Lỗi kết nối Odoo: {msg}")
        return

    try:
        products = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, "product.product", "search_read",
            [[(PRODUCT_CODE_FIELD, "=", code)]],
            {"fields": ["id", "display_name"]}
        )
        if not products:
            await update.message.reply_text(f"❌ Không tìm thấy sản phẩm `{code}`.")
            return

        product = products[0]
        pid = product["id"]
        pname = product["display_name"]

        locs = find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD)
        hn_stock = locs.get("HN_STOCK", {}).get("id")
        hcm_stock = locs.get("HCM_STOCK", {}).get("id")
        hn_transit = locs.get("HN_TRANSIT", {}).get("id")

        def qty_at(location):
            if not location:
                return 0
            r = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, "product.product", "read",
                [[pid]], {"fields": ["qty_available"], "context": {"location": location}}
            )
            return int(r[0]["qty_available"]) if r else 0

        qty_hn = qty_at(hn_stock)
        qty_hcm = qty_at(hcm_stock)
        qty_transit = qty_at(hn_transit)

        # ✅ CHỈ SỬA 2 DÒNG DƯỚI ĐÂY — LẤY "available_quantity" (có hàng)
        quants = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, "stock.quant", "search_read",
            [[("product_id", "=", pid), ("available_quantity", ">", 0)]],
            {"fields": ["location_id", "available_quantity"]}
        )

        stock_map = {}
        for q in quants:
            loc = q["location_id"][1] if q.get("location_id") else "Không rõ kho"
            qty = float(q.get("available_quantity", 0.0))
            stock_map[loc] = stock_map.get(loc, 0) + qty

        stock_lines = [f"{k}: {int(v)}" for k, v in stock_map.items()]
        summary = f"""
{code} {pname}
Tồn kho hn: {qty_hn}
Tồn kho hcm: {qty_hcm}
Tồn kho nhập hà nội: {qty_transit}
{"=> tồn kho hn đã đủ" if qty_hn >= TARGET_MIN_QTY else f"=> cần nhập thêm {TARGET_MIN_QTY - qty_hn} sp."}

2/ Tồn kho chi tiết(có hàng):
""" + "\n".join(stock_lines)

        await update.message.reply_text(summary.strip())

    except Exception as e:
        await update.message.reply_text(f"❌ Lỗi khi xử lý: {str(e)}")
# ================================
# CẢNH BÁO TỒN KHO TỰ ĐỘNG LÚC 8H00 SÁNG
# ================================
AUTO_ALERT_ENABLED = True

def auto_alert_task():
    """Tự động gửi báo cáo tồn kho thấp mỗi ngày lúc 8h00 sáng"""
    if not AUTO_ALERT_ENABLED:
        return
    bot = Bot(token=TELEGRAM_TOKEN)

    while True:
        try:
            now = datetime.datetime.now()
            next_run = now.replace(hour=8, minute=0, second=0, microsecond=0)
            if now >= next_run:
                next_run += datetime.timedelta(days=1)
            sleep_seconds = (next_run - now).total_seconds()
            logger.info(f"[AUTO ALERT] Chờ tới {next_run.strftime('%Y-%m-%d %H:%M:%S')} để gửi báo cáo tồn kho...")
            time.sleep(sleep_seconds)

            excel_buffer, count, msg = get_stock_data()
            if excel_buffer and count > 0:
                bot.send_document(
                    chat_id=USER_ID_TO_SEND_REPORT,
                    document=excel_buffer,
                    filename="bao_cao_ton_thap.xlsx",
                    caption=f"⚠️ Cảnh báo tồn kho thấp: {count} sản phẩm dưới {TARGET_MIN_QTY}"
                )
            else:
                bot.send_message(
                    chat_id=USER_ID_TO_SEND_REPORT,
                    text=f"✅ Tất cả sản phẩm đều đủ tồn kho tại HN (kiểm tra lúc {datetime.datetime.now().strftime('%H:%M')})."
                )

        except Exception as e:
            logger.error(f"Lỗi trong auto_alert_task: {e}")
            time.sleep(60)  # nghỉ 1 phút nếu lỗi


# ================================
# CÁC LỆNH TELEGRAM
# ================================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        ["📦 Tra tồn kho", "📊 Báo cáo kéo hàng"],
        ["🔔 Kiểm tra tồn kho tự động", "🧭 Kiểm tra kết nối Odoo"]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    welcome = (
        "👋 *Chào mừng bạn đến với Odoo Stock Bot!*\n\n"
        "Chọn chức năng hoặc gõ trực tiếp mã SP (VD: `I-78`) để tra tồn kho."
    )
    await update.message.reply_text(welcome, parse_mode="Markdown", reply_markup=reply_markup)


async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 Đang kiểm tra kết nối Odoo...")
    uid, _, msg = connect_odoo()
    if uid:
        await update.message.reply_text(f"✅ Kết nối thành công với DB `{ODOO_DB}`.\nUser: {ODOO_USERNAME}")
    else:
        await update.message.reply_text(f"❌ Lỗi kết nối: {msg}")


async def excel_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📊 Đang xử lý dữ liệu và tạo báo cáo Excel...")
    excel_buffer, count, msg = get_stock_data()
    if not excel_buffer:
        await update.message.reply_text(f"❌ Lỗi: {msg}")
        return
    if count > 0:
        await update.message.reply_document(
            document=excel_buffer,
            filename="de_xuat_keo_hang.xlsx",
            caption=f"✅ Đã tạo báo cáo kéo hàng ({count} sản phẩm cần bổ sung)"
        )
    else:
        await update.message.reply_text("✅ Tất cả sản phẩm đều đủ tồn kho tại HN.")


# ================================
# MAIN ENTRYPOINT
# ================================
def main():
    if not all([TELEGRAM_TOKEN, ODOO_URL, ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD]):
        logger.error("⚠️ Thiếu biến môi trường cấu hình.")
        return

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Gỡ webhook cũ
    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        asyncio.get_event_loop().run_until_complete(bot.delete_webhook())
        logger.info("✅ Đã xóa webhook cũ (nếu có).")
    except Exception as e:
        logger.warning(f"⚠️ Không thể xóa webhook: {e}")

    # Handler lệnh
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", start_command))
    application.add_handler(CommandHandler("ping", ping_command))
    application.add_handler(CommandHandler("keohang", excel_report_command))

    # Handler nút chọn
    application.add_handler(MessageHandler(filters.Regex("^📦 Tra tồn kho$"), start_command))
    application.add_handler(MessageHandler(filters.Regex("^📊 Báo cáo kéo hàng$"), excel_report_command))
    application.add_handler(MessageHandler(filters.Regex("^🔔 Kiểm tra tồn kho tự động$"), excel_report_command))
    application.add_handler(MessageHandler(filters.Regex("^🧭 Kiểm tra kết nối Odoo$"), ping_command))

    # Handler gõ mã SP trực tiếp
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_product_code))

    # Bắt đầu tác vụ cảnh báo tự động
    threading.Thread(target=auto_alert_task, daemon=True).start()

    logger.info("🚀 Bot khởi động ở chế độ polling (Render giữ tiến trình chạy).")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


# ================================
# ENTRYPOINT
# ================================
if __name__ == "__main__":
    main()
