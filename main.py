# Tệp: main.py - Phiên bản DỨT ĐIỂM HOÀN TOÀN: Fix lỗi cộng dồn tồn kho chi tiết bằng ID và xử lý FLOAT

import os
import io
import logging
import pandas as pd
import ssl
import xmlrpc.client
from urllib.parse import urlparse
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# --- 1. Cấu hình & Biến môi trường ---
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
ODOO_URL_RAW = os.environ.get('ODOO_URL').rstrip('/')
if ODOO_URL_RAW.lower().endswith('/odoo'):
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

logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)


# --- 2. Hàm kết nối Odoo ---
def connect_odoo():
    try:
        common_url = f'{ODOO_URL_FINAL}/xmlrpc/2/common'
        context = ssl._create_unverified_context()

        common = xmlrpc.client.ServerProxy(common_url, context=context)
        uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD, {})

        if not uid:
            error_message = f"đăng nhập thất bại (uid=0). kiểm tra lại user/pass/db: {ODOO_USERNAME} / {ODOO_DB}."
            return None, None, error_message

        models = xmlrpc.client.ServerProxy(f'{ODOO_URL_FINAL}/xmlrpc/2/object', context=context)
        return uid, models, "kết nối thành công."

    except xmlrpc.client.ProtocolError as pe:
        error_message = f"lỗi giao thức odoo (400 bad request?): {pe}. url: {common_url}"
        return None, None, error_message
    except Exception as e:
        error_message = f"lỗi kết nối odoo xml-rpc: {e}. url: {common_url}"
        return None, None, error_message


# --- Helper ---
def find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD):
    location_ids = {}

    def search_location(name_code):
        loc_data = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'stock.location', 'search_read',
            [[('display_name', 'ilike', name_code)]],
            {'fields': ['id', 'display_name']}
        )
        if not loc_data:
            return None

        preferred_loc = next((l for l in loc_data if name_code.lower() in l['display_name'].lower()), loc_data[0])
        if preferred_loc and 'id' in preferred_loc and 'display_name' in preferred_loc:
            return {'id': preferred_loc['id'], 'name': preferred_loc['display_name']}
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
    special_chars = ['\\', '_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    text = str(text)
    for char in special_chars:
        text = text.replace(char, f'\\{char}')
    return text.replace('\\`', '`')


# --- 3. Hàm lấy dữ liệu kéo hàng ---
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
        df = df[COLUMNS_ORDER]
        for col in ['Tồn Kho HN', 'Tồn Kho HCM', 'Kho Nhập HN', 'Số Lượng Đề Xuất']:
            df[col] = df[col].apply(lambda x: int(round(x)))

        excel_buffer = io.BytesIO()
        df.to_excel(excel_buffer, index=False, sheet_name='DeXuatKeoHang')
        excel_buffer.seek(0)
        return excel_buffer, len(report_data), "thành công"

    except Exception as e:
        error_msg = f"lỗi khi truy vấn dữ liệu odoo xml-rpc: {e}"
        return None, 0, error_msg


# --- 4. Tra cứu sản phẩm ---
async def handle_product_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    product_code = update.message.text.strip().upper()
    await update.message.reply_text(f"đang tra tồn cho `{product_code}`, vui lòng chờ!", parse_mode='Markdown')

    uid, models, error_msg = connect_odoo()
    if not uid:
        await update.message.reply_text(f"❌ lỗi kết nối odoo. chi tiết: `{error_msg.lower()}`", parse_mode='Markdown')
        return

    try:
        location_ids = find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD)
        hn_transit_id = location_ids.get('HN_TRANSIT', {}).get('id')
        hn_stock_id = location_ids.get('HN_STOCK', {}).get('id')
        hcm_stock_id = location_ids.get('HCM_STOCK', {}).get('id')

        product_domain = [(PRODUCT_CODE_FIELD, '=', product_code)]
        products = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'product.product', 'search_read',
            [product_domain],
            {'fields': ['display_name', 'id']}
        )

        if not products:
            await update.message.reply_text(f"❌ không tìm thấy sản phẩm nào có mã `{product_code}`.")
            return

        product = products[0]
        product_id = product['id']
        product_name = product['display_name']

        def get_qty_available(location_id):
            if not location_id:
                return 0
            stock_product_info = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, 'product.product', 'read',
                [[product_id]],
                {'fields': ['qty_available'], 'context': {'location': location_id}}
            )
            return int(round(stock_product_info[0].get('qty_available', 0.0))) if stock_product_info and stock_product_info[0] else 0

        hn_stock_qty = get_qty_available(hn_stock_id)
        hn_transit_qty = get_qty_available(hn_transit_id)
        hcm_stock_qty = get_qty_available(hcm_stock_id)

        # ✅ FIX: Cộng dồn đúng "Có hàng" (quantity)
        quant_domain_all = [('product_id', '=', product_id), ('quantity', '>', 0)]
        quant_data_all = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'stock.quant', 'search_read',
            [quant_domain_all],
            {'fields': ['location_id', 'quantity']}
        )

        location_ids_all = list(set([q['location_id'][0] for q in quant_data_all]))
        location_info = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'stock.location', 'search_read',
            [[('id', 'in', location_ids_all)]],
            {'fields': ['id', 'display_name', 'usage']}
        )
        location_map = {loc['id']: loc for loc in location_info}

        stock_by_loc_id = {}
        for q in quant_data_all:
            loc_id = q['location_id'][0]
            qty = float(q.get('quantity', 0.0))
            loc_data = location_map.get(loc_id, {})
            loc_usage = loc_data.get('usage', 'internal')

            if qty > 0 and loc_usage in ('internal', 'transit'):
                stock_by_loc_id[loc_id] = stock_by_loc_id.get(loc_id, 0.0) + qty

        all_stock_details = {}
        for loc_id, qty in stock_by_loc_id.items():
            rounded_qty = int(round(qty))
            if rounded_qty > 0:
                loc_name = location_map.get(loc_id, {}).get('display_name', f"n/a (ID: {loc_id})")
                all_stock_details[loc_name] = rounded_qty

        total_hn_stock = hn_stock_qty + hn_transit_qty
        recommendation_qty = 0
        if total_hn_stock < TARGET_MIN_QTY:
            qty_needed = TARGET_MIN_QTY - total_hn_stock
            recommendation_qty = min(qty_needed, hcm_stock_qty)

        recommendation_text = f"=> đề xuất nhập thêm `{int(recommendation_qty)}` sp để hn đủ tồn `{TARGET_MIN_QTY}` sản phẩm." if recommendation_qty > 0 else f"=> tồn kho hn đã đủ (`{int(total_hn_stock)}`/{TARGET_MIN_QTY} sp)."

        detail_stock_list = []
        priority_items = []
        for p_code in PRIORITY_LOCATIONS:
            for name, qty in all_stock_details.items():
                if p_code.lower() in name.lower() and name not in [item[0] for item in priority_items]:
                    safe_name = escape_markdown(name.lower())
                    priority_items.append((name, f"**{safe_name}**: `{qty}`"))
                    break

        priority_names = [name for name, _ in priority_items]
        other_items = []
        for name, qty in sorted(all_stock_details.items()):
            if name not in priority_names:
                safe_name = escape_markdown(name.lower())
                other_items.append((name, f"{safe_name}: `{qty}`"))

        detail_stock_list.extend([item[1] for item in priority_items])
        detail_stock_list.extend([item[1] for item in other_items])

        detail_stock_content = '\n'.join(detail_stock_list) if detail_stock_list else 'không có tồn kho chi tiết lớn hơn 0.'

        message = f"""
1/ {product_name}
Tồn kho HN: `{int(hn_stock_qty)}`
Tồn kho HCM: `{int(hcm_stock_qty)}`
Tồn kho nhập Hà Nội: `{int(hn_transit_qty)}`
{recommendation_text}

2/ Tồn kho chi tiết (có hàng):
{detail_stock_content}
"""
        await update.message.reply_text(message.strip(), parse_mode='Markdown')

    except Exception as e:
        logger.error(f"lỗi khi tra cứu sản phẩm xml-rpc: {e}")
        await update.message.reply_text(f"❌ Có lỗi xảy ra khi truy vấn odoo: `{escape_markdown(str(e))}`.", parse_mode='Markdown')


# --- 5. Telegram handlers ---
async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("đang kiểm tra kết nối odoo, xin chờ...")
    uid, _, error_msg = connect_odoo()
    if uid:
        await update.message.reply_text(f"✅ **Thành công!** kết nối odoo db: `{ODOO_DB}` tại `{ODOO_URL_RAW}`. user id: `{uid}`", parse_mode='Markdown')
    else:
        await update.message.reply_text(f"❌ **Lỗi!** không thể kết nối odoo.\nchi tiết: `{error_msg.lower()}`", parse_mode='Markdown')


async def excel_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⌛️ Đang xử lý dữ liệu và tạo báo cáo Excel. Vui lòng chờ...")
    excel_buffer, item_count, error_msg = get_stock_data()
    if excel_buffer is None:
        await update.message.reply_text(f"❌ Lỗi kết nối odoo hoặc lỗi nghiệp vụ.\nchi tiết: `{error_msg.lower()}`", parse_mode='Markdown')
        return
    if item_count > 0:
        await update.message.reply_document(
            document=excel_buffer,
            filename='de_xuat_keo_hang.xlsx',
            caption=f"✅ Hoàn thành! đã tìm thấy **{item_count}** sản phẩm cần kéo hàng."
        )
    else:
        await update.message.reply_text(f"✅ Tất cả sản phẩm đã đạt mức tồn kho tối thiểu {TARGET_MIN_QTY} tại kho hn.")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_name = update.message.from_user.first_name
    welcome_message = (
        f"Chào mừng **{user_name}** đến với odoo stock bot! 🤖\n\n"
        "1. Gõ mã sp (vd: `i-78`) để tra tồn.\n"
        "2. Dùng lệnh `/keohang` để tạo báo cáo excel.\n"
        "3. Dùng lệnh `/ping` để kiểm tra kết nối."
    )
    await update.message.reply_text(welcome_message.lower(), parse_mode='Markdown')


def main():
    if not TELEGRAM_TOKEN or not ODOO_URL_RAW or not ODOO_DB or not ODOO_USERNAME or not ODOO_PASSWORD:
        logger.error("vui lòng thiết lập đầy đủ biến môi trường.")
        return

    application = Application.builder().token(TELEGRAM_TOKEN).build()
    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        bot.delete_webhook()
    except Exception as e:
        logger.warning(f"lỗi khi xóa webhook: {e}")

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", start_command))
    application.add_handler(CommandHandler("ping", ping_command))
    application.add_handler(CommandHandler("keohang", excel_report_command))
