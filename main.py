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
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# ---------------- Config & Env ----------------
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
# Normalise ODOO URL (remove trailing / and optional /odoo)
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
# Mở port giả để Render (Web Service free) không báo timeout.
def keep_port_open():
    try:
        s = socket.socket()
        s.bind(("0.0.0.0", 10000))
        s.listen(1)
        while True:
            conn, _ = s.accept()
            conn.close()
    except Exception:
        # nếu không bind đc (port bị chiếm) thì im lặng
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
            error_message = f"Đăng nhập thất bại (uid=0). kiểm tra lại user/pass/db."
            return None, None, error_message
        models = xmlrpc.client.ServerProxy(f'{ODOO_URL_FINAL}/xmlrpc/2/object', context=context)
        return uid, models, "kết nối thành công."
    except xmlrpc.client.ProtocolError as pe:
        error_message = f"lỗi giao thức odoo: {pe}"
        return None, None, error_message
    except Exception as e:
        error_message = f"lỗi kết nối odoo xml-rpc: {e}"
        return None, None, error_message

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
        if preferred_loc and 'id' in preferred_loc and 'display_name' in preferred_loc:
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
    text = str(text)
    for char in special_chars:
        text = text.replace(char, f'\\{char}')
    return text.replace('\\`', '`')

# ---------------- Report /keohang (giữ nguyên logic) ----------------
def get_stock_data():
    uid, models, error_msg = connect_odoo()
    if not uid:
        return None, 0, error_msg
    try:
        location_ids = find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD)
        if len(location_ids) < 3:
            error_msg = f"không tìm thấy đủ 3 kho cần thiết: {list(location_ids.keys())}"
            logger.error(error_msg)
            return None, 0, error_msg

        all_locations_ids = [v['id'] for v in location_ids.values()]
        quant_domain = [('location_id', 'in', all_locations_ids), ('quantity', '>', 0)]
        quant_data = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'stock.quant', 'search_read',
            [quant_domain],
            {'fields': ['product_id', 'location_id', 'quantity']}
        )

        product_ids = list(set([q['product_id'][0] for q in quant_data]))
        product_info = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'product.product', 'search_read',
            [[('id', 'in', product_ids)]],
            {'fields': ['display_name', PRODUCT_CODE_FIELD]}
        )
        product_map = {p['id']: p for p in product_info}

        data = {}
        for q in quant_data:
            prod_id = q['product_id'][0]
            loc_id = q['location_id'][0]
            qty = float(q['quantity'])
            if prod_id not in data and prod_id in product_map:
                data[prod_id] = {
                    'Mã SP': product_map[prod_id].get(PRODUCT_CODE_FIELD, 'N/A'),
                    'Tên SP': product_map[prod_id]['display_name'],
                    'Tồn Kho HN': 0.0, 'Tồn Kho HCM': 0.0, 'Kho Nhập HN': 0.0, 'Tổng Tồn HN': 0.0, 'Số Lượng Đề Xuất': 0.0
                }
            if loc_id == location_ids.get('HN_STOCK', {}).get('id'):
                data[prod_id]['Tồn Kho HN'] += qty
            elif loc_id == location_ids.get('HCM_STOCK', {}).get('id'):
                data[prod_id]['Tồn Kho HCM'] += qty
            elif loc_id == location_ids.get('HN_TRANSIT', {}).get('id'):
                data[prod_id]['Kho Nhập HN'] += qty

        report_data = []
        for prod_id, info in data.items():
            info['Tổng Tồn HN'] = info['Tồn Kho HN'] + info['Kho Nhập HN']
            if info['Tổng Tồn HN'] < TARGET_MIN_QTY:
                qty_needed = TARGET_MIN_QTY - info['Tổng Tồn HN']
                info['Số Lượng Đề Xuất'] = min(qty_needed, info['Tồn Kho HCM'])
                if info['Số Lượng Đề Xuất'] > 0:
                    report_data.append(info)

        df = pd.DataFrame(report_data)
        COLUMNS_ORDER = ['Mã SP', 'Tên SP', 'Tồn Kho HN', 'Tồn Kho HCM', 'Kho Nhập HN', 'Số Lượng Đề Xuất']
        if not df.empty:
            df = df[COLUMNS_ORDER]
            for col in ['Tồn Kho HN', 'Tồn Kho HCM', 'Kho Nhập HN', 'Số Lượng Đề Xuất']:
                df[col] = df[col].apply(lambda x: int(round(x)))
        else:
            df = pd.DataFrame(columns=COLUMNS_ORDER)

        excel_buffer = io.BytesIO()
        df.to_excel(excel_buffer, index=False, sheet_name='DeXuatKeoHang')
        excel_buffer.seek(0)
        return excel_buffer, len(report_data), "thành công"
    except Exception as e:
        error_msg = f"lỗi khi truy vấn dữ liệu odoo xml-rpc: {e}"
        return None, 0, error_msg

# ---------------- Handle product code (CHỈ đổi chi tiết lấy Có hàng) ----------------
async def handle_product_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    product_code = update.message.text.strip().upper()
    await update.message.reply_text(f"đang tra tồn cho `{product_code}`, vui lòng chờ!", parse_mode='Markdown')

    uid, models, error_msg = connect_odoo()
    if not uid:
        await update.message.reply_text(f"❌ lỗi kết nối odoo. chi tiết: `{escape_markdown(error_msg)}`", parse_mode='Markdown')
        return

    try:
        # Lấy location ids cần thiết
        location_ids = find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD)
        hn_transit_id = location_ids.get('HN_TRANSIT', {}).get('id')
        hn_stock_id = location_ids.get('HN_STOCK', {}).get('id')
        hcm_stock_id = location_ids.get('HCM_STOCK', {}).get('id')

        # Lấy sản phẩm
        product_domain = [(PRODUCT_CODE_FIELD, '=', product_code)]
        products = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'product.product', 'search_read',
            [product_domain],
            {'fields': ['display_name', 'id']}
        )
        if not products:
            await update.message.reply_text(f"❌ Không tìm thấy sản phẩm nào có mã `{product_code}`, ĐỒ NGOO")
            return
        product = products[0]
        product_id = product['id']
        product_name = product['display_name']

        # Summary: qty_available (Hiện có) theo từng kho
        def get_qty_available(location_id):
            if not location_id: return 0
            stock_product_info = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, 'product.product', 'read',
                [[product_id]],
                {'fields': ['qty_available'], 'context': {'location': location_id}}
            )
            return int(round(stock_product_info[0].get('qty_available', 0.0))) if stock_product_info and stock_product_info[0] else 0

        hn_stock_qty = get_qty_available(hn_stock_id)
        hn_transit_qty = get_qty_available(hn_transit_id)
        hcm_stock_qty = get_qty_available(hcm_stock_id)

        # Detail: lấy tồn chi tiết - CHỈ THAY ĐỔI 2 DÒNG Ở ĐÂY để dùng available_quantity
        quant_domain_all = [('product_id', '=', product_id), ('available_quantity', '>', 0)]

        # ✅ Thay 1: lấy available_quantity thay vì quantity
        quant_data_all = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'stock.quant', 'search_read',
            [quant_domain_all],
            {'fields': ['location_id', 'available_quantity']}
        )

        # Lấy tên location
        location_ids_all = list({q['location_id'][0] for q in quant_data_all if q.get('location_id')})
        if location_ids_all:
            location_info = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, 'stock.location', 'read',
                [location_ids_all],
                {'fields': ['id', 'display_name', 'complete_name', 'usage']}
            )
        else:
            location_info = []
        location_map = {loc['id']: loc for loc in location_info}

        # ✅ Thay 2: cộng dồn theo available_quantity
        stock_by_loc_id = {}
        for q in quant_data_all:
            loc_field = q.get('location_id')
            if not loc_field:
                continue
            loc_id = loc_field[0]
            qty = float(q.get('available_quantity', 0.0))
            if qty <= 0:
                continue
            stock_by_loc_id[loc_id] = stock_by_loc_id.get(loc_id, 0.0) + qty

        # Chuyển sang tên kho và dùng int (cắt thập phân)
        all_stock_details = {}
        for loc_id, qty in stock_by_loc_id.items():
            display_name = location_map.get(loc_id, {}).get('complete_name') or location_map.get(loc_id, {}).get('display_name') or f"ID:{loc_id}"
            qty_int = int(qty)
            if qty_int > 0:
                all_stock_details[display_name] = qty_int

        # Tính đề xuất (giữ nguyên logic)
        total_hn_stock = hn_stock_qty + hn_transit_qty
        recommendation_qty = 0
        if total_hn_stock < TARGET_MIN_QTY:
            qty_needed = TARGET_MIN_QTY - total_hn_stock
            recommendation_qty = min(qty_needed, hcm_stock_qty)
        recommendation_text = f"=> đề xuất nhập thêm `{int(recommendation_qty)}` sp để hn đủ tồn `{TARGET_MIN_QTY}` sản phẩm." if recommendation_qty > 0 else f"=> tồn kho hn đã đủ (`{int(total_hn_stock)}`/{TARGET_MIN_QTY} sp)."

        # Format trả về theo thứ tự bạn yêu cầu
        header_line = f"{product_code} {product_name}"
        summary_lines = [
            f"tồn kho hn: {int(hn_stock_qty)}",
            f"tồn kho hcm: {int(hcm_stock_qty)}",
            f"tồn kho nhập hà nội: {int(hn_transit_qty)}",
            recommendation_text.replace('`', '')
        ]

        # Sắp xếp tồn chi tiết: ưu tiên PRIORITY_LOCATIONS (so sánh theo substring)
        priority_items = []
        other_items = []
        used_names = set()
        for code in PRIORITY_LOCATIONS:
            for name, qty in all_stock_details.items():
                if code.lower() in name.lower() and name not in used_names:
                    priority_items.append((name, qty))
                    used_names.add(name)
                    break
        for name, qty in sorted(all_stock_details.items()):
            if name not in used_names:
                other_items.append((name, qty))
                used_names.add(name)

        detail_lines = []
        for name, qty in priority_items + other_items:
            detail_lines.append(f"{name}: {qty}")

        detail_content = "\n".join(detail_lines) if detail_lines else "Không có tồn kho chi tiết lớn hơn 0."

        message = f"""{header_line}
{summary_lines[0]}
{summary_lines[1]}
{summary_lines[2]}
{summary_lines[3]}

2/ Tồn kho chi tiết(Có hàng):
{detail_content}
"""
        await update.message.reply_text(message.strip())

    except Exception as e:
        logger.error(f"Lỗi khi tra cứu sản phẩm xml-rpc: {e}")
        await update.message.reply_text(f"❌ Có lỗi xảy ra khi truy vấn odoo: {str(e)}")

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
        await update.message.reply_document(document=excel_buffer, filename='de_xuat_keo_hang.xlsx', caption=f"✅ iem đây! đã tìm thấy {item_count} sản phẩm cần kéo hàng.")
    else:
        await update.message.reply_text(f"✅ Tất cả sản phẩm đã đạt mức tồn kho tối thiểu {TARGET_MIN_QTY} tại kho hn.")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.message.from_user.first_name
    welcome_message = (
        f"Chào mừng {user_name} đến với cuộc đời iem!\n\n"
        "1. Gõ mã sp (vd: I-78) để tra tồn.\n"
        "2. Dùng lệnh /keohang để tạo báo cáo excel.\n"
        "3. Dùng lệnh /ping để kiểm tra kết nối."
        "4. Không có nhu cầu thì đừng phiền iem!"
    )
    await update.message.reply_text(welcome_message)

# ---------------- Main ----------------
def main():
    if not TELEGRAM_TOKEN or not ODOO_URL_RAW or not ODOO_DB or not ODOO_USERNAME or not ODOO_PASSWORD:
        logger.error("vui lòng thiết lập tất cả các biến môi trường cần thiết (token, url, db, user, pass).")
        return

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # xóa webhook (gọi đồng bộ để tránh warning)
    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        try:
            asyncio.get_event_loop().run_until_complete(bot.delete_webhook())
            logger.info("đã xóa webhook cũ (nếu có).")
        except Exception as e:
            logger.warning(f"lỗi khi xóa webhook (không ảnh hưởng): {e}")
    except Exception as e:
        logger.warning(f"lỗi khi tạo Bot object: {e}")

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", start_command))
    application.add_handler(CommandHandler("ping", ping_command))
    application.add_handler(CommandHandler("keohang", excel_report_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_product_code))

    logger.info("bot đang khởi chạy ở chế độ polling.")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)
    # ====================== AUTO MOVE ALERT START ======================
threading.Thread(target=auto_move_alert_task, daemon=True).start()
import datetime

def auto_move_alert_task():
    """Theo dõi phiếu chuyển hàng có mã 201/IN hoặc 201/OUT liên quan kho Hà Nội"""
    logger.info("🔁 Bắt đầu theo dõi phiếu chuyển kho 201/201 Hà Nội (5 phút/lần)")
    last_check = datetime.datetime.utcnow() - datetime.timedelta(minutes=5)
    bot = Bot(token=TELEGRAM_TOKEN)
    notified = set()

    while True:
        try:
            uid, models, msg = connect_odoo()
            if not uid:
                logger.error(f"[MOVE ALERT] Không kết nối được Odoo: {msg}")
                time.sleep(300)
                continue

            now = datetime.datetime.utcnow()
            domain = [
                ("state", "=", "done"),
                ("write_date", ">=", (last_check - datetime.timedelta(minutes=1)).strftime("%Y-%m-%d %H:%M:%S")),
            ]
            pickings = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                "stock.picking", "search_read",
                [domain],
                {"fields": ["name", "location_id", "location_dest_id", "move_ids_without_package", "write_date"]}
            )

            for picking in pickings:
                name = picking.get("name", "")
                if not name.startswith("201/OUT") and not name.startswith("201/IN"):
                    continue

                if name in notified:
                    continue
                notified.add(name)

                source = picking.get("location_id", ["", ""])[1]
                dest = picking.get("location_dest_id", ["", ""])[1]
                moves = models.execute_kw(
                    ODOO_DB, uid, ODOO_PASSWORD,
                    "stock.move", "read",
                    [picking["move_ids_without_package"]],
                    {"fields": ["product_id", "product_uom_qty"]}
                )

                for mv in moves:
                    product_name = mv.get("product_id", ["", ""])[1]
                    qty = mv.get("product_uom_qty", 0)
                    if name.startswith("201/OUT"):
                        direction = f"🔻 *Xuất khỏi kho 201/201 Kho Hà Nội*"
                        to_loc = dest
                    else:
                        direction = f"🔺 *Nhập vào kho 201/201 Kho Hà Nội*"
                        to_loc = source

                    text = (
                        f"📦 *Cập nhật chuyển kho*\n"
                        f"Phiếu: `{name}`\n"
                        f"{direction}\n\n"
                        f"*Tên SP:* {product_name}\n"
                        f"*Số lượng:* {qty}\n"
                        f"*Địa điểm đích:* {to_loc}"
                    )

                    try:
                        for chat_id in active_users:
                            bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
                    except Exception as e:
                        logger.error(f"[MOVE ALERT] Gửi cảnh báo lỗi: {e}")

            last_check = now
            time.sleep(300)  # 5 phút

        except Exception as e:
            logger.error(f"[MOVE ALERT] Lỗi vòng lặp: {e}")
            time.sleep(300)
# ====================== AUTO MOVE ALERT END ======================


if __name__ == '__main__':
    main()
