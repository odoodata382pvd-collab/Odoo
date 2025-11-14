# main.py - Phiên bản đầy đủ: bổ sung handler nhận Excel đơn hàng (mapping + SL kho nhập HN)
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
            return None, None, "Đăng nhập thất bại (uid=0). kiểm tra lại user/pass/db."
        models = xmlrpc.client.ServerProxy(f'{ODOO_URL_FINAL}/xmlrpc/2/object', context=context)
        return uid, models, "kết nối thành công."
    except Exception as e:
        return None, None, f"lỗi kết nối odoo xml-rpc: {e}"

# ---------------- Helpers ----------------
def find_required_location_ids(models, uid, db, pwd):
    location_ids = {}

    def search_location(name_code):
        loc_data = models.execute_kw(
            db, uid, pwd, 'stock.location', 'search_read',
            [[('display_name', 'ilike', name_code)]],
            {'fields': ['id', 'display_name', 'complete_name']}
        )
        if not loc_data:
            return None
        return next((l for l in loc_data if name_code.lower() in l['display_name'].lower()), loc_data[0])

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

# ---------------- Report /keohang (giữ nguyên full thuật toán gốc) ----------------
def get_stock_data():
    uid, models, error_msg = connect_odoo()
    if not uid:
        return None, 0, error_msg
    try:
        location_ids = find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD)
        if len(location_ids) < 3:
            return None, 0, "không tìm thấy đủ 3 kho cần thiết."

        all_locations_ids = [v['id'] for v in location_ids.values()]
        quant_domain = [('location_id', 'in', all_locations_ids), ('quantity', '>', 0)]

        quant_data = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'stock.quant', 'search_read',
            [quant_domain],
            {'fields': ['product_id', 'location_id', 'quantity']}
        )

        product_ids = list({q['product_id'][0] for q in quant_data})
        product_info = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'product.product', 'search_read',
            [[('id', 'in', product_ids)]],
            {'fields': ['display_name', PRODUCT_CODE_FIELD]}
        )
        product_map = {p['id']: p for p in product_info}

        data = {}
        for q in quant_data:
            pid = q['product_id'][0]
            lid = q['location_id'][0]
            qty = float(q['quantity'])

            if pid not in data:
                info = product_map.get(pid)
                if not info:
                    continue
                data[pid] = {
                    'Mã SP': info.get(PRODUCT_CODE_FIELD, 'N/A'),
                    'Tên SP': info['display_name'],
                    'Tồn Kho HN': 0, 'Tồn Kho HCM': 0, 'Kho Nhập HN': 0,
                    'Tổng Tồn HN': 0, 'Số Lượng Đề Xuất': 0
                }
            if lid == location_ids['HN_STOCK']['id']:
                data[pid]['Tồn Kho HN'] += qty
            elif lid == location_ids['HCM_STOCK']['id']:
                data[pid]['Tồn Kho HCM'] += qty
            elif lid == location_ids['HN_TRANSIT']['id']:
                data[pid]['Kho Nhập HN'] += qty

        out = []
        for pid, info in data.items():
            info['Tổng Tồn HN'] = info['Tồn Kho HN'] + info['Kho Nhập HN']
            if info['Tổng Tồn HN'] < TARGET_MIN_QTY:
                need = TARGET_MIN_QTY - info['Tổng Tồn HN']
                info['Số Lượng Đề Xuất'] = min(need, info['Tồn Kho HCM'])
                if info['Số Lượng Đề Xuất'] > 0:
                    out.append(info)

        df = pd.DataFrame(out)
        if not df.empty:
            cols = ['Mã SP','Tên SP','Tồn Kho HN','Tồn Kho HCM','Kho Nhập HN','Số Lượng Đề Xuất']
            df = df[cols]
            for c in ['Tồn Kho HN','Tồn Kho HCM','Kho Nhập HN','Số Lượng Đề Xuất']:
                df[c] = df[c].round().astype(int)
        else:
            df = pd.DataFrame(columns=['Mã SP','Tên SP','Tồn Kho HN','Tồn Kho HCM','Kho Nhập HN','Số Lượng Đề Xuất'])

        buf = io.BytesIO()
        df.to_excel(buf, index=False)
        buf.seek(0)
        return buf, len(out), "ok"

    except Exception as e:
        return None, 0, str(e)

# ---------------- Handle product code (giữ nguyên 100%) ----------------
async def handle_product_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    product_code = update.message.text.strip().upper()
    await update.message.reply_text(f"đang tra tồn cho `{product_code}`, vui lòng chờ!", parse_mode='Markdown')

    uid, models, err = connect_odoo()
    if not uid:
        await update.message.reply_text(f"❌ lỗi kết nối odoo. `{escape_markdown(err)}`", parse_mode='Markdown')
        return

    try:
        loc = find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD)
        hn = loc.get('HN_STOCK',{}).get('id')
        tx = loc.get('HN_TRANSIT',{}).get('id')
        hcm = loc.get('HCM_STOCK',{}).get('id')

        products = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD, 'product.product','search_read',
            [[(PRODUCT_CODE_FIELD,'=',product_code)]],
            {'fields':['display_name','id']}
        )
        if not products:
            await update.message.reply_text(f"❌ Không tìm thấy sản phẩm `{product_code}`")
            return

        product = products[0]
        pid = product['id']

        def qty(loc_id):
            if not loc_id: return 0
            r = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,'product.product','read',
                [[pid]],
                {'fields':['qty_available'],'context':{'location':loc_id}}
            )
            return int(round(r[0].get('qty_available',0)))

        hn_qty = qty(hn)
        tx_qty = qty(tx)
        hcm_qty = qty(hcm)

        quant = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,'stock.quant','search_read',
            [[('product_id','=',pid),('available_quantity','>',0)]],
            {'fields':['location_id','available_quantity']}
        )

        loc_ids = list({q['location_id'][0] for q in quant})
        loc_info = {}
        if loc_ids:
            loc_raw = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,'stock.location','read',
                [loc_ids],
                {'fields':['id','display_name','complete_name']}
            )
            loc_info = {l['id']:l for l in loc_raw}

        detail = {}
        for q in quant:
            lid = q['location_id'][0]
            qty = int(q['available_quantity'])
            if qty <= 0: continue
            name = loc_info.get(lid,{}).get('complete_name') or loc_info.get(lid,{}).get('display_name') or str(lid)
            detail[name] = detail.get(name,0)+qty

        total_hn = hn_qty + tx_qty
        if total_hn < TARGET_MIN_QTY:
            need = TARGET_MIN_QTY - total_hn
            rec = min(need, hcm_qty)
            rec_msg = f"=> đề xuất nhập thêm {rec} sp để đủ {TARGET_MIN_QTY}"
        else:
            rec_msg = f"=> tồn hn đã đủ ({total_hn}/{TARGET_MIN_QTY})"

        msg = f"""{product_code} {product['display_name']}
tồn kho hn: {hn_qty}
tồn kho hcm: {hcm_qty}
tồn kho nhập hà nội: {tx_qty}
{rec_msg}

2/ Tồn kho chi tiết(Có hàng):
"""

        if detail:
            for k,v in detail.items():
                msg += f"{k}: {v}\n"
        else:
            msg += "Không có tồn kho chi tiết > 0."

        await update.message.reply_text(msg)

    except Exception as e:
        await update.message.reply_text(f"❌ lỗi: {e}")

# ---------------- Handle Excel Order File (Mapping + SL kho nhập HN) ----------------
async def handle_excel_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        doc = update.message.document
        name = (doc.file_name or "").lower()

        if not (name.endswith(".xlsx") or name.endswith(".xls")):
            await update.message.reply_text("❌ File không phải Excel.")
            return

        await update.message.reply_text("⌛ Đang xử lý đơn hàng...")

        file = await doc.get_file()
        raw = await file.download_as_bytearray()

        try:
            df = pd.read_excel(io.BytesIO(raw))
        except Exception as e:
            await update.message.reply_text(f"❌ Không đọc được file: {e}")
            return

        df.columns = df.columns.str.strip().str.lower()

        def map_col(keys):
            for col in df.columns:
                for k in keys:
                    if k in col:
                        return col
            return None

        col_code = map_col(['mã hàng','mã sp','ma sp','code','sku','model','mã','ma hang'])
        col_sl   = map_col(['sl','số lượng','so luong','qty','quantity','sl đặt'])
        col_dv   = map_col(['dv nhận','đơn vị nhận','receiver','dv_nhan'])

        if not col_code or not col_sl or not col_dv:
            await update.message.reply_text(
                f"❌ Không map được cột.\n"
                f"Mã hàng: {col_code}\nSL: {col_sl}\nDV nhận: {col_dv}"
            )
            return

        uid, models, err = connect_odoo()
        if not uid:
            await update.message.reply_text(f"❌ Lỗi Odoo: {err}")
            return

        loc = find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD)
        hn_stock = loc.get("HN_STOCK",{}).get("id")
        hn_transit = loc.get("HN_TRANSIT",{}).get("id")

        if not hn_stock or not hn_transit:
            await update.message.reply_text("❌ Không tìm được kho 201/201 hoặc Kho nhập Hà Nội.")
            return

        result = []

        for _, row in df.iterrows():
            code = str(row[col_code]).strip().upper()
            try:
                qty_need = int(float(row[col_sl]))
            except:
                qty_need = 0
            dv_nhan = str(row[col_dv])

            prod = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,'product.product','search_read',
                [[(PRODUCT_CODE_FIELD,'=',code)]],
                {'fields':['id','display_name','default_code']}
            )

            if not prod:
                result.append([code,"KHÔNG TÌM THẤY","",dv_nhan,0,0,qty_need,"Không có",qty_need])
                continue

            prod = prod[0]
            pid = prod['id']

            def qty(loc_id):
                r = models.execute_kw(
                    ODOO_DB, uid, ODOO_PASSWORD,'product.product','read',
                    [[pid]],
                    {'fields':['qty_available'],'context':{'location':loc_id}}
                )
                return int(round(r[0].get('qty_available',0)))

            stock_hn = qty(hn_stock)
            stock_transit = qty(hn_transit)

            total = stock_hn + stock_transit
            if total >= qty_need:
                status = "Đủ"
                missing = 0
            else:
                status = "Thiếu"
                missing = qty_need - total

            result.append([
                code,
                prod['display_name'],
                prod.get('default_code',""),
                dv_nhan,
                stock_hn,
                stock_transit,
                qty_need,
                status,
                missing
            ])

        out = pd.DataFrame(result, columns=[
            "Mã SP","Tên SP","Model","DV nhận",
            "SL tồn HN (201/201)",
            "SL kho nhập HN",
            "SL đặt","Đủ/Không","Thiếu bao nhiêu"
        ])

        buf = io.BytesIO()
        out.to_excel(buf, index=False)
        buf.seek(0)

        await update.message.reply_document(
            document=buf,
            filename="ket_qua_don_hang.xlsx",
            caption="📦 Kết quả kiểm tra đơn hàng"
        )

    except Exception as e:
        await update.message.reply_text(f"❌ Lỗi xử lý đơn: {e}")

# ---------------- Commands ----------------
async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Đang kiểm tra kết nối odoo...")
    uid,_,err=connect_odoo()
    if uid:
        await update.message.reply_text(f"✅ Odoo OK. user id: {uid}")
    else:
        await update.message.reply_text(f"❌ Lỗi: {err}")

async def excel_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⌛ Đang tạo báo cáo...")
    buf,count,err = get_stock_data()
    if not buf:
        await update.message.reply_text(f"❌ Lỗi: {err}")
        return
    if count>0:
        await update.message.reply_document(document=buf, filename="de_xuat_keo_hang.xlsx")
    else:
        await update.message.reply_text("Không có sản phẩm cần kéo hàng.")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    name = update.message.from_user.first_name
    txt = (
        f"Chào {name}!\n"
        "1. Gõ mã SP để tra tồn.\n"
        "2. /keohang để báo cáo.\n"
        "3. Gửi file Excel đơn hàng để kiểm tra."
    )
    await update.message.reply_text(txt)

# ---------------- Main ----------------
def main():
    if not TELEGRAM_TOKEN or not ODOO_URL_RAW or not ODOO_DB or not ODOO_USERNAME or not ODOO_PASSWORD:
        logger.error("Thiếu biến môi trường cấu hình.")
        return

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    try:
        bot = Bot(TELEGRAM_TOKEN)
        asyncio.get_event_loop().run_until_complete(bot.delete_webhook())
    except:
        pass

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", start_command))
    app.add_handler(CommandHandler("ping", ping_command))
    app.add_handler(CommandHandler("keohang", excel_report_command))

    app.add_handler(MessageHandler(filters.Document.ALL, handle_excel_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_product_code))

    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == '__main__':
    main()
