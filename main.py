# Tệp: main.py - Phiên bản HOÀN CHỈNH: Sửa lỗi cú pháp f-string và cập nhật định dạng tra cứu

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

# Cấu hình nghiệp vụ
TARGET_MIN_QTY = 50
# NOTE: Đã chuyển sang tìm kiếm theo tên/mã code để bắt tên đầy đủ trong Odoo.
LOCATION_MAP = {
    'HN_STOCK_CODE': '201/201', 
    'HCM_STOCK_CODE': '124/124', 
    'HN_TRANSIT_NAME': 'Kho nhập Hà Nội', 
}
PRODUCT_CODE_FIELD = 'default_code'

# Cấu hình Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# --- 2. Hàm kết nối Odoo ---
def connect_odoo():
    """Thiết lập kết nối với Odoo bằng XML-RPC, xử lý proxy URL."""
    try:
        parsed_url = urlparse(ODOO_URL)
        base_url_for_rpc = f"{parsed_url.scheme}://{parsed_url.netloc}" 
    except Exception as e:
        error_message = f"Lỗi phân tích cú pháp ODOO_URL: {e}"
        return None, None, error_message
    
    common_url = '{}/xmlrpc/2/common'.format(base_url_for_rpc)
    try:
        common = xmlrpc.client.ServerProxy(common_url, context=ssl._create_unverified_context())
        uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD, {})
        
        if not uid:
             error_message = f"Đăng nhập thất bại (UID=0). Kiểm tra lại User/Pass/DB: {ODOO_USERNAME} / {ODOO_DB}."
             return None, None, error_message
        
        models = xmlrpc.client.ServerProxy('{}/xmlrpc/2/object'.format(base_url_for_rpc), 
                                            context=ssl._create_unverified_context())

        return uid, models, "Kết nối thành công."
    
    except xmlrpc.client.ProtocolError as pe:
        error_message = f"Lỗi Giao thức Odoo (400 Bad Request?): {pe}. URL: {common_url}"
        return None, None, error_message
    except Exception as e:
        error_message = f"Lỗi Kết nối Odoo XML-RPC: {e}. URL: {common_url}"
        return None, None, error_message

# --- 3. Hàm chính (Logic nghiệp vụ Odoo) ---
def get_stock_data():
    """Lấy dữ liệu tồn kho từ Odoo bằng XML-RPC."""
    uid, models, error_msg = connect_odoo()
    if not uid:
        return None, 0, error_msg 

    try:
        location_ids = {}
        
        # Lấy HN_STOCK (201/201) - Dùng ILIKE để tìm kiếm linh hoạt hơn
        loc_data = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'stock.location', 'search_read', 
            [[('name', 'ilike', LOCATION_MAP['HN_STOCK_CODE'])]], 
            {'fields': ['id', 'display_name']}
        )
        if loc_data: location_ids['HN_STOCK'] = {'id': loc_data[0]['id'], 'name': loc_data[0]['display_name']}

        # Lấy HCM_STOCK (124/124) - Dùng ILIKE
        loc_data = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'stock.location', 'search_read', 
            [[('name', 'ilike', LOCATION_MAP['HCM_STOCK_CODE'])]], 
            {'fields': ['id', 'display_name']}
        )
        if loc_data: location_ids['HCM_STOCK'] = {'id': loc_data[0]['id'], 'name': loc_data[0]['display_name']}

        # Lấy Kho nhập HN (Tìm chính xác theo tên)
        loc_data = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'stock.location', 'search_read', 
            [[('name', '=', LOCATION_MAP['HN_TRANSIT_NAME'])]], 
            {'fields': ['id', 'display_name']}
        )
        if loc_data: location_ids['HN_TRANSIT'] = {'id': loc_data[0]['id'], 'name': loc_data[0]['display_name']}
            
        if len(location_ids) < 3:
            error_msg = f"Không tìm thấy đủ 3 kho cần thiết. Đã tìm thấy: {list(location_ids.keys())} - ID: {location_ids}"
            logger.error(error_msg)
            return None, 0, error_msg 

        # ... (Phần còn lại của logic nghiệp vụ không thay đổi) ...

        # Lấy danh sách tồn kho (Quant)
        all_locations_ids = [v['id'] for v in location_ids.values()]
        quant_domain = [('location_id', 'in', all_locations_ids), ('quantity', '>', 0)]
        
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
        
        # Lấy bản đồ ID Location
        location_id_to_name = {v['id']: v['name'] for v in location_ids.values()}

        # Xử lý logic nghiệp vụ và tính toán
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

            # Map quantity to correct key
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
        return None, 0, error_msg

# --- 4. CẬP NHẬT: Định dạng lại tin nhắn tra cứu sản phẩm (ĐÃ FIX LỖI SYNTAX) ---
async def handle_product_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Tra cứu nhanh tồn kho theo Mã sản phẩm (default_code).
    Định dạng lại theo yêu cầu mới.
    """
    product_code = update.message.text.strip().upper()
    await update.message.reply_text(f"Đang tra tồn cho sản phẩm `{product_code}`...", parse_mode='Markdown')

    uid, models, error_msg = connect_odoo()
    if not uid:
        await update.message.reply_text(f"❌ Lỗi kết nối Odoo. Chi tiết: `{error_msg}`", parse_mode='Markdown')
        return

    try:
        # Lấy thông tin sản phẩm và tồn kho tổng
        product_domain = [(PRODUCT_CODE_FIELD, '=', product_code)]
        products = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'product.product', 'search_read',
            [product_domain],
            {'fields': ['display_name', 'qty_available', 'virtual_available', 'id']}
        )
        
        if not products:
            await update.message.reply_text(f"❌ Không tìm thấy sản phẩm nào có mã `{product_code}`.")
            return

        product = products[0]
        product_id = product['id']
        product_name = product['display_name']
        
        # Lấy TỒN KHO CHI TIẾT (stock.quant)
        quant_domain = [('product_id', '=', product_id), ('quantity', '>', 0)]
        quant_data = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'stock.quant', 'search_read',
            [quant_domain],
            {'fields': ['location_id', 'quantity']}
        )
        
        # Lấy tên các kho liên quan
        location_ids = list(set([q['location_id'][0] for q in quant_data]))
        location_info = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'stock.location', 'search_read',
            [[('id', 'in', location_ids)]],
            {'fields': ['id', 'display_name']}
        )
        location_map = {loc['id']: loc['display_name'] for loc in location_info}
        
        # Tính toán tồn kho chi tiết theo yêu cầu
        hn_stock_qty = 0
        hn_transit_qty = 0
        hcm_stock_qty = 0
        
        # Danh sách tồn kho chi tiết (Quant)
        detail_stock_list = []
        
        # Map IDs và tính toán
        for q in quant_data:
            loc_id = q['location_id'][0]
            qty = q['quantity']
            loc_name = location_map.get(loc_id, "N/A")
            
            detail_stock_list.append(f"* {loc_name}: `{int(qty)}`")
            
            # Tính toán cho Khuyến nghị
            if LOCATION_MAP['HN_STOCK_CODE'] in loc_name:
                hn_stock_qty += qty
            elif LOCATION_MAP['HCM_STOCK_CODE'] in loc_name:
                hcm_stock_qty += qty
            elif LOCATION_MAP['HN_TRANSIT_NAME'] in loc_name:
                hn_transit_qty += qty
                
        total_hn_stock = hn_stock_qty + hn_transit_qty
        
        # Tính Khuyến nghị
        recommendation_qty = 0
        if total_hn_stock < TARGET_MIN_QTY:
            qty_needed = TARGET_MIN_QTY - total_hn_stock
            recommendation_qty = min(qty_needed, hcm_stock_qty)
        
        recommendation_text = ""
        if recommendation_qty > 0:
            recommendation_text = f"🚨 **Khuyến nghị đặt thêm:** `{int(recommendation_qty)}` SP (tồn kho HCM) để HN đủ tồn min `{TARGET_MIN_QTY}` SP/mã."
        else:
            recommendation_text = f"✅ Tồn kho HN đã đủ (`{int(total_hn_stock)}`/{TARGET_MIN_QTY} SP)."

        detail_stock_content = '\n'.join(detail_stock_list) if detail_stock_list else 'Không có tồn kho chi tiết lớn hơn 0.'

        # Định dạng tin nhắn trả về (SỬ DỤNG TRIPLE QUOTES ĐỂ KHẮC PHỤC LỖI SYNTAX)
        message = f"""
**1/ {product_code} - {product_name}**
Tồn kho HN: `{int(hn_stock_qty)}`
Tồn kho nhập HN: `{int(hn_transit_qty)}`
Tồn kho HCM: `{int(hcm_stock_qty)}`
{recommendation_text}

**2/ TỒN KHO CHI TIẾT (Theo kho)**
{detail_stock_content}
"""
        # message = message.strip() # Giữ nguyên format trên telegram

        await update.message.reply_text(message, parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"Lỗi khi tra cứu sản phẩm XML-RPC: {e}")
        await update.message.reply_text(f"❌ Có lỗi xảy ra khi truy vấn Odoo: {e}")

# --- 5. Các hàm khác (Không đổi) ---

async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kiểm tra kết nối tới Odoo."""
    await update.message.reply_text("Đang kiểm tra kết nối Odoo, xin chờ...")
    
    uid, _, error_msg = connect_odoo() 
    
    if uid:
        await update.message.reply_text(
            f"✅ **Thành công!** Kết nối Odoo DB: `{ODOO_DB}` tại `{ODOO_URL}`. User ID: `{uid}`", 
            parse_mode='Markdown'
        )
    else:
        final_error = f"❌ **Lỗi!** Không thể kết nối hoặc đăng nhập Odoo.\n\nChi tiết lỗi: `{error_msg}`"
        await update.message.reply_text(final_error, parse_mode='Markdown')

async def excel_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tạo và gửi báo cáo Excel đề xuất kéo hàng."""
    
    await update.message.reply_text("⌛️ Đang xử lý dữ liệu và tạo báo cáo Excel. Tác vụ này có thể mất vài giây. Vui lòng chờ...")
    
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


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gửi tin nhắn chào mừng và hướng dẫn."""
    user_name = update.message.from_user.first_name
    welcome_message = (
        f"Chào mừng **{user_name}** đến với Odoo Stock Bot! 🤖\n\n"
        "Tôi có thể thực hiện 3 tác vụ sau:\n"
        "1. **Tra cứu nhanh:** Gõ bất kỳ mã sản phẩm nào (ví dụ: `I-78`). Tôi sẽ trả về tồn kho chi tiết.\n"
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
