# Tệp: main.py (bot.py) - Phiên bản CUỐI CÙNG (XML-RPC Tối ưu)

import os
import io
import logging
import pandas as pd
import ssl
import xmlrpc.client
from urllib.parse import urlparse 
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# --- 1. Cấu hình & Biến môi trường ---
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
ODOO_URL = os.environ.get('ODOO_URL') 
ODOO_DB = os.environ.get('ODOO_DB')
ODOO_USERNAME = os.environ.get('ODOO_USERNAME')
ODOO_PASSWORD = os.environ.get('ODOO_PASSWORD')
USER_ID_TO_SEND_REPORT = os.environ.get('USER_ID_TO_SEND_REPORT')

# Cấu hình nghiệp vụ (Đã rà soát)
TARGET_MIN_QTY = 50
LOCATION_MAP = {
    'HN_STOCK': '201/201', 
    'HCM_STOCK': '124/124', 
    'HN_TRANSIT': '201',     
}
PRODUCT_CODE_FIELD = 'default_code'

# Cấu hình Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- 2. Hàm kết nối Odoo (Tách URL + XML-RPC) ---
def connect_odoo():
    """Thiết lập kết nối với Odoo bằng XML-RPC, xử lý proxy URL."""
    
    # Base URL chỉ còn scheme và netloc (ví dụ: https://erp.nguonsongviet.vn)
    try:
        parsed_url = urlparse(ODOO_URL)
        base_url_for_rpc = f"{parsed_url.scheme}://{parsed_url.netloc}" 
    except Exception as e:
        logger.error(f"Lỗi phân tích cú pháp ODOO_URL: {e}")
        return None, None, "Lỗi phân tích cú pháp URL."
    
    try:
        # 1. Kết nối Common Service (dùng để login)
        common_url = '{}/xmlrpc/2/common'.format(base_url_for_rpc)
        common = xmlrpc.client.ServerProxy(common_url, context=ssl._create_unverified_context())
        
        # 2. Login và lấy UID
        uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD, {})
        
        if not uid:
             error_message = f"Đăng nhập thất bại (UID=0). Kiểm tra lại User/Pass/DB: {ODOO_USERNAME} / {ODOO_DB}."
             logger.error(error_message)
             return None, None, error_message
        
        # 3. Kết nối Object Service (dùng để CRUD dữ liệu)
        models = xmlrpc.client.ServerProxy('{}/xmlrpc/2/object'.format(base_url_for_rpc), 
                                            context=ssl._create_unverified_context())

        # Thành công: Trả về UID, Models, và thông báo thành công
        return uid, models, "Kết nối thành công."
    
    except xmlrpc.client.ProtocolError as pe:
        error_message = f"Lỗi Giao thức Odoo (400 Bad Request?): {pe}. URL: {common_url}"
        logger.error(error_message)
        return None, None, error_message
    except Exception as e:
        error_message = f"Lỗi Kết nối Odoo XML-RPC: {e}. URL: {common_url}"
        logger.error(error_message)
        return None, None, error_message

# --- 3. Hàm chính (Logic nghiệp vụ Odoo) ---
def get_stock_data():
    """Lấy dữ liệu tồn kho từ Odoo bằng XML-RPC."""
    uid, models, error_msg = connect_odoo()
    if not uid:
        return None, 0, error_msg

    try:
        # ⚠️ Phần truy vấn Odoo (search_read) KHÔNG BỊ LỖI trong các lần test trước 
        # (Chứng minh qua tra cứu I-78) nên được giữ nguyên.

        # 1. Lấy Location IDs
        location_ids = {}
        # ... (Phần code này giống hệt phiên bản trước, được chứng minh hoạt động) ...
        # Lấy HN_STOCK
        loc_data = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'stock.location', 'search_read', 
            [[('name', '=', LOCATION_MAP['HN_STOCK'])]], 
            {'fields': ['id']}
        )
        if loc_data: location_ids['HN_STOCK'] = loc_data[0]['id']

        # Lấy HCM_STOCK
        loc_data = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'stock.location', 'search_read', 
            [[('name', '=', LOCATION_MAP['HCM_STOCK'])]], 
            {'fields': ['id']}
        )
        if loc_data: location_ids['HCM_STOCK'] = loc_data[0]['id']

        # Lấy Kho nhập HN (Tìm theo tên "Kho nhập Hà Nội")
        loc_data = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'stock.location', 'search_read', 
            [[('name', '=', 'Kho nhập Hà Nội')]], 
            {'fields': ['id']}
        )
        if loc_data: location_ids['HN_TRANSIT'] = loc_data[0]['id']
            
        if len(location_ids) < 3:
            error_msg = "Không tìm thấy đủ 3 kho (HN, HCM, Nhập HN) trong Odoo."
            logger.error(error_msg)
            return None, 0, error_msg 

        # 2. Lấy danh sách tồn kho (Quant)
        all_locations_ids = list(location_ids.values())
        quant_domain = [('location_id', 'in', all_locations_ids), ('quantity', '>', 0)]
        quant_data = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'stock.quant', 'search_read',
            [quant_domain],
            {'fields': ['product_id', 'location_id', 'quantity']}
        )
        
        # 3. Lấy thông tin sản phẩm (Tên và Mã SP)
        product_ids = list(set([q['product_id'][0] for q in quant_data]))
        product_info = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'product.product', 'search_read',
            [[('id', 'in', product_ids)]],
            {'fields': ['display_name', PRODUCT_CODE_FIELD]}
        )
        product_map = {p['id']: p for p in product_info}

        # 4. Xử lý logic nghiệp vụ và tính toán (Tính đề xuất)
        data = {}
        for q in quant_data:
            prod_id = q['product_id'][0]
            loc_id = q['location_id'][0]
            qty = q['quantity']
            
            if prod_id not in data and prod_id in product_map:
                data[prod_id] = {
                    'Mã SP': product_map[prod_id].get(PRODUCT_CODE_FIELD, 'N/A'),
                    'Tên SP': product_map[prod_id]['display_name'],
                    'Tồn Kho HN': 0, 'Tồn Kho HCM': 0, 'Kho Nhập HN': 0, 'Tổng Tồn HN': 0, 'Số Lượng Đề Xuất': 0
                }

            for key, loc_id_check in location_ids.items():
                if loc_id == loc_id_check:
                    if key == 'HN_STOCK': data[prod_id]['Tồn Kho HN'] += qty
                    elif key == 'HCM_STOCK': data[prod_id]['Tồn Kho HCM'] += qty
                    elif key == 'HN_TRANSIT': data[prod_id]['Kho Nhập HN'] += qty
                        
        report_data = []
        for prod_id, info in data.items():
            info['Tổng Tồn HN'] = info['Tồn Kho HN'] + info['Kho Nhập HN']
            if info['Tổng Tồn HN'] < TARGET_MIN_QTY:
                qty_needed = TARGET_MIN_QTY - info['Tổng Tồn HN']
                info['Số Lượng Đề Xuất'] = min(qty_needed, info['Tồn Kho HCM'])
                if info['Số Lượng Đề Xuất'] > 0: report_data.append(info)
                    
        df = pd.DataFrame(report_data)
        COLUMNS_ORDER = ['Mã SP', 'Tên SP', 'Tồn Kho HN', 'Tồn Kho HCM', 'Kho Nhập HN', 'Số Lượng Đề Xuất']
        df = df[COLUMNS_ORDER]
        
        excel_buffer = io.BytesIO()
        df.to_excel(excel_buffer, index=False, sheet_name='DeXuatKeoHang')
        excel_buffer.seek(0)
        
        return excel_buffer, len(report_data), "Thành công"

    except Exception as e:
        error_msg = f"Lỗi khi truy vấn dữ liệu Odoo XML-RPC: {e}"
        logger.error(error_msg)
        return None, 0, error_msg

# --- 4. Các hàm xử lý Bot Telegram ---

async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kiểm tra kết nối tới Odoo."""
    await update.message.reply_text("Đang kiểm tra kết nối Odoo, xin chờ...")
    
    # ⚠️ THAY ĐỔI: Nhận cả 3 giá trị trả về
    uid, _, error_msg = connect_odoo() 
    
    if uid:
        await update.message.reply_text(
            f"✅ **Thành công!** Kết nối Odoo DB: `{ODOO_DB}` tại `{ODOO_URL}`. User ID: `{uid}`", 
            parse_mode='Markdown'
        )
    else:
        # Nếu login thất bại, dùng chính error_msg để báo cáo
        final_error = f"❌ **Lỗi!** Không thể kết nối hoặc đăng nhập Odoo.\n\nChi tiết lỗi: `{error_msg}`"
        await update.message.reply_text(final_error, parse_mode='Markdown')

async def handle_product_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tra cứu nhanh tồn kho theo Mã sản phẩm (default_code)."""
    product_code = update.message.text.strip().upper()
    
    uid, models, error_msg = connect_odoo()
    if not uid:
        await update.message.reply_text(f"❌ Lỗi kết nối Odoo. Chi tiết: `{error_msg}`", parse_mode='Markdown')
        return

    domain = [(PRODUCT_CODE_FIELD, '=', product_code)]
    
    try:
        products = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'product.product', 'search_read',
            [domain],
            {'fields': ['display_name', 'qty_available', 'virtual_available']}
        )
        
        if products:
            product = products[0]
            message = (
                f"🔎 **Thông tin sản phẩm:**\n"
                f"- **Tên SP:** {product['display_name']}\n"
                f"- **Mã SP:** `{product_code}`\n"
                f"- **Tồn Kho Thực Tế (Tổng):** `{int(product.get('qty_available', 0))}`\n"
                f"- **Tồn Kho Dự Báo (Tổng):** `{int(product.get('virtual_available', 0))}`\n\n"
                f"_(Sử dụng lệnh /keohang để xem tồn kho chi tiết theo từng kho và đề xuất kéo hàng.)_"
            )
            await update.message.reply_text(message, parse_mode='Markdown')
        else:
            await update.message.reply_text(f"❌ Không tìm thấy sản phẩm nào có mã `{product_code}`.")
    
    except Exception as e:
        logger.error(f"Lỗi khi tra cứu sản phẩm XML-RPC: {e}")
        await update.message.reply_text(f"❌ Có lỗi xảy ra khi truy vấn Odoo: {e}")

async def excel_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tạo và gửi báo cáo Excel đề xuất kéo hàng."""
    
    await update.message.reply_text("⌛️ Đang xử lý dữ liệu và tạo báo cáo Excel. Tác vụ này có thể mất vài giây. Vui lòng chờ...")
    
    # ⚠️ THAY ĐỔI: Nhận cả 3 giá trị trả về
    excel_buffer, item_count, error_msg = get_stock_data() 
    
    if excel_buffer is None:
        await update.message.reply_text(f"❌ Lỗi kết nối Odoo hoặc Lỗi nghiệp vụ. Không thể tạo báo cáo.\n\nChi tiết lỗi: `{error_msg}`", parse_mode='Markdown')
        return
    
    if item_count > 0:
        await update.message.reply_document(
            document=excel_buffer,
            filename='De_Xuat_Keo_Hang.xlsx',
            caption=f"✅ Hoàn thành! Đã tìm thấy **{item_count}** sản phẩm cần kéo hàng từ HCM về HN để đạt tồn kho tối thiểu {TARGET_MIN_QTY}."
        )
    else:
        await update.message.reply_text(f"✅ Tuyệt vời! Tất cả sản phẩm hiện tại đã đạt hoặc vượt mức tồn kho tối thiểu {TARGET_MIN_QTY} tại kho HN (bao gồm cả hàng đi đường). Không cần kéo thêm hàng.")

# (Giữ nguyên các hàm còn lại: start_command và main)
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gửi tin nhắn chào mừng và hướng dẫn."""
    user_name = update.message.from_user.first_name
    welcome_message = (
        f"Chào mừng **{user_name}** đến với Odoo Stock Bot! 🤖\n\n"
        "Tôi có thể thực hiện 3 tác vụ sau:\n"
        "1. **Tra cứu nhanh:** Gõ bất kỳ mã sản phẩm nào (ví dụ: `I-78`). Tôi sẽ trả về tồn kho nhanh (Tổng).\n"
        "2. **Báo cáo kéo hàng (Excel):** Dùng lệnh `/keohang` để nhận file Excel thống kê các sản phẩm cần kéo từ HCM về HN.\n"
        "3. **Kiểm tra kết nối:** Dùng lệnh `/ping` để kiểm tra kết nối Odoo."
    )
    await update.message.reply_text(welcome_message, parse_mode='Markdown')

def main():
    """Chạy bot."""
    if not TELEGRAM_TOKEN or not ODOO_URL or not ODOO_DB or not ODOO_USERNAME or not ODOO_PASSWORD:
        logger.error("Vui lòng thiết lập TẤT CẢ các biến môi trường cần thiết (TOKEN, URL, DB, USER, PASS).")
        return
        
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", start_command))
    application.add_handler(CommandHandler("ping", ping_command))
    application.add_handler(CommandHandler("keohang", excel_report_command))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_product_code))
    
    logger.info("Bot đang khởi chạy ở chế độ Polling (Render Free Tier).")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == '__main__':
    main()
