# Tệp: main.py (bot.py) - Sử dụng XML-RPC CHÍNH THỨC của Python

import os
import io
import logging
import pandas as pd
import ssl
import xmlrpc.client
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# --- 1. Cấu hình & Biến môi trường (LẤY TỪ RENDER) ---
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
# ODOO_URL phải là 'https://erp.nguonsongviet.vn/odoo'
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

# --- 2. Hàm kết nối Odoo (GIẢI PHÁP TỐI ƯU: XML-RPC) ---
def connect_odoo():
    """Thiết lập kết nối với Odoo bằng XML-RPC."""
    try:
        # **Xử lý SSL/Proxy:** Dùng ssl._create_unverified_context để bỏ qua lỗi SSL
        # URL dịch vụ common (dùng để login)
        common = xmlrpc.client.ServerProxy('{}/xmlrpc/2/common'.format(ODOO_URL), 
                                           context=ssl._create_unverified_context())
        
        # Gọi login để lấy UID (User ID)
        uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD, {})
        
        if not uid:
             logger.error("Đăng nhập thất bại: Tên đăng nhập/Mật khẩu/DB không đúng.")
             return None, None
        
        # URL dịch vụ object (dùng để CRUD dữ liệu)
        models = xmlrpc.client.ServerProxy('{}/xmlrpc/2/object'.format(ODOO_URL), 
                                            context=ssl._create_unverified_context())

        # Trả về các thông số cần thiết để gọi các method Odoo
        return uid, models
    
    except Exception as e:
        logger.error(f"Lỗi kết nối Odoo XML-RPC: {e}")
        return None, None

# --- 3. Hàm chính (Logic nghiệp vụ Odoo) ---
def get_stock_data():
    """
    Lấy dữ liệu tồn kho từ Odoo bằng XML-RPC.
    """
    uid, models = connect_odoo()
    if not uid:
        return None, 0

    try:
        # Lấy Location IDs
        location_ids = {}
        stock_location_id = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'stock.location', 'search_read', 
            [
                [('name', '=', LOCATION_MAP['HN_STOCK'])]
            ], 
            {'fields': ['id', 'name']}
        )
        if stock_location_id:
            location_ids['HN_STOCK'] = stock_location_id[0]['id']

        # Tương tự cho HCM_STOCK
        stock_location_id = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'stock.location', 'search_read', 
            [
                [('name', '=', LOCATION_MAP['HCM_STOCK'])]
            ], 
            {'fields': ['id', 'name']}
        )
        if stock_location_id:
            location_ids['HCM_STOCK'] = stock_location_id[0]['id']

        # Tương tự cho Kho nhập HN (Tìm theo tên "Kho nhập Hà Nội")
        stock_location_id = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'stock.location', 'search_read', 
            [
                [('name', '=', 'Kho nhập Hà Nội')]
            ], 
            {'fields': ['id', 'name']}
        )
        if stock_location_id:
            location_ids['HN_TRANSIT'] = stock_location_id[0]['id']
            
        if len(location_ids) < 3:
            logger.error("Không tìm thấy đủ 3 kho (HN, HCM, Nhập HN) trong Odoo.")
            return None, 0 

        # Lấy danh sách tồn kho (Quant)
        all_locations_ids = list(location_ids.values())
        quant_domain = [
            ('location_id', 'in', all_locations_ids),
            ('quantity', '>', 0)
        ]
        
        quant_data = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'stock.quant', 'search_read',
            [quant_domain],
            {'fields': ['product_id', 'location_id', 'quantity']}
        )
        
        # Lấy thông tin sản phẩm (Tên và Mã SP)
        product_ids = list(set([q['product_id'][0] for q in quant_data]))
        product_info = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'product.product', 'search_read',
            [[('id', 'in', product_ids)]],
            {'fields': ['display_name', PRODUCT_CODE_FIELD]}
        )
        product_map = {p['id']: p for p in product_info}

        # Xử lý logic nghiệp vụ và tính toán (Giống logic cũ)
        data = {}
        for q in quant_data:
            prod_id = q['product_id'][0]
            loc_id = q['location_id'][0]
            qty = q['quantity']
            
            if prod_id not in data and prod_id in product_map:
                data[prod_id] = {
                    'Mã SP': product_map[prod_id].get(PRODUCT_CODE_FIELD, 'N/A'),
                    'Tên SP': product_map[prod_id]['display_name'],
                    'Tồn Kho HN': 0,
                    'Tồn Kho HCM': 0,
                    'Kho Nhập HN': 0,
                    'Tổng Tồn HN': 0,
                    'Số Lượng Đề Xuất': 0
                }

            for key, loc_id_check in location_ids.items():
                if loc_id == loc_id_check:
                    if key == 'HN_STOCK':
                        data[prod_id]['Tồn Kho HN'] += qty
                    elif key == 'HCM_STOCK':
                        data[prod_id]['Tồn Kho HCM'] += qty
                    elif key == 'HN_TRANSIT':
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
        
        excel_buffer = io.BytesIO()
        df.to_excel(excel_buffer, index=False, sheet_name='DeXuatKeoHang')
        excel_buffer.seek(0)
        
        return excel_buffer, len(report_data)

    except Exception as e:
        logger.error(f"Lỗi khi truy vấn dữ liệu Odoo XML-RPC: {e}")
        return None, 0

# --- 4. Các hàm xử lý Bot Telegram ---
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

async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kiểm tra kết nối tới Odoo."""
    await update.message.reply_text("Đang kiểm tra kết nối Odoo, xin chờ...")
    
    uid, _ = connect_odoo() # Chỉ cần thử kết nối và login
    
    if uid:
        await update.message.reply_text(f"✅ **Thành công!** Kết nối Odoo DB: `{ODOO_DB}` tại `{ODOO_URL}`. User ID: `{uid}`", parse_mode='Markdown')
    else:
        logger.error("Lỗi kết nối Odoo hoặc đăng nhập. Vui lòng kiểm tra 4 biến môi trường (URL, DB, Username, Password).")
        await update.message.reply_text("❌ **Lỗi!** Không thể kết nối hoặc đăng nhập Odoo. Vui lòng kiểm tra lại 4 biến môi trường (URL, DB, Username, Password).")

async def handle_product_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tra cứu nhanh tồn kho theo Mã sản phẩm (default_code)."""
    product_code = update.message.text.strip().upper()
    
    uid, models = connect_odoo()
    if not uid:
        await update.message.reply_text("❌ Lỗi kết nối Odoo. Vui lòng thử lại sau.")
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
        await update.message.reply_text("❌ Có lỗi xảy ra khi truy vấn Odoo. Vui lòng kiểm tra log.")

async def excel_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tạo và gửi báo cáo Excel đề xuất kéo hàng."""
    
    await update.message.reply_text("⌛️ Đang xử lý dữ liệu và tạo báo cáo Excel. Tác vụ này có thể mất vài giây. Vui lòng chờ...")
    
    excel_buffer, item_count = get_stock_data() # Hàm này đã tự connect
    
    if excel_buffer is None:
        await update.message.reply_text("❌ Lỗi kết nối Odoo hoặc Lỗi nghiệp vụ. Không thể tạo báo cáo.")
        return
    
    if item_count > 0:
        await update.message.reply_document(
            document=excel_buffer,
            filename='De_Xuat_Keo_Hang.xlsx',
            caption=f"✅ Hoàn thành! Đã tìm thấy **{item_count}** sản phẩm cần kéo hàng từ HCM về HN để đạt tồn kho tối thiểu {TARGET_MIN_QTY}."
        )
    else:
        await update.message.reply_text(f"✅ Tuyệt vời! Tất cả sản phẩm hiện tại đã đạt hoặc vượt mức tồn kho tối thiểu {TARGET_MIN_QTY} tại kho HN (bao gồm cả hàng đi đường). Không cần kéo thêm hàng.")

# --- 5. Hàm chạy Bot chính ---
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
