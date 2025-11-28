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

ODOO_URL_RAW = os.environ.get('ODOO_URL').rstrip('/') if os.environ.get('ODOO_URL') else None
if ODOO_URL_RAW and ODOO_URL_RAW.lower().endswith('/odoo'):
    ODOO_URL_FINAL = ODOO_URL_RAW[:-len('/odoo')]
else:
    ODOO_URL_FINAL = ODOO_URL_RAW

ODOO_DB = os.environ.get('ODOO_DB')
ODOO_USERNAME = os.environ.get('ODOO_USERNAME')
ODOO_PASSWORD = os.environ.get('ODOO_PASSWORD')

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
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO)
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

        common = xmlrpc.client.ServerProxy(
            f"{ODOO_URL_FINAL}/xmlrpc/2/common",
            context=ssl._create_unverified_context()
        )
        uid = common.authenticate(ODOO_DB, ODOO_USERNAME, ODOO_PASSWORD, {})

        if not uid:
            return None, None, "Đăng nhập thất bại. Kiểm tra DB/user/pass."

        models = xmlrpc.client.ServerProxy(
            f"{ODOO_URL_FINAL}/xmlrpc/2/object",
            context=ssl._create_unverified_context()
        )
        return uid, models, "OK"
    except Exception as e:
        return None, None, f"Lỗi kết nối: {e}"

# ---------------- Helpers ----------------
def find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD):
    out = {}

    def search(key):
        locs = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'stock.location', 'search_read',
            [[('display_name','ilike', key)]],
            {'fields':['id','display_name','complete_name']}
        )
        if not locs:
            return None
        for l in locs:
            if key.lower() in l['display_name'].lower():
                return {'id': l['id'], 'name': l['display_name']}
        l = locs[0]
        return {'id': l['id'], 'name': l['display_name']}

    hn = search(LOCATION_MAP['HN_STOCK_CODE'])
    if hn: out['HN_STOCK'] = hn

    hcm = search(LOCATION_MAP['HCM_STOCK_CODE'])
    if hcm: out['HCM_STOCK'] = hcm

    tran = search(LOCATION_MAP['HN_TRANSIT_NAME'])
    if tran: out['HN_TRANSIT'] = tran

    return out

def escape_markdown(text):
    chars = ['\\','_','*','[',']','(',')','~','`','>','#','+','-','=','|','{','}','.','!']
    text = str(text)
    for c in chars:
        text = text.replace(c, f"\\{c}")
    return text.replace('\\`', '`')
# ---------------- Report /keohang (đÃ TỐI ƯU – KHÔNG TREO BOT) ----------------
def get_stock_data():
    uid, models, error_msg = connect_odoo()
    if not uid:
        return None, 0, error_msg

    try:
        # Lấy ID 3 kho
        location_ids = find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD)
        if len(location_ids) < 3:
            return None, 0, f"Thiếu kho: {list(location_ids.keys())}"

        hn_id = location_ids['HN_STOCK']['id']
        tran_id = location_ids['HN_TRANSIT']['id']
        hcm_id = location_ids['HCM_STOCK']['id']

        # Lấy toàn bộ quant có hàng của 3 kho (KHÔNG đọc qty_available để tránh treo)
        quant_data = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'stock.quant','search_read',
            [[
                ('location_id','in',[hn_id, tran_id, hcm_id]),
                ('available_quantity','>',0),
            ]],
            {'fields':['product_id','location_id','available_quantity']}
        )

        # Nếu không có bất kỳ mã nào có hàng
        if not quant_data:
            empty_df = pd.DataFrame(columns=[
                'Mã SP','Tên SP','Tồn Kho HN','Tồn Kho HCM','Kho Nhập HN','Số Lượng Đề Xuất'
            ])
            buf = io.BytesIO()
            empty_df.to_excel(buf, index=False, sheet_name='DeXuatKeoHang')
            buf.seek(0)
            return buf, 0, "EMPTY"

        # Tổng hợp tồn kho chi tiết theo từng product
        stock_map = {}  # pid -> {hn,tran,hcm}
        for q in quant_data:
            pid = q['product_id'][0]
            loc = q['location_id'][0]
            qty = q['available_quantity']

            if pid not in stock_map:
                stock_map[pid] = {'hn':0,'tran':0,'hcm':0}

            if loc == hn_id:
                stock_map[pid]['hn'] += qty
            elif loc == tran_id:
                stock_map[pid]['tran'] += qty
            elif loc == hcm_id:
                stock_map[pid]['hcm'] += qty

        # Lấy thông tin SP
        product_ids = list(stock_map.keys())
        product_info = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'product.product','search_read',
            [[('id','in',product_ids)]],
            {'fields':['id','display_name',PRODUCT_CODE_FIELD]}
        )
        product_map = {p['id']: p for p in product_info}

        # Build báo cáo đề xuất
        report = []
        for pid, qtys in stock_map.items():
            info = product_map.get(pid)
            if not info:
                continue

            code = info.get(PRODUCT_CODE_FIELD, "")
            name = info.get("display_name","")

            ton_hn = int(qtys['hn'])
            ton_tran = int(qtys['tran'])
            ton_hcm = int(qtys['hcm'])

            total_hn = ton_hn + ton_tran
            need = max(TARGET_MIN_QTY - total_hn, 0)
            de_xuat = min(need, ton_hcm)

            if de_xuat > 0:
                report.append({
                    'Mã SP': code,
                    'Tên SP': name,
                    'Tồn Kho HN': ton_hn,
                    'Tồn Kho HCM': ton_hcm,
                    'Kho Nhập HN': ton_tran,
                    'Số Lượng Đề Xuất': de_xuat
                })

        df = pd.DataFrame(report)
        COLUMNS = ['Mã SP','Tên SP','Tồn Kho HN','Tồn Kho HCM','Kho Nhập HN','Số Lượng Đề Xuất']

        if not df.empty:
            df = df[COLUMNS]
        else:
            df = pd.DataFrame(columns=COLUMNS)

        # Xuất Excel
        buf = io.BytesIO()
        df.to_excel(buf, index=False, sheet_name='DeXuatKeoHang')
        buf.seek(0)

        return buf, len(df), "OK"

    except Exception as e:
        return None, 0, f"Lỗi xử lý kéo hàng: {e}"
# ---------------- PO /checkpo helpers ----------------
def _read_po_with_auto_header(file_bytes: bytes):
    try:
        df_tmp = pd.read_excel(io.BytesIO(file_bytes), header=None)
    except Exception as e:
        return None, f"Không đọc được file PO: {e}"

    header_idx = None
    for i in range(len(df_tmp)):
        row = df_tmp.iloc[i].astype(str).str.lower()
        txt = " ".join(row)
        if any(k in txt for k in ["model", "mã sp", "ma sp", "mã hàng", "ma hang"]):
            header_idx = i
            break

    if header_idx is None:
        header_idx = 0

    try:
        df = pd.read_excel(io.BytesIO(file_bytes), header=header_idx)
        return df, None
    except Exception as e:
        return None, f"Lỗi đọc header tại dòng {header_idx}: {e}"


def _detect_po_columns(df: pd.DataFrame):
    cols = {c: str(c).lower().strip() for c in df.columns}

    # Ưu tiên tuyệt đối "Model"
    code_col = None
    for col, low in cols.items():
        if low == "model":
            code_col = col
            break

    if not code_col:
        for col, low in cols.items():
            if low.strip() == "model":
                code_col = col
                break

    # fallback
    def find(keys):
        for col, low in cols.items():
            for k in keys:
                if k in low:
                    return col
        return None

    if not code_col:
        code_col = find(["mã sp","ma sp","mã hàng","ma hang","mã sản phẩm","ma san pham"])

    qty_col = find(["sl","số lượng","so luong","sl đặt","sl dat","s.l"])
    recv_col = find(["đv nhận","dv nhận","đơn vị nhận","don vi nhan","cửa hàng nhận"])

    return code_col, qty_col, recv_col


def _get_stock_for_product_with_cache(models, uid, product_id, location_ids, cache):
    if product_id in cache:
        return cache[product_id]

    hn_id = location_ids.get('HN_STOCK', {}).get('id')
    tran_id = location_ids.get('HN_TRANSIT', {}).get('id')
    hcm_id = location_ids.get('HCM_STOCK', {}).get('id')

    def _qty(loc):
        if not loc:
            return 0
        res = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'product.product','read',
            [[product_id]],
            {'fields':['qty_available'], 'context':{'location':loc}}
        )
        if res and res[0]:
            return int(round(res[0].get('qty_available', 0)))
        return 0

    out = {
        'hn': _qty(hn_id),
        'transit': _qty(tran_id),
        'hcm': _qty(hcm_id),
    }
    cache[product_id] = out
    return out


def process_po_and_build_report(file_bytes: bytes):
    df_raw, err = _read_po_with_auto_header(file_bytes)
    if df_raw is None:
        return None, err

    if df_raw.empty:
        return None, "File PO trống."

    code_col, qty_col, recv_col = _detect_po_columns(df_raw)
    if not code_col or not qty_col or not recv_col:
        return None, (
            "Không nhận diện được đủ 3 cột Model/Mã SP – Số lượng – ĐV nhận.\n"
            f"Các cột hiện có: {list(df_raw.columns)}"
        )

    df = df_raw[[code_col, qty_col, recv_col]].copy()
    df.columns = ['Mã SP','SL cần giao','ĐV nhận']

    df['Mã SP'] = df['Mã SP'].astype(str).strip().str.upper()
    df['SL cần giao'] = pd.to_numeric(df['SL cần giao'], errors='coerce').fillna(0)

    df = df[(df['Mã SP']!="") & (df['SL cần giao']>0)]
    if df.empty:
        return None, "Không có dòng hợp lệ."

    uid, models, err_conn = connect_odoo()
    if not uid:
        return None, f"Lỗi kết nối Odoo: {err_conn}"

    try:
        # Map mã SP -> product
        codes = sorted(df['Mã SP'].unique().tolist())
        products = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'product.product','search_read',
            [[(PRODUCT_CODE_FIELD,'in',codes)]],
            {'fields':['id','display_name',PRODUCT_CODE_FIELD]}
        )
        code_map = {}
        for p in products:
            c = str(p.get(PRODUCT_CODE_FIELD) or "").strip().upper()
            code_map[c] = p

        # Lấy ID kho
        location_ids = find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD)
        if len(location_ids) < 2:
            return None, f"Không tìm thấy đủ kho: {list(location_ids.keys())}"

        stock_cache = {}
        rows = []

        for _, r in df.iterrows():
            code = r['Mã SP']
            qty_need = int(round(r['SL cần giao']))
            recv = r['ĐV nhận']

            prod = code_map.get(code)
            if not prod:
                rows.append({
                    'Mã SP': code,
                    'Tên SP': 'KHÔNG TÌM THẤY TRÊN ODOO',
                    'ĐV nhận': recv,
                    'SL cần giao': qty_need,
                    'Tồn HN': 0,
                    'Tồn Kho Nhập': 0,
                    'Tổng tồn HN': 0,
                    'Tồn HCM': 0,
                    'Trạng thái': 'KHÔNG TÌM THẤY MÃ',
                    'SL cần kéo từ HCM': 0,
                    'SL thiếu': qty_need,
                })
                continue

            pid = prod['id']
            name = prod['display_name']

            stock = _get_stock_for_product_with_cache(models, uid, pid, location_ids, stock_cache)
            hn = stock['hn']
            tran = stock['transit']
            hcm = stock['hcm']

            total_hn = hn + tran
            pull = 0
            shortage = 0

            if qty_need <= hn:
                status = "ĐỦ tại kho HN (201/201)"
            elif qty_need <= total_hn:
                status = "ĐỦ (HN + Kho nhập HN)"
            else:
                need_from_hcm = qty_need - total_hn
                if need_from_hcm <= hcm:
                    pull = need_from_hcm
                    status = "CẦN KÉO HÀNG TỪ HCM"
                else:
                    pull = hcm
                    shortage = need_from_hcm - hcm
                    status = "THIẾU DÙ ĐÃ KÉO TỐI ĐA TỪ HCM"

            rows.append({
                'Mã SP': code,
                'Tên SP': name,
                'ĐV nhận': recv,
                'SL cần giao': qty_need,
                'Tồn HN': hn,
                'Tồn Kho Nhập': tran,
                'Tổng tồn HN': total_hn,
                'Tồn HCM': hcm,
                'Trạng thái': status,
                'SL cần kéo từ HCM': pull,
                'SL thiếu': shortage,
            })

        df_out = pd.DataFrame(rows)

        ORDER = [
            'Mã SP','Tên SP','ĐV nhận','SL cần giao',
            'Tồn HN','Tồn Kho Nhập','Tổng tồn HN','Tồn HCM',
            'Trạng thái','SL cần kéo từ HCM','SL thiếu'
        ]
        for col in ORDER:
            if col not in df_out:
                df_out[col] = ""

        df_out = df_out[ORDER]

        buf = io.BytesIO()
        df_out.to_excel(buf, index=False, sheet_name='KiemTraPO')
        buf.seek(0)
        return buf, None

    except Exception as e:
        return None, f"Lỗi xử lý PO: {e}"
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
            f"❌ lỗi kết nối odoo. chi tiết: `{escape_markdown(error_msg)}`",
            parse_mode='Markdown'
        )
        return

    try:
        location_ids = find_required_location_ids(models, uid, ODOO_DB, ODOO_PASSWORD)
        hn_id = location_ids.get('HN_STOCK',{}).get('id')
        tran_id = location_ids.get('HN_TRANSIT',{}).get('id')
        hcm_id = location_ids.get('HCM_STOCK',{}).get('id')

        # Lấy product
        products = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'product.product','search_read',
            [[(PRODUCT_CODE_FIELD,'=',product_code)]],
            {'fields':['display_name','id']}
        )
        if not products:
            await update.message.reply_text(f"❌ Không tìm thấy sản phẩm `{product_code}`")
            return

        prod = products[0]
        pid = prod['id']
        name = prod['display_name']

        # Lấy tồn kho chuẩn theo từng kho
        def get_qty(loc):
            if not loc:
                return 0
            res = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                'product.product','read',
                [[pid]],
                {'fields':['qty_available'], 'context':{'location':loc}}
            )
            if res and res[0]:
                return int(round(res[0].get('qty_available',0)))
            return 0

        hn_qty = get_qty(hn_id)
        tran_qty = get_qty(tran_id)
        hcm_qty = get_qty(hcm_id)

        # Lấy tồn chi tiết (Quant có hàng)
        quant_data = models.execute_kw(
            ODOO_DB, uid, ODOO_PASSWORD,
            'stock.quant','search_read',
            [[('product_id','=',pid), ('available_quantity','>',0)]],
            {'fields':['location_id','available_quantity']}
        )

        loc_ids = list({q['location_id'][0] for q in quant_data})
        if loc_ids:
            loc_info = models.execute_kw(
                ODOO_DB, uid, ODOO_PASSWORD,
                'stock.location','read',
                [loc_ids],
                {'fields':['id','display_name','complete_name']}
            )
        else:
            loc_info = []

        loc_map = {l['id']:l for l in loc_info}

        detail = {}
        for q in quant_data:
            loc = q['location_id'][0]
            qty = int(q['available_quantity'])
            name_loc = loc_map.get(loc,{}).get('complete_name') or loc_map.get(loc,{}).get('display_name') or f"ID:{loc}"
            detail[name_loc] = detail.get(name_loc,0) + qty

        total_hn = hn_qty + tran_qty
        recommend = 0
        if total_hn < TARGET_MIN_QTY:
            need = TARGET_MIN_QTY - total_hn
            recommend = min(need, hcm_qty)

        msg = f"""{product_code} {name}
Tồn kho HN: {hn_qty}
Tồn kho HCM: {hcm_qty}
Tồn kho nhập Hà Nội: {tran_qty}
=> đề xuất nhập thêm {recommend} sp để HN đủ tồn {TARGET_MIN_QTY} sp.

2/ Tồn kho chi tiết (Có hàng):"""

        if detail:
            for k,v in detail.items():
                msg += f"\n{k}: {v}"
        else:
            msg += "\nKhông có tồn kho chi tiết."

        await update.message.reply_text(msg.strip())

    except Exception as e:
        logger.error(f"Lỗi tra tồn: {e}")
        await update.message.reply_text(f"❌ Lỗi khi tra tồn: {e}")


# ---------------- Telegram Handlers ----------------
async def ping_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Đang kiểm tra kết nối Odoo...")
    uid, _, msg = connect_odoo()
    if uid:
        await update.message.reply_text(f"✅ Kết nối Odoo OK! DB: {ODOO_DB}")
    else:
        await update.message.reply_text(f"❌ Lỗi: {msg}")


async def excel_report_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⌛️ Đang xử lý báo cáo kéo hàng, chờ xíu…")
    excel_buffer, count, msg = get_stock_data()

    if excel_buffer is None:
        await update.message.reply_text(f"❌ Lỗi: {msg}")
        return

    if count > 0:
        await update.message.reply_document(
            document=excel_buffer,
            filename="de_xuat_keo_hang.xlsx",
            caption=f"Đã tìm thấy {count} SP cần kéo hàng."
        )
    else:
        await update.message.reply_text("Không có SP nào cần kéo hàng.")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.message.from_user.first_name
    await update.message.reply_text(
        f"Chào {user}!\n"
        "1. Gõ mã SP để tra tồn\n"
        "2. /keohang tạo báo cáo\n"
        "3. /checkpo để kiểm tra PO\n"
        "4. /ping kiểm tra hệ thống"
    )


async def checkpo_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['waiting_for_po'] = True
    await update.message.reply_text("Gửi file PO (.xlsx) để iem xử lý nhé!")


async def handle_po_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get('waiting_for_po'):
        return

    context.user_data['waiting_for_po'] = False

    doc = update.message.document
    if not doc or not doc.file_name.lower().endswith(".xlsx"):
        await update.message.reply_text("Chỉ hỗ trợ file .xlsx.")
        return

    await update.message.reply_text("⌛ Đang xử lý PO, vui lòng đợi...")

    try:
        f = await doc.get_file()
        file_bytes = await f.download_as_bytearray()
    except Exception as e:
        await update.message.reply_text(f"Lỗi tải file: {e}")
        return

    buf, err = process_po_and_build_report(bytes(file_bytes))
    if buf is None:
        await update.message.reply_text(f"❌ Lỗi: {err}")
        return

    await update.message.reply_document(
        document=buf,
        filename="kiem_tra_po.xlsx",
        caption="Đã xử lý PO!"
    )


# ---------------- Main ----------------
def main():
    if not TELEGRAM_TOKEN:
        logger.error("Thiếu TELEGRAM_TOKEN")
        return

    app = Application.builder().token(TELEGRAM_TOKEN).build()

    # Clear webhook để tránh conflict
    try:
        bot = Bot(token=TELEGRAM_TOKEN)
        asyncio.get_event_loop().run_until_complete(bot.delete_webhook())
    except:
        pass

    # Commands
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", start_command))
    app.add_handler(CommandHandler("ping", ping_command))
    app.add_handler(CommandHandler("keohang", excel_report_command))
    app.add_handler(CommandHandler("checkpo", checkpo_command))

    # Nhận file PO
    app.add_handler(MessageHandler(filters.Document.ALL, handle_po_file))

    # Tra mã SP
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_product_code))

    logger.info("Bot đang chạy…")
    app.run_polling()


# ---------------- HTTP keepalive server ----------------
from http.server import BaseHTTPRequestHandler, HTTPServer

class PingHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type","text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

    def log_message(self, format, *args):
        return

def start_http():
    try:
        server = HTTPServer(("0.0.0.0", 10001), PingHandler)
        server.serve_forever()
    except:
        pass

threading.Thread(target=start_http, daemon=True).start()


# ---------------- Run ----------------
if __name__ == "__main__":
    main()
