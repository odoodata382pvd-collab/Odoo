# main.py - Phiên bản phục hồi đầy đủ + bổ sung tính năng /checkexcel + xử lý excel so tồn
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
# Normalise ODOO URL
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

# ---------------- Keep port open ----------------
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
            return None, None, "Đăng nhập thất bại. kiểm tra lại user/pass/db."
        models = xmlrpc.client.ServerProxy(f'{ODOO_URL_FINAL}/xmlrpc/2/object', context=context)
        return uid, models, "kết nối thành công."
    except Exception as e:
        return None, None, f"lỗi kết nối: {e}"
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

        preferred_loc = next(
            (l for l in loc_data if name_code.lower() in l['display_name'].lower()),
            loc_data[0]
        )
        if preferred_loc:
            return {
                'id': preferred_loc['id'],
                'name': preferred_loc.get('display_name') or preferred_loc.get('complete_name')
            }
        return None

    hn_stock = search_location(LOCATION_MAP['HN_STOCK_CODE'])
    if hn_stock:
        location_ids['HN_STOCK'] = hn_stock

    hcm_stock = search_location(LOCATION_MAP['HCM_STOCK_CODE'])
    if hcm_stock:
        location_ids['HCM_STOCK'] = hcm_stock

    hn_transit = search_location(LOCATION_MAP['HN_TRANSIT_NAME'])
    if hn_transit:
        location_ids['HN_TRANSIT'] = hn_transit

    return location_ids


def escape_markdown(text):
    special_chars = ['\\', '_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+',
                     '-', '=', '|', '{', '}', '.', '!']
    text = str(text)
    for char in special_chars:
        text = text.replace(char, f'\\{char}')
    return text.replace('\\`', '`')


# ---------------- Report /keohang ----------------
def get_stock_data():
    uid, models, error_msg = connect_odoo()
    if not uid:
        return None, 0, error_msg

    try:
        location_ids = find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD)
        if len(location_ids) < 3:
            return None, 0, f"không tìm đủ 3 kho bắt buộc: {list(location_ids.keys())}"

        all_location_ids = [v['id'] for v in location_ids.values()]
        quant_domain = [('location_id', 'in', all_location_ids), ('quantity', '>', 0)]

        quant_data = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'stock.quant', 'search_read',
            [quant_domain],
            {'fields': ['product_id', 'location_id', 'quantity']}
        )

        product_ids = list({q['product_id'][0] for q in quant_data})
        product_info = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'product.product', 'search_read',
            [[('id', 'in', product_ids)]],
            {'fields': ['display_name', PRODUCT_CODE_FIELD]}
        )

        product_map = {p['id']: p for p in product_info}

        data = {}
        for q in quant_data:
            prod_id = q['product_id'][0]
            loc_id = q['location_id'][0]
            qty = float(q['quantity'])

            if prod_id not in data:
                if prod_id not in product_map:
                    continue
                data[prod_id] = {
                    'Mã SP': product_map[prod_id].get(PRODUCT_CODE_FIELD, 'N/A'),
                    'Tên SP': product_map[prod_id]['display_name'],
                    'Tồn Kho HN': 0.0,
                    'Tồn Kho HCM': 0.0,
                    'Kho Nhập HN': 0.0,
                    'Tổng Tồn HN': 0.0,
                    'Số Lượng Đề Xuất': 0.0,
                }

            if loc_id == location_ids['HN_STOCK']['id']:
                data[prod_id]['Tồn Kho HN'] += qty
            elif loc_id == location_ids['HCM_STOCK']['id']:
                data[prod_id]['Tồn Kho HCM'] += qty
            elif loc_id == location_ids['HN_TRANSIT']['id']:
                data[prod_id]['Kho Nhập HN'] += qty

        report_data = []

        for prod_id, info in data.items():
            info['Tổng Tồn HN'] = info['Tồn Kho HN'] + info['Kho Nhập HN']

            if info['Tổng Tồn HN'] < TARGET_MIN_QTY:
                needed = TARGET_MIN_QTY - info['Tổng Tồn HN']
                info['Số Lượng Đề Xuất'] = min(needed, info['Tồn Kho HCM'])
                if info['Số Lượng Đề Xuất'] > 0:
                    report_data.append(info)

        df = pd.DataFrame(report_data)
        COLUMNS_ORDER = [
            'Mã SP', 'Tên SP', 'Tồn Kho HN', 'Tồn Kho HCM',
            'Kho Nhập HN', 'Số Lượng Đề Xuất'
        ]

        if not df.empty:
            df = df[COLUMNS_ORDER]
            for col in ['Tồn Kho HN', 'Tồn Kho HCM', 'Kho Nhập HN', 'Số Lượng Đề Xuất']:
                df[col] = df[col].apply(lambda x: int(round(x)))
        else:
            df = pd.DataFrame(columns=COLUMNS_ORDER)

        buffer = io.BytesIO()
        df.to_excel(buffer, index=False, sheet_name='DeXuatKeoHang')
        buffer.seek(0)
        return buffer, len(report_data), "thành công"

    except Exception as e:
        return None, 0, f"Lỗi: {e}"


# ---------------- Handle product code ----------------
async def handle_product_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    product_code = update.message.text.strip().upper()
    await update.message.reply_text(
        f"đang tra tồn cho `{product_code}`, vui lòng chờ!",
        parse_mode='Markdown'
    )

    uid, models, error_msg = connect_odoo()
    if not uid:
        await update.message.reply_text(
            f"❌ lỗi kết nối odoo: `{escape_markdown(error_msg)}`",
            parse_mode='Markdown'
        )
        return

    try:
        location_ids = find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD)
        hn_transit_id = location_ids['HN_TRANSIT']['id']
        hn_stock_id = location_ids['HN_STOCK']['id']
        hcm_stock_id = location_ids['HCM_STOCK']['id']

        product_domain = [(PRODUCT_CODE_FIELD, '=', product_code)]
        products = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'product.product', 'search_read',
            [product_domain],
            {'fields': ['display_name', 'id']}
        )

        if not products:
            await update.message.reply_text(f"❌ Không tìm thấy mã `{product_code}`")
            return

        product = products[0]
        product_id = product['id']
        product_name = product['display_name']

        def get_qty(location_id):
            if not location_id:
                return 0
            stock_info = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                'product.product', 'read',
                [[product_id]],
                {'fields': ['qty_available'], 'context': {'location': location_id}}
            )
            if stock_info:
                return int(round(stock_info[0].get('qty_available', 0)))
            return 0

        hn_stock_qty = get_qty(hn_stock_id)
        hn_transit_qty = get_qty(hn_transit_id)
        hcm_stock_qty = get_qty(hcm_stock_id)

        total_hn = hn_stock_qty + hn_transit_qty
        rec_qty = 0

        if total_hn < TARGET_MIN_QTY:
            need = TARGET_MIN_QTY - total_hn
            rec_qty = min(need, hcm_stock_qty)

        rec_text = (
            f"=> đề xuất nhập thêm {rec_qty} sp để hn đủ {TARGET_MIN_QTY} sp."
            if rec_qty > 0 else
            f"=> tồn hn đã đủ ({total_hn}/{TARGET_MIN_QTY})."
        )

        quant_domain_all = [('product_id', '=', product_id), ('available_quantity', '>', 0)]
        quant_data_all = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'stock.quant', 'search_read',
            [quant_domain_all],
            {'fields': ['location_id', 'available_quantity']}
        )

        loc_ids = list({q['location_id'][0] for q in quant_data_all})
        if loc_ids:
            loc_info = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                'stock.location', 'read',
                [loc_ids],
                {'fields': ['id', 'display_name', 'complete_name']}
            )
            loc_map = {l['id']: l for l in loc_info}
        else:
            loc_map = {}

        stock_detail = {}
        for q in quant_data_all:
            loc_id = q['location_id'][0]
            qty = int(q['available_quantity'])
            if qty > 0:
                name = loc_map[loc_id].get('complete_name') or loc_map[loc_id]['display_name']
                stock_detail[name] = qty

        details_sorted = sorted(stock_detail.items())
        detail_lines = "\n".join([f"{k}: {v}" for k, v in details_sorted])

        msg = f"""{product_code} {product_name}
Tồn kho HN: {hn_stock_qty}
Tồn kho HCM: {hcm_stock_qty}
Tồn kho nhập HN: {hn_transit_qty}
{rec_text}

2/ Tồn kho chi tiết(Có hàng):
{detail_lines if detail_lines else "Không có tồn kho > 0."}
"""

        await update.message.reply_text(msg)

    except Exception as e:
        await update.message.reply_text(f"❌ lỗi truy vấn: {e}")
# ---------------- NEW FEATURE: PROCESS EXCEL ----------------

async def checkexcel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kích hoạt chế độ nhận file Excel."""
    context.user_data['waiting_for_excel'] = True
    await update.message.reply_text(
        "Vui lòng gửi file Excel cần kiểm tra (định dạng .xlsx).\n"
        "BOT sẽ tự so sánh tồn HN (HN + Kho nhập) và đề xuất cần nhập từ HCM."
    )


async def excel_file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Nhận file Excel sau khi chạy /checkexcel."""
    if not context.user_data.get('waiting_for_excel'):
        return  # tránh xử lý nhầm file khác

    context.user_data['waiting_for_excel'] = False

    try:
        file = await update.message.document.get_file()
        file_bytes = await file.download_as_bytearray()
        df = pd.read_excel(io.BytesIO(file_bytes))

        required_cols = ['Model', 'SL', 'ĐV nhận']
        for col in required_cols:
            if col not in df.columns:
                await update.message.reply_text(f"❌ File thiếu cột bắt buộc: {col}")
                return

        uid, models, error_msg = connect_odoo()
        if not uid:
            await update.message.reply_text(f"❌ Lỗi kết nối Odoo: {error_msg}")
            return

        # Lấy ID kho
        location_ids = find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD)
        hn_stock_id = location_ids['HN_STOCK']['id']
        hn_transit_id = location_ids['HN_TRANSIT']['id']
        hcm_stock_id = location_ids['HCM_STOCK']['id']

        results = []

        for _, row in df.iterrows():
            model = str(row['Model']).strip()
            sl_required = int(row['SL'])

            # tìm product theo model
            prod = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                'product.product', 'search_read',
                [[(PRODUCT_CODE_FIELD, '=', model)]],
                {'fields': ['id', 'display_name']}
            )

            if not prod:
                results.append({
                    'Model': model,
                    'SL yêu cầu': sl_required,
                    'Trạng thái': 'Không tìm thấy sản phẩm trong Odoo',
                    'Đề xuất nhập': 0
                })
                continue

            product_id = prod[0]['id']

            def get_qty(location_id):
                stock_info = models.execute_kw(
                    ODOO_DB, uid, ODOO_PASSWORD,
                    'product.product', 'read',
                    [[product_id]],
                    {'fields': ['qty_available'], 'context': {'location': location_id}}
                )
                if stock_info:
                    return int(round(stock_info[0].get('qty_available', 0)))
                return 0

            qty_hn = get_qty(hn_stock_id)
            qty_transit = get_qty(hn_transit_id)
            qty_hcm = get_qty(hcm_stock_id)

            total_hn = qty_hn + qty_transit

            if total_hn >= sl_required:
                status = 'Đủ'
                suggest = 0
            else:
                need = sl_required - total_hn
                suggest = min(need, qty_hcm)
                status = f"Thiếu {need}"

            results.append({
                'Model': model,
                'SL yêu cầu': sl_required,
                'Tồn HN (HN + Nhập)': total_hn,
                'Tồn HCM': qty_hcm,
                'Trạng thái': status,
                'Đề xuất nhập từ HCM': suggest,
                'ĐV nhận': row['ĐV nhận']
            })

        out_df = pd.DataFrame(results)

        buffer = io.BytesIO()
        out_df.to_excel(buffer, index=False, sheet_name='KiemTraTon')
        buffer.seek(0)

        await update.message.reply_document(
            document=InputFile(buffer, filename='ket_qua_kiem_tra_ton.xlsx'),
            caption="✅ Đã kiểm tra tồn kho theo file anh gửi."
        )

    except Exception as e:
        await update.message.reply_text(f"❌ Lỗi xử lý Excel: {e}")


# ---------------- Telegram Handlers ----------------
async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Đang kiểm tra kết nối odoo, xin chờ...")
    uid, _, error_msg = connect_odoo()
    if uid:
        await update.message.reply_text(f"✅ Thành công! kết nối odoo db: {ODOO_DB}")
    else:
        await update.message.reply_text(f"❌ Lỗi: {error_msg}")


async def excel_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⌛️ Đang tạo báo cáo đề xuất kéo hàng…")
    excel_buffer, n, msg = get_stock_data()

    if excel_buffer is None:
        await update.message.reply_text(f"❌ Lỗi: {msg}")
        return

    if n > 0:
        await update.message.reply_document(
            document=excel_buffer,
            filename='de_xuat_keo_hang.xlsx',
            caption=f"Đã tìm thấy {n} sản phẩm cần đề xuất."
        )
    else:
        await update.message.reply_text(
            f"Không có sản phẩm nào cần kéo hàng. (>= {TARGET_MIN_QTY})"
        )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.from_user.first_name
    await update.message.reply_text(
        f"Chào {name}!\n"
        "- Gõ mã SP để tra tồn.\n"
        "- Dùng /keohang để tạo báo cáo kéo hàng.\n"
        "- Dùng /checkexcel để gửi file Excel kiểm tra tồn.\n"
        "- Dùng /ping để kiểm tra kết nối."
    )


# ---------------- Main ----------------
def main():
    if not TELEGRAM_TOKEN:
        logger.error("Thiếu biến môi trường TOKEN")
        return

    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # xoá webhook
    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        asyncio.get_event_loop().run_until_complete(bot.delete_webhook())
    except:
        pass

    # Handlers cũ giữ nguyên
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", start_command))
    application.add_handler(CommandHandler("ping", ping_command))
    application.add_handler(CommandHandler("keohang", excel_report_command))

    # NEW — LỆNH KÍCH HOẠT CHẾ ĐỘ NHẬN FILE
    application.add_handler(CommandHandler("checkexcel", checkexcel_command))

    # NEW — HANDLER NHẬN FILE EXCEL
    application.add_handler(MessageHandler(filters.Document.ALL, excel_file_handler))

    # Handler cũ xử lý mã sản phẩm
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_product_code))

    logger.info("Bot đang chạy…")
    application.run_polling()


# ---------------- HTTP ping server ----------------
from http.server import BaseHTTPRequestHandler, HTTPServer

class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

    def log_message(self, *args):
        return


def start_http_server():
    try:
        server = HTTPServer(("0.0.0.0", 10001), PingHandler)
        server.serve_forever()
    except Exception as e:
        logger.error(f"Lỗi HTTP server: {e}")


threading.Thread(target=start_http_server, daemon=True).start()

if __name__ == "__main__":
    main()
