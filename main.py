# Tệp: bot.py

import os
import io
import logging
import pandas as pd
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
from odoorpc.odoo import ODOO

# --- 1. Cấu hình & Biến môi trường (LẤY TỪ RENDER) ---
# Tự động lấy các giá trị nhạy cảm từ biến môi trường của Render
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN')
ODOO_URL = os.environ.get('ODOO_URL')
ODOO_DB = os.environ.get('ODOO_DB')
ODOO_USERNAME = os.environ.get('ODOO_USERNAME')
ODOO_PASSWORD = os.environ.get('ODOO_PASSWORD')
USER_ID_TO_SEND_REPORT = os.environ.get('USER_ID_TO_SEND_REPORT') # ID Telegram của bạn để nhận báo cáo tự động

# Cấu hình nghiệp vụ (Sử dụng mã kho bạn cung cấp)
TARGET_MIN_QTY = 50
LOCATION_MAP = {
    'HN_STOCK': '201/201', # Kho Hà Nội (Tồn kho thực tế)
    'HCM_STOCK': '124/124', # Kho HCM (Nguồn kéo hàng)
    'HN_TRANSIT': '201',     # Mã kho nhập Hà Nội (Hàng đi đường). Tên cần tìm là "Kho nhập Hà Nội"
}
PRODUCT_CODE_FIELD = 'default_code' # Trường mã sản phẩm dùng để tra cứu

# Cấu hình Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- 2. Hàm kết nối Odoo ---
def connect_odoo():
    """Thiết lập kết nối với Odoo bằng ODOO_URL, ODOO_DB, USERNAME và PASSWORD."""
    try:
        odoo_instance = ODOO(ODOO_URL, timeout=30)
        odoo_instance.login(ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD)
        return odoo_instance
    except Exception as e:
        logger.error(f"Lỗi kết nối hoặc đăng nhập Odoo: {e}")
        return None

# --- 3. Hàm chính (Logic nghiệp vụ Odoo) ---
def get_stock_data(odoo_instance):
    """
    Lấy dữ liệu tồn kho từ Odoo, tính toán và xuất ra DataFrame của pandas.
    """
    stock_quant = odoo_instance.env['stock.quant']
    product_product = odoo_instance.env['product.product']
    stock_location = odoo_instance.env['stock.location']
    
    # 1. Lấy IDs của các Location dựa trên mã kho bạn cung cấp
    location_ids = {}
    for key, code in LOCATION_MAP.items():
        # Đối với HN_TRANSIT, tìm bằng Tên (Kho nhập Hà Nội) để phân biệt với 201/201
        if key == 'HN_TRANSIT':
            domain = [('name', '=', 'Kho nhập Hà Nội')]
        # Đối với các kho khác, tìm bằng Mã (Name)
        else:
            domain = [('name', '=', code)]
            
        loc = stock_location.search_read(domain, fields=['id', 'name'])
        if loc:
            # Lấy ID của Location đầu tiên tìm được
            location_ids[key] = loc[0]['id']
        else:
            logger.warning(f"Không tìm thấy Location Code/Name: {code}")
            # Bỏ qua để hàm tiếp tục kiểm tra các kho khác
    
    if len(location_ids) < 3:
        logger.error("Không tìm thấy đủ 3 kho (HN, HCM, Nhập HN) trong Odoo.")
        return None, 0 # Trả về None nếu không tìm thấy đủ 3 kho quan trọng

    # 2. Lấy danh sách tồn kho (Quant) cho các kho quan trọng
    # Lấy tồn kho cho tất cả các sản phẩm có số lượng > 0 tại 3 kho
    all_locations_ids = list(location_ids.values())
    quant_domain = [
        ('location_id', 'in', all_locations_ids),
        ('quantity', '>', 0)
    ]
    
    quant_data = stock_quant.search_read(
        quant_domain, 
        fields=['product_id', 'location_id', 'quantity']
    )
    
    # 3. Lấy tên sản phẩm
    product_ids = list(set([q['product_id'][0] for q in quant_data]))
    product_info = product_product.search_read(
        [('id', 'in', product_ids)], 
        fields=['id', 'display_name', PRODUCT_CODE_FIELD]
    )
    product_map = {p['id']: p for p in product_info}

    # 4. Xử lý logic nghiệp vụ và tính toán
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

        # Cập nhật số lượng cho từng kho
        for key, loc_id_check in location_ids.items():
            if loc_id == loc_id_check:
                if key == 'HN_STOCK':
                    data[prod_id]['Tồn Kho HN'] += qty
                elif key == 'HCM_STOCK':
                    data[prod_id]['Tồn Kho HCM'] += qty
                elif key == 'HN_TRANSIT':
                    data[prod_id]['Kho Nhập HN'] += qty
                    
    # 5. Tính toán đề xuất kéo hàng
    report_data = []
    for prod_id, info in data.items():
        # Tổng Tồn HN = Tồn Kho HN (Thực tế) + Kho Nhập HN (Hàng đi đường)
        info['Tổng Tồn HN'] = info['Tồn Kho HN'] + info['Kho Nhập HN']
        
        if info['Tổng Tồn HN'] < TARGET_MIN_QTY:
            # Số lượng cần kéo để đạt MIN QTY
            qty_needed = TARGET_MIN_QTY - info['Tổng Tồn HN']
            
            # Số lượng đề xuất = MIN(Số lượng cần, Tồn kho HCM)
            info['Số Lượng Đề Xuất'] = min(qty_needed, info['Tồn Kho HCM'])
            
            # Chỉ thêm vào báo cáo nếu có đề xuất > 0
            if info['Số Lượng Đề Xuất'] > 0:
                report_data.append(info)
                
    # 6. Tạo DataFrame và xuất file Excel
    df = pd.DataFrame(report_data)
    
    # Sắp xếp lại cột theo đúng thứ tự yêu cầu
    COLUMNS_ORDER = ['Mã SP', 'Tên SP', 'Tồn Kho HN', 'Tồn Kho HCM', 'Kho Nhập HN', 'Số Lượng Đề Xuất']
    df = df[COLUMNS_ORDER]
    
    # Sử dụng io.BytesIO để tạo file Excel trong bộ nhớ (không cần lưu ra đĩa)
    excel_buffer = io.BytesIO()
    df.to_excel(excel_buffer, index=False, sheet_name='DeXuatKeoHang')
    excel_buffer.seek(0)
    
    return excel_buffer, len(report_data)

# --- 4. Các hàm xử lý Bot Telegram ---

# Xử lý lệnh /start và /help
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

# Xử lý lệnh /ping
async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kiểm tra kết nối tới Odoo."""
    await update.message.reply_text("Đang kiểm tra kết nối Odoo, xin chờ...")
    odoo = connect_odoo()
    if odoo:
        await update.message.reply_text(f"✅ **Thành công!** Kết nối Odoo DB: `{ODOO_DB}` tại `{ODOO_URL}`.", parse_mode='Markdown')
    else:
        await update.message.reply_text("❌ **Lỗi!** Không thể kết nối hoặc đăng nhập Odoo. Vui lòng kiểm tra lại 4 biến môi trường (URL, DB, Username, Password).")

# Xử lý tính năng tra cứu nhanh (Mã sản phẩm)
async def handle_product_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tra cứu nhanh tồn kho theo Mã sản phẩm (default_code)."""
    product_code = update.message.text.strip().upper()
    
    odoo = connect_odoo()
    if not odoo:
        await update.message.reply_text("❌ Lỗi kết nối Odoo. Vui lòng thử lại sau.")
        return

    product_model = odoo.env['product.product']
    # Tìm sản phẩm theo trường default_code
    domain = [(PRODUCT_CODE_FIELD, '=', product_code)]
    
    try:
        products = product_model.search_read(
            domain, 
            fields=['display_name', 'qty_available', 'virtual_available']
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
        logger.error(f"Lỗi khi tra cứu sản phẩm: {e}")
        await update.message.reply_text("❌ Có lỗi xảy ra khi truy vấn Odoo. Vui lòng kiểm tra log.")

# Xử lý lệnh /keohang (Xuất báo cáo Excel)
async def excel_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tạo và gửi báo cáo Excel đề xuất kéo hàng."""
    
    await update.message.reply_text("⌛️ Đang xử lý dữ liệu và tạo báo cáo Excel. Tác vụ này có thể mất vài giây. Vui lòng chờ...")
    
    odoo = connect_odoo()
    if not odoo:
        await update.message.reply_text("❌ Lỗi kết nối Odoo. Không thể tạo báo cáo.")
        return
    
    try:
        excel_buffer, item_count = get_stock_data(odoo)
        
        if excel_buffer is None:
             await update.message.reply_text("❌ Lỗi nghiệp vụ Odoo: Không thể tìm thấy đủ các kho (HN, HCM, Kho Nhập HN) hoặc kết nối bị lỗi trong quá trình xử lý. Vui lòng kiểm tra log.")
             return
        
        if item_count > 0:
            # Gửi file Excel
            await update.message.reply_document(
                document=excel_buffer,
                filename='De_Xuat_Keo_Hang.xlsx',
                caption=f"✅ Hoàn thành! Đã tìm thấy **{item_count}** sản phẩm cần kéo hàng từ HCM về HN để đạt tồn kho tối thiểu {TARGET_MIN_QTY}."
            )
        else:
            await update.message.reply_text(f"✅ Tuyệt vời! Tất cả sản phẩm hiện tại đã đạt hoặc vượt mức tồn kho tối thiểu {TARGET_MIN_QTY} tại kho HN (bao gồm cả hàng đi đường). Không cần kéo thêm hàng.")

    except Exception as e:
        logger.error(f"Lỗi khi tạo báo cáo Excel: {e}")
        await update.message.reply_text(f"❌ Đã xảy ra lỗi nghiêm trọng khi xử lý báo cáo: {e}")

# Xử lý tính năng TỰ ĐỘNG BÁO CÁO HÀNG NGÀY (Lệnh này chỉ dùng để kích hoạt báo cáo cho mục đích Cron Job)
async def daily_report_webhook(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pass

# --- 5. Hàm chạy Bot chính ---
def main():
    """Chạy bot."""
    if not TELEGRAM_TOKEN or not ODOO_URL or not ODOO_DB or not ODOO_USERNAME or not ODOO_PASSWORD:
        logger.error("Vui lòng thiết lập TẤT CẢ các biến môi trường cần thiết (TOKEN, URL, DB, USER, PASS).")
        return
        
    # Xây dựng ứng dụng bot Telegram
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    # Thêm các Handler cho các lệnh
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", start_command))
    application.add_handler(CommandHandler("ping", ping_command))
    application.add_handler(CommandHandler("keohang", excel_report_command))

    # Handler cho tin nhắn (dùng để tra cứu mã sản phẩm)
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_product_code))
    
    # Khởi chạy bot (polling mode)
    logger.info("Bot đang khởi chạy ở chế độ Polling (Render Free Tier).")
    # Tắt tính năng tự động cập nhật URL Webhook vì chúng ta dùng Polling (đơn giản hơn)
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == '__main__':
    main()
