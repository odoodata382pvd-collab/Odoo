# Tệp: main.py - Phiên bản HOÀN CHỈNH: Sửa lỗi Markdown, Lọc kho không cần thiết & Định dạng TRA TỒN

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
# Cập nhật: Sử dụng tên kho để map tồn kho
LOCATION_MAP = {
    'HN_STOCK_CODE': '201/201', 
    'HCM_STOCK_CODE': '124/124', 
    'HN_TRANSIT_NAME': 'Kho nhập Hà Nội', 
}

# Tên các kho ưu tiên (dùng để in đậm và sắp xếp)
PRIORITY_LOCATIONS = [
    LOCATION_MAP['HN_STOCK_CODE'],      # 201/201
    LOCATION_MAP['HN_TRANSIT_NAME'],    # Kho nhập Hà Nội
    LOCATION_MAP['HCM_STOCK_CODE'],     # 124/124
]

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
        error_message = f"lỗi phân tích cú pháp odoo_url: {e}"
        return None, None, error_message
    
    common_url = '{}/xmlrpc/2/common'.format(base_url_for_rpc)
    try:
        common = xmlrpc.client.ServerProxy(common_url, context=ssl._create_unverified_context())
        uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD, {})
        
        if not uid:
             error_message = f"đăng nhập thất bại (uid=0). kiểm tra lại user/pass/db: {ODOO_USERNAME} / {ODOO_DB}."
             return None, None, error_message
        
        models = xmlrpc.client.ServerProxy('{}/xmlrpc/2/object'.format(base_url_for_rpc), 
                                            context=ssl._create_unverified_context())

        return uid, models, "kết nối thành công."
    
    except xmlrpc.client.ProtocolError as pe:
        error_message = f"lỗi giao thức odoo (400 bad request?): {pe}. url: {common_url}"
        return None, None, error_message
    except Exception as e:
        error_message = f"lỗi kết nối odoo xml-rpc: {e}. url: {common_url}"
        return None, None, error_message

# --- Helper: Tìm ID của các kho cần thiết ---
def find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD):
    location_ids = {}
    
    # Lấy HN_STOCK (201/201)
    loc_data = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD, 'stock.location', 'search_read', 
        [[('display_name', 'ilike', LOCATION_MAP['HN_STOCK_CODE'])]], 
        {'fields': ['id', 'display_name']}
    )
    if loc_data: location_ids['HN_STOCK'] = {'id': loc_data[0]['id'], 'name': loc_data[0]['display_name']}

    # Lấy HCM_STOCK (124/124)
    loc_data = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD, 'stock.location', 'search_read', 
        [[('display_name', 'ilike', LOCATION_MAP['HCM_STOCK_CODE'])]], 
        {'fields': ['id', 'display_name']}
    )
    if loc_data: location_ids['HCM_STOCK'] = {'id': loc_data[0]['id'], 'name': loc_data[0]['display_name']}

    # Lấy Kho nhập HN (Tìm theo tên hiển thị 'Kho nhập Hà Nội')
    loc_data = models.execute_kw(
        ODOO_DB, uid, ODOO_PASSWORD, 'stock.location', 'search_read', 
        [[('display_name', 'ilike', LOCATION_MAP['HN_TRANSIT_NAME'])]], 
        {'fields': ['id', 'display_name']}
    )
    if loc_data: 
        preferred_loc = next((l for l in loc_data if l['display_name'].endswith(LOCATION_MAP['HN_TRANSIT_NAME'])), loc_data[0])
        location_ids['HN_TRANSIT'] = {'id': preferred_loc['id'], 'name': preferred_loc['display_name']}
    
    return location_ids

# --- 3. Hàm chính (Logic nghiệp vụ Odoo) ---
def get_stock_data():
    """Lấy dữ liệu tồn kho từ Odoo bằng XML-RPC (cho lệnh /keohang)."""
    uid, models, error_msg = connect_odoo()
    if not uid:
        return None, 0, error_msg 

    try:
        # TÌM LOCATION IDs
        location_ids = find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD)
            
        if len(location_ids) < 3:
            found_keys = list(location_ids.keys())
            found_ids = {k: v['id'] for k, v in location_ids.items()}
            error_msg = f"không tìm thấy đủ 3 kho cần thiết. đã tìm thấy: {found_keys} - id: {found_ids}"
            logger.error(error_msg)
            return None, 0, error_msg 

        # LẤY DANH SÁCH TỒN KHO (Quant - Cột "Có hàng")
        # Logic /keohang cần tồn kho tổng (HN Stock + HN Transit) để tính đề xuất
        # Ta phải dùng stock.quant cho cả 3 kho vì đây là báo cáo tổng hợp.
        # Lưu ý: Logic /keohang không yêu cầu tách biệt "Hiện có" hay "Có hàng", nên ta dùng chung source stock.quant cho đơn giản.
        
        all_locations_ids = [v['id'] for v in location_ids.values()]
        quant_domain = [('location_id', 'in', all_locations_ids), ('quantity', '>', 0)]
        
        quant_data = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'stock.quant', 'search_read',
            [quant_domain],
            {'fields': ['product_id', 'location_id', 'quantity']}
        )
        
        # ... (Phần còn lại của logic nghiệp vụ không thay đổi) ...
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
            qty = q['quantity']
            
            if prod_id not in data and prod_id in product_map:
                data[prod_id] = {
                    'Mã SP': product_map[prod_id].get(PRODUCT_CODE_FIELD, 'N/A'),
                    'Tên SP': product_map[prod_id]['display_name'],
                    'Tồn Kho HN': 0, 'Tồn Kho HCM': 0, 'Kho Nhập HN': 0, 'Tổng Tồn HN': 0, 'Số Lượng Đề Xuất': 0
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
                
                if info['Số Lượng Đề Xuất'] > 0: report_data.append(info)
                    
        df = pd.DataFrame(report_data)
        COLUMNS_ORDER = ['Mã SP', 'Tên SP', 'Tồn Kho HN', 'Tồn Kho HCM', 'Kho Nhập HN', 'Số Lượng Đề Xuất']
        df = df[COLUMNS_ORDER]
        
        excel_buffer = io.BytesIO()
        df.to_excel(excel_buffer, index=False, sheet_name='DeXuatKeoHang')
        excel_buffer.seek(0)
        
        return excel_buffer, len(report_data), "thành công"

    except Exception as e:
        error_msg = f"lỗi khi truy vấn dữ liệu odoo xml-rpc: {e}"
        return None, 0, error_msg

# --- Helper: Escape Markdown V2 ---
def escape_markdown(text):
    """Escape special characters for Markdown V1/V2 format."""
    # Ký tự cần escape: \ (phải là đầu tiên), _, *, [, ], (, ), ~, `, >, #, +, -, =, |, {, }, ., !
    special_chars = ['\\', '_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for char in special_chars:
        text = text.replace(char, f'\\{char}')
    return text

# --- 4. Hàm xử lý Tra Cứu Sản Phẩm (ĐÃ SỬA LOGIC TỒN KHO NHẬP HN) ---
async def handle_product_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Tra cứu nhanh tồn kho theo Mã sản phẩm (default_code).
    Sử dụng Hiện có cho Kho nhập HN, Có hàng cho các kho khác.
    """
    product_code = update.message.text.strip().upper()
    await update.message.reply_text(f"đang tra tồn cho `{product_code}`, vui lòng chờ!", parse_mode='Markdown')

    uid, models, error_msg = connect_odoo()
    if not uid:
        await update.message.reply_text(f"❌ lỗi kết nối odoo. chi tiết: `{error_msg.lower()}`", parse_mode='Markdown')
        return

    try:
        # TÌM LOCATION IDs CẦN THIẾT
        location_ids = find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD)
        
        hn_transit_id = location_ids.get('HN_TRANSIT', {}).get('id')
        hn_stock_id = location_ids.get('HN_STOCK', {}).get('id')
        hcm_stock_id = location_ids.get('HCM_STOCK', {}).get('id')
        
        # 1. Lấy thông tin sản phẩm
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
        
        # 2. LẤY TỒN KHO SUMMARY (Mục 1)
        
        # 2a. LẤY TỒN KHO NHẬP HN (Hiện có - product.product context)
        hn_transit_qty = 0
        if hn_transit_id:
            # Truy vấn 'qty_available' (Hiện có) cho vị trí kho nhập
            transit_product_info = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD, 'product.product', 'read',
                [[product_id]],
                {'fields': ['qty_available'], 'context': {'location': hn_transit_id}}
            )
            if transit_product_info:
                hn_transit_qty = transit_product_info[0].get('qty_available', 0)

        # 2b. LẤY TỒN KHO HN/HCM (Có hàng - stock.quant)
        hn_stock_qty = 0
        hcm_stock_qty = 0

        quant_loc_ids = [lid for lid in [hn_stock_id, hcm_stock_id] if lid]
        quant_domain = [('product_id', '=', product_id), ('quantity', '>', 0), ('location_id', 'in', quant_loc_ids)]
        quant_data = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'stock.quant', 'search_read',
            [quant_domain],
            {'fields': ['location_id', 'quantity']}
        )
        
        for q in quant_data:
            loc_id = q['location_id'][0]
            qty = q['quantity']
            
            if loc_id == hn_stock_id:
                hn_stock_qty += qty
            elif loc_id == hcm_stock_id:
                hcm_stock_qty += qty

        # 3. LẤY TỒN KHO CHI TIẾT (Mục 2 - Có hàng - stock.quant)
        
        # Lấy TỒN KHO CHI TIẾT (stock.quant) cho TẤT CẢ các kho có tồn > 0
        quant_domain_all = [('product_id', '=', product_id), ('quantity', '>', 0)]
        quant_data_all = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'stock.quant', 'search_read',
            [quant_domain_all],
            {'fields': ['location_id', 'quantity']}
        )
        
        # Lấy tên và loại (usage) của các kho liên quan
        location_ids_all = list(set([q['location_id'][0] for q in quant_data_all]))
        location_info = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'stock.location', 'search_read',
            [[('id', 'in', location_ids_all)]],
            {'fields': ['id', 'display_name', 'usage']} 
        )
        location_map = {loc['id']: loc for loc in location_info}
        
        all_stock_details = {} 
        for q in quant_data_all:
            loc_id = q['location_id'][0]
            qty = q['quantity']
            loc_data = location_map.get(loc_id, {})
            loc_name = loc_data.get('display_name', "n/a")
            loc_usage = loc_data.get('usage', 'internal')
            
            # CHỈ LƯU VÀ HIỂN THỊ CÁC KHO CÓ USAGE LÀ 'internal' HOẶC 'transit'
            if loc_usage in ['internal', 'transit']:
                all_stock_details[loc_name] = int(qty)


        # 4. TÍNH TOÁN KHUYẾN NGHỊ VÀ FORMAT TIN NHẮN
        
        total_hn_stock = hn_stock_qty + hn_transit_qty
        
        recommendation_qty = 0
        if total_hn_stock < TARGET_MIN_QTY:
            qty_needed = TARGET_MIN_QTY - total_hn_stock
            recommendation_qty = min(qty_needed, hcm_stock_qty)
        
        recommendation_text = f"=> đề xuất nhập thêm `{int(recommendation_qty)}` sp để hn đủ tồn `{TARGET_MIN_QTY}` sản phẩm." if recommendation_qty > 0 else f"=> tồn kho hn đã đủ (`{int(total_hn_stock)}`/{TARGET_MIN_QTY} sp)."

        # Sắp xếp và định dạng TỒN KHO CHI TIẾT (Mục 2)
        sorted_locations = {}
        other_locations = {}
        
        for name, qty in all_stock_details.items():
            is_priority = False
            for p_code in PRIORITY_LOCATIONS:
                if p_code.lower() in name.lower():
                    sorted_locations[p_code] = (name, qty) 
                    is_priority = True
                    break
            if not is_priority:
                other_locations[name] = qty

        detail_stock_list = []
        
        # 1. 3 kho ưu tiên (theo thứ tự)
        for p_code in PRIORITY_LOCATIONS:
            if p_code in sorted_locations:
                name, qty = sorted_locations[p_code]
                # In đậm tên kho ưu tiên VÀ escape ký tự đặc biệt
                safe_name = escape_markdown(name.lower())
                detail_stock_list.append(f"**{safe_name}**: `{qty}`")
        
        # 2. Các kho còn lại (sắp xếp theo tên)
        for name in sorted(other_locations.keys()):
            qty = other_locations[name]
            # Escape ký tự đặc biệt
            safe_name = escape_markdown(name.lower())
            detail_stock_list.append(f"{safe_name}: `{qty}`")

        detail_stock_content = '\n'.join(detail_stock_list) if detail_stock_list else 'không có tồn kho chi tiết lớn hơn 0.'

        # Định dạng tin nhắn trả về (Chữ thường theo yêu cầu)
        message = f"""
1/ {product_name}
tồn kho hn: `{int(hn_stock_qty)}`
tồn kho hcm: `{int(hcm_stock_qty)}`
tồn kho nhập hà nội: `{int(hn_transit_qty)}`
{recommendation_text}

2/ tồn kho chi tiết (có hàng):
{detail_stock_content}
"""
        # SỬ DỤNG PARSE_MODE='MARKDOWN' ĐÃ ĐƯỢC CẬP NHẬT VÀ SỬ DỤNG ESCAPE_MARKDOWN
        await update.message.reply_text(message.strip(), parse_mode='Markdown')
        
    except Exception as e:
        logger.error(f"lỗi khi tra cứu sản phẩm xml-rpc: {e}")
        await update.message.reply_text(f"❌ có lỗi xảy ra khi truy vấn odoo: {e}.\n\n_(lỗi này có thể do ký tự đặc biệt trong tên kho hoặc truy vấn không hợp lệ)_", parse_mode='Markdown')

# --- 5. Các hàm khác (Đã chuyển sang chữ thường cho tin nhắn) ---
async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Kiểm tra kết nối tới Odoo."""
    await update.message.reply_text("đang kiểm tra kết nối odoo, xin chờ...")
    
    uid, _, error_msg = connect_odoo() 
    
    if uid:
        await update.message.reply_text(
            f"✅ **thành công!** kết nối odoo db: `{ODOO_DB}` tại `{ODOO_URL}`. user id: `{uid}`", 
            parse_mode='Markdown'
        )
    else:
        final_error = f"❌ **lỗi!** không thể kết nối hoặc đăng nhập odoo.\n\nchi tiết lỗi: `{error_msg.lower()}`"
        await update.message.reply_text(final_error, parse_mode='Markdown')

async def excel_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tạo và gửi báo cáo Excel đề xuất kéo hàng."""
    
    await update.message.reply_text("⌛️ đang xử lý dữ liệu và tạo báo cáo excel. tác vụ này có thể mất vài giây. vui lòng chờ...")
    
    excel_buffer, item_count, error_msg = get_stock_data() 
    
    if excel_buffer is None:
        await update.message.reply_text(f"❌ lỗi kết nối odoo hoặc lỗi nghiệp vụ. không thể tạo báo cáo.\n\nchi tiết lỗi: `{error_msg.lower()}`", parse_mode='Markdown')
        return
    
    if item_count > 0:
        await update.message.reply_document(
            document=excel_buffer,
            filename='de_xuat_keo_hang.xlsx',
            caption=f"✅ hoàn thành! đã tìm thấy **{item_count}** sản phẩm cần kéo hàng từ hcm về hn để đạt tồn kho tối thiểu {TARGET_MIN_QTY}."
        )
    else:
        await update.message.reply_text(f"✅ tuyệt vời! tất cả sản phẩm hiện tại đã đạt hoặc vượt mức tồn kho tối thiểu {TARGET_MIN_QTY} tại kho hn (bao gồm cả hàng đi đường). không cần kéo thêm hàng.")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Gửi tin nhắn chào mừng và hướng dẫn."""
    user_name = update.message.from_user.first_name
    welcome_message = (
        f"chào mừng **{user_name}** đến với odoo stock bot! 🤖\n\n"
        "tôi có thể thực hiện 3 tác vụ sau:\n"
        "1. tra cứu nhanh: gõ bất kỳ mã sản phẩm nào (ví dụ: `i-78`). tôi sẽ trả về tồn kho chi tiết.\n"
        "2. báo cáo kéo hàng (excel): dùng lệnh `/keohang` để nhận file excel thống kê các sản phẩm cần kéo từ hcm về hn.\n"
        "3. kiểm tra kết nối: dùng lệnh `/ping` để kiểm tra kết nối odoo."
    )
    await update.message.reply_text(welcome_message.lower(), parse_mode='Markdown')

def main():
    """Chạy bot."""
    if not TELEGRAM_TOKEN or not ODOO_URL or not ODOO_DB or not ODOO_USERNAME or not ODOO_PASSWORD:
        logger.error("vui lòng thiết lập tất cả các biến môi trường cần thiết (token, url, db, user, pass).")
        return
        
    application = Application.builder().token(TELEGRAM_TOKEN).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", start_command))
    application.add_handler(CommandHandler("ping", ping_command))
    application.add_handler(CommandHandler("keohang", excel_report_command))

    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_product_code))
    
    logger.info("bot đang khởi chạy ở chế độ polling (render free tier).")
    application.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == '__main__':
    main()
