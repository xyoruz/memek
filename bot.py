"""
Bot Telegram Dor XL v2 - login nomor MyXL + OTP dulu, baru bisa masuk menu.
Taruh di root folder me-cli (sejajar main.py), jalankan: python bot_dor_v2.py
.env tambahan:  BOT_TOKEN=xxx   (opsional) ADMIN_IDS=id1,id2   (opsional) TG_PROXY=...
  - ADMIN_IDS diisi  -> hanya ID Telegram itu yang bisa memakai bot
  - ADMIN_IDS kosong -> siapa pun boleh, tapi WAJIB login nomor MyXL + OTP sendiri
Sesi tiap pengguna disimpan di bot_sessions.json (berisi refresh token, JANGAN dibagikan).
Semua pembayaran memakai harga asli paket.
"""
import os, io, json, time, asyncio, logging, threading
from collections import defaultdict
from datetime import datetime
import requests, qrcode
from dotenv import load_dotenv
load_dotenv()

from telegram import Update, InlineKeyboardButton as Btn, InlineKeyboardMarkup as Markup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, ContextTypes, filters

from app.util import ensure_api_key
from app.client.ciam import get_otp, submit_otp, get_new_token
from app.client.engsel import (get_family, get_package, get_package_details, get_balance,
                               get_profile, get_tiering_info)
from app.client.purchase.balance import settlement_balance
from app.client.purchase.qris import settlement_qris, get_qris_code
from app.client.purchase.ewallet import settlement_multipayment
from app.type_dict import PaymentItem

logging.basicConfig(level=logging.INFO)
TOKEN = os.getenv("BOT_TOKEN", "")
ADMINS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
HOT1_URL = "https://raw.githubusercontent.com/xyoruz/izinvps/refs/heads/ipuk/Hot.josn"
HOT2_URL = "https://raw.githubusercontent.com/xyoruz/izinvps/refs/heads/ipuk/pg-hot2.json"
API_KEY = ensure_api_key()
SESS_FILE = "bot_sessions.json"
PAGE = 8
OTP_MAX_TRY = 5
OTP_COOLDOWN = 60


# ===================== penyimpanan sesi per pengguna =====================
_FLOCK = threading.Lock()
_ULOCK = defaultdict(threading.Lock)
_TOK: dict = {}  # (uid, number) -> (tokens, waktu)


class LoginRequired(Exception):
    pass


def _load() -> dict:
    try:
        with open(SESS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save(d: dict):
    with open(SESS_FILE, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)
    try:
        os.chmod(SESS_FILE, 0o600)
    except OSError:
        pass


def get_sess(uid) -> dict:
    with _FLOCK:
        return _load().get(str(uid), {"accounts": [], "active": None})


def put_sess(uid, s: dict):
    with _FLOCK:
        d = _load()
        d[str(uid)] = s
        _save(d)


def active_acc(uid):
    s = get_sess(uid)
    return next((a for a in s["accounts"] if a["number"] == s["active"]), None)


def logged_in(uid) -> bool:
    return active_acc(uid) is not None


def upsert_account(uid, acc: dict):
    s = get_sess(uid)
    s["accounts"] = [a for a in s["accounts"] if a["number"] != acc["number"]] + [acc]
    s["active"] = acc["number"]
    put_sess(uid, s)


def remove_account(uid, number):
    s = get_sess(uid)
    s["accounts"] = [a for a in s["accounts"] if a["number"] != number]
    if s["active"] == number:
        s["active"] = s["accounts"][0]["number"] if s["accounts"] else None
    put_sess(uid, s)
    _TOK.pop((uid, number), None)


def get_ctx(uid):
    """Return (api_key, tokens, akun) untuk akun aktif pengguna; refresh token bila perlu."""
    with _ULOCK[uid]:
        acc = active_acc(uid)
        if not acc:
            raise LoginRequired("Belum login")
        key = (uid, acc["number"])
        c = _TOK.get(key)
        if c and time.time() - c[1] < 240:
            return API_KEY, c[0], acc
        try:
            tokens = get_new_token(API_KEY, acc["refresh_token"], acc.get("subscriber_id", ""))
        except ValueError as e:
            raise LoginRequired(str(e))
        if not tokens:
            raise LoginRequired("Sesi tidak valid / kedaluwarsa")
        acc["refresh_token"] = tokens["refresh_token"]
        upsert_account(uid, acc)
        _TOK[key] = (tokens, time.time())
        return API_KEY, tokens, acc


# ===================== fungsi me-cli (blocking, dijalankan di thread) =====================
def norm_number(raw: str) -> str | None:
    """Nomor MyXL wajib diawali 62 (bukan 08). Return None bila tidak valid."""
    n = raw.strip().replace(" ", "").replace("-", "").lstrip("+")
    return n if n.startswith("628") and n.isdigit() and 10 <= len(n) <= 14 else None


def cli_send_otp(number: str):
    return get_otp(number)  # subscriber_id / None


def cli_verify_otp(uid, number: str, otp: str):
    tokens = submit_otp(API_KEY, "SMS", number, otp)
    if not tokens:
        return None
    p = (get_profile(API_KEY, tokens["access_token"], tokens["id_token"]) or {}).get("profile", {})
    acc = {"number": number, "subscriber_id": p.get("subscriber_id", ""),
           "subscription_type": p.get("subscription_type", ""),
           "refresh_token": tokens["refresh_token"]}
    upsert_account(uid, acc)
    _TOK[(uid, number)] = (tokens, time.time())
    return acc


def cli_info(uid) -> str:
    api_key, tokens, acc = get_ctx(uid)
    b = get_balance(api_key, tokens["id_token"]) or {}
    exp = "-"
    if b.get("expired_at"):
        exp = datetime.fromtimestamp(b["expired_at"]).strftime("%Y-%m-%d")
    out = (f'📱 Nomor: {acc["number"]}\nTipe: {acc.get("subscription_type") or "-"}\n'
           f'Pulsa: Rp{b.get("remaining", "?")}\nAktif s/d: {exp}')
    if acc.get("subscription_type") == "PREPAID":
        t = get_tiering_info(api_key, tokens) or {}
        out += f'\nPoin: {t.get("current_point", 0)} | Tier: {t.get("tier", 0)}'
    return out


def cli_family(uid, family_code: str, is_enterprise=None) -> list[dict]:
    api_key, tokens, _ = get_ctx(uid)
    data = get_family(api_key, tokens, family_code, is_enterprise)
    if not data:
        return []
    return [{"label": f'{v["name"]} - {o["name"]} - Rp{o["price"]:,}', "code": o["package_option_code"]}
            for v in data["package_variants"] for o in v["package_options"]]


def cli_hot1_list() -> list[dict]:
    r = requests.get(HOT1_URL, timeout=30)
    r.raise_for_status()
    return [{"label": f'{p["family_name"]} - {p["variant_name"]} - {p["option_name"]}', "raw": p}
            for p in r.json()]


def cli_hot1_resolve(uid, p: dict):
    api_key, tokens, _ = get_ctx(uid)
    fam = get_family(api_key, tokens, p["family_code"], p["is_enterprise"])
    if not fam:
        return None
    for v in fam["package_variants"]:
        if v["name"] == p["variant_name"]:
            for o in v["package_options"]:
                if o["order"] == p["order"]:
                    return o["package_option_code"]
    return None


def cli_hot2_list() -> list[dict]:
    r = requests.get(HOT2_URL, timeout=30)
    r.raise_for_status()
    return [{"label": f'{p["name"]} - {p["price"]}', "raw": p} for p in r.json()]


def cli_option_info(uid, option_code: str):
    api_key, tokens, _ = get_ctx(uid)
    pkg = get_package(api_key, tokens, option_code)
    if not pkg:
        return None
    o = pkg["package_option"]
    return {"name": f'{pkg.get("package_family", {}).get("name", "")} - '
                    f'{pkg.get("package_detail_variant", {}).get("name", "")} - {o["name"]}',
            "price": o["price"], "validity": o.get("validity", "")}


def _build_items(api_key, tokens, kind, val):
    if kind == "opt":
        pkg = get_package(api_key, tokens, val)
        if not pkg:
            return "❌ Gagal ambil detail paket.", None, None
        o = pkg["package_option"]
        item = PaymentItem(
            item_code=val, product_type="", item_price=o["price"],
            item_name=f'{pkg.get("package_detail_variant", {}).get("name", "")} {o["name"]}'.strip(),
            tax=0, token_confirmation=pkg["token_confirmation"])
        return [item], pkg["package_family"]["payment_for"], 0
    items = []  # hot 2 (bundle)
    for pk in val.get("packages", []):
        d = get_package_details(api_key, tokens, pk["family_code"], pk["variant_code"],
                                pk["order"], pk["is_enterprise"], pk["migration_type"])
        if not d:
            return f'❌ Gagal ambil detail {pk["family_code"]}', None, None
        po = d["package_option"]
        items.append(PaymentItem(item_code=po["package_option_code"], product_type="",
                                 item_price=po["price"], item_name=po["name"], tax=0,
                                 token_confirmation=d["token_confirmation"]))
    if not items:
        return "❌ Paket kosong.", None, None
    return items, val.get("payment_for", "BUY_PACKAGE"), val.get("token_confirmation_idx", 0)


WALLETS = {"DANA": "DANA", "OVO": "OVO", "SHOPEEPAY": "ShopeePay", "GOPAY": "GoPay"}


def _result(res) -> str:
    if isinstance(res, dict) and res.get("status") == "SUCCESS":
        return "✅ Permintaan pembelian berhasil dikirim. Cek hasilnya di aplikasi MyXL."
    return f"❌ Gagal: {str(res)[:300]}"


def cli_pay(uid, kind, val, method, wallet=""):
    """Return (teks, qris|None). Selalu harga asli, tanpa overwrite nominal."""
    api_key, tokens, _ = get_ctx(uid)
    items, payment_for, idx = _build_items(api_key, tokens, kind, val)
    if isinstance(items, str):
        return items, None
    total = sum(i["item_price"] for i in items)

    if method == "BALANCE":
        res = settlement_balance(api_key, tokens, items, payment_for, False,
                                 overwrite_amount=total, token_confirmation_idx=idx)
        return _result(res), None

    if method == "QRIS":
        tid = settlement_qris(api_key, tokens, items, payment_for, False, total, idx)
        if not tid or not isinstance(tid, str) or tid.lstrip().startswith("{"):
            return f"❌ Gagal membuat transaksi QRIS: {str(tid)[:200]}", None
        code = get_qris_code(api_key, tokens, tid)
        if not code:
            return "❌ Gagal mengambil kode QRIS.", None
        return f"📱 Scan QRIS untuk bayar Rp{total:,}", code

    if method in WALLETS:
        res = settlement_multipayment(api_key, tokens, items, wallet, method, payment_for,
                                      False, total, idx)
        if not isinstance(res, dict) or res.get("status") != "SUCCESS":
            return f"❌ Gagal: {str(res)[:300]}", None
        if method == "OVO":
            return "✅ Buka aplikasi OVO untuk menyelesaikan pembayaran.", None
        link = res.get("data", {}).get("deeplink", "")
        return (f"✅ Selesaikan pembayaran {WALLETS[method]}:\n{link}" if link
                else f"✅ Transaksi {WALLETS[method]} dibuat. Cek aplikasinya."), None
    return "❌ Metode tidak dikenal.", None


def make_qr_png(data: str) -> io.BytesIO:
    bio = io.BytesIO()
    qrcode.make(data).save(bio, format="PNG")
    bio.seek(0)
    return bio


# ===================== tampilan Telegram =====================
BACK = [Btn("⬅️ Menu", callback_data="menu")]
LOGIN_KB = Markup([[Btn("🔐 Login MyXL", callback_data="login")]])
MENU_KB = Markup([
    [Btn("🔑 Buy Famcode", callback_data="fam")],
    [Btn("🔥 Paket Hot 1", callback_data="h1"), Btn("🔥 Paket Hot 2", callback_data="h2")],
    [Btn("ℹ️ Cek Info Nomor", callback_data="info"), Btn("🔄 Ganti Akun Nomor", callback_data="acc")],
])
PAY_KB = Markup([
    [Btn("💳 Pulsa", callback_data="pay:BALANCE"), Btn("📱 QRIS", callback_data="pay:QRIS")],
    [Btn("DANA", callback_data="pay:DANA"), Btn("OVO", callback_data="pay:OVO")],
    [Btn("ShopeePay", callback_data="pay:SHOPEEPAY"), Btn("GoPay", callback_data="pay:GOPAY")],
    BACK,
])
LOGIN_TEXT = ("🔒 Kamu harus login dulu dengan nomor MyXL "
              "(kode OTP dikirim via SMS ke nomor tersebut).\n"
              "Isi nomor dengan awalan 62, bukan 08.")


def menu_text(uid) -> str:
    a = active_acc(uid)
    return f'Menu Dor XL\nNomor aktif: {a["number"]}' if a else LOGIN_TEXT


def allowed(update: Update) -> bool:
    return not ADMINS or update.effective_user.id in ADMINS


def list_kb(items, prefix, page) -> Markup:
    start = page * PAGE
    rows = [[Btn(it["label"][:60], callback_data=f"{prefix}:{start + i}")]
            for i, it in enumerate(items[start:start + PAGE])]
    nav = []
    if page > 0:
        nav.append(Btn("◀️", callback_data=f"pg:{page - 1}"))
    if start + PAGE < len(items):
        nav.append(Btn("▶️", callback_data=f"pg:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append(BACK)
    return Markup(rows)


def acc_kb(uid) -> Markup:
    s = get_sess(uid)
    rows = [[Btn(("✅ " if a["number"] == s["active"] else "") + a["number"],
                 callback_data=f'sw:{a["number"]}')] for a in s["accounts"]]
    rows.append([Btn("➕ Login nomor baru", callback_data="login")])
    if s["active"]:
        rows.append([Btn("🚪 Logout nomor aktif", callback_data="logout")])
    rows.append(BACK)
    return Markup(rows)


async def show_list(q, ctx, items, prefix, title, page=0):
    ctx.user_data.update(items=items, prefix=prefix, title=title)
    if not items:
        await q.edit_message_text("Tidak ada paket.", reply_markup=Markup([BACK]))
        return
    await q.edit_message_text(f"{title}\nHal {page + 1}/{(len(items) - 1) // PAGE + 1}",
                              reply_markup=list_kb(items, prefix, page))


def confirm_kb():
    return Markup([[Btn("✅ Lanjut", callback_data="buy"), Btn("❌ Batal", callback_data="menu")]])


async def run_pay(msg, ctx, uid, method, wallet=""):
    kind, val = ctx.user_data.pop("buy")
    await msg.edit_text("Memproses...")
    text, qris = await asyncio.to_thread(cli_pay, uid, kind, val, method, wallet)
    await msg.edit_text(text, reply_markup=Markup([BACK]))
    if qris:  # kode QRIS sengaja tidak ditampilkan sebagai teks, hanya gambar
        await msg.reply_photo(make_qr_png(qris), caption=text)


# ===================== handler =====================
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    ctx.user_data.clear()
    uid = update.effective_user.id
    await update.message.reply_text(menu_text(uid),
                                    reply_markup=MENU_KB if logged_in(uid) else LOGIN_KB)


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    q = update.callback_query
    await q.answer()
    d, ud, uid = q.data, ctx.user_data, update.effective_user.id
    try:
        # ---- login / akun (boleh tanpa login) ----
        if d == "login":
            ud.clear()
            ud["state"] = "number"
            await q.edit_message_text(
                "Kirim nomor MyXL kamu dengan awalan 62, bukan 08.\n"
                "Contoh: 628123456789",
                reply_markup=Markup([BACK]))
            return
        if not logged_in(uid) and d != "menu":
            await q.edit_message_text(LOGIN_TEXT, reply_markup=LOGIN_KB)
            return

        if d == "menu":
            ud.clear()
            await q.edit_message_text(menu_text(uid), reply_markup=MENU_KB if logged_in(uid) else LOGIN_KB)

        elif d == "info":
            await q.edit_message_text("Mengambil info...")
            await q.edit_message_text(await asyncio.to_thread(cli_info, uid), reply_markup=Markup([BACK]))

        elif d == "acc":
            await q.edit_message_text("Pilih akun / kelola login:", reply_markup=acc_kb(uid))

        elif d.startswith("sw:"):
            s = get_sess(uid)
            num = d[3:]
            if any(a["number"] == num for a in s["accounts"]):
                s["active"] = num
                put_sess(uid, s)
                await q.edit_message_text("Mengganti akun...")
                await asyncio.to_thread(get_ctx, uid)  # validasi sesi
                await q.edit_message_text(menu_text(uid), reply_markup=MENU_KB)

        elif d == "logout":
            a = active_acc(uid)
            if a:
                remove_account(uid, a["number"])
            ud.clear()
            await q.edit_message_text(menu_text(uid), reply_markup=MENU_KB if logged_in(uid) else LOGIN_KB)

        # ---- menu dor ----
        elif d == "fam":
            ud.clear()
            ud["state"] = "fam"
            await q.edit_message_text("Kirim family code:", reply_markup=Markup([BACK]))

        elif d == "h1":
            await q.edit_message_text("Memuat Hot 1...")
            await show_list(q, ctx, await asyncio.to_thread(cli_hot1_list), "p1", "🔥 Paket Hot 1")

        elif d == "h2":
            await q.edit_message_text("Memuat Hot 2...")
            await show_list(q, ctx, await asyncio.to_thread(cli_hot2_list), "p2", "🔥 Paket Hot 2")

        elif d.startswith("pg:"):
            await show_list(q, ctx, ud["items"], ud["prefix"], ud["title"], int(d[3:]))

        elif d[:3] in ("pf:", "p1:", "p2:"):
            kind, idx = d[:2], int(d[3:])
            it = ud["items"][idx]
            if kind == "p2":
                ud["buy"] = ("hot2", it["raw"])
                await q.edit_message_text(
                    f'{it["raw"]["name"]}\nHarga: {it["raw"].get("price", "-")}\n\n'
                    f'{str(it["raw"].get("detail", ""))[:800]}\n\nLanjut beli?', reply_markup=confirm_kb())
                return
            await q.edit_message_text("Memuat detail...")
            code = it["code"] if kind == "pf" else await asyncio.to_thread(cli_hot1_resolve, uid, it["raw"])
            info = await asyncio.to_thread(cli_option_info, uid, code) if code else None
            if not info:
                await q.edit_message_text("Gagal ambil detail paket.", reply_markup=Markup([BACK]))
                return
            ud["buy"] = ("opt", code)
            await q.edit_message_text(
                f'{info["name"]}\nHarga: Rp{info["price"]:,}\nMasa aktif: {info["validity"]}\n\nLanjut beli?',
                reply_markup=confirm_kb())

        elif d == "buy":
            if "buy" in ud:
                await q.edit_message_text("Pilih metode pembayaran:", reply_markup=PAY_KB)

        elif d.startswith("pay:"):
            method = d[4:]
            if "buy" not in ud:
                return
            if method in ("DANA", "OVO"):
                ud["state"] = "wallet"
                ud["wallet_method"] = method
                await q.edit_message_text(f"Kirim nomor {method} (contoh 08123456789):",
                                          reply_markup=Markup([BACK]))
            else:
                await run_pay(q.message, ctx, uid, method)

    except LoginRequired as e:
        await q.edit_message_text(f"🔒 Sesi berakhir, login ulang. ({e})", reply_markup=LOGIN_KB)
    except Exception as e:
        logging.exception("error")
        await q.edit_message_text(f"Error: {e}", reply_markup=Markup([BACK]))


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    ud, uid = ctx.user_data, update.effective_user.id
    text = update.message.text.strip()
    st = ud.get("state")

    if st == "number":
        num = norm_number(text)
        if not num:
            await update.message.reply_text(
                "❌ Nomor tidak valid. Gunakan awalan 62, bukan 08.\nContoh: 628123456789")
            return
        if time.time() - ud.get("otp_at", 0) < OTP_COOLDOWN:
            await update.message.reply_text(f"Tunggu {OTP_COOLDOWN} detik sebelum minta OTP lagi.")
            return
        msg = await update.message.reply_text("Mengirim OTP...")
        sub = await asyncio.to_thread(cli_send_otp, num)
        ud["otp_at"] = time.time()
        if not sub:
            await msg.edit_text("❌ Gagal mengirim OTP. Cek nomor lalu coba lagi.", reply_markup=LOGIN_KB)
            ud.pop("state", None)
            return
        ud.update(state="otp", login_number=num, otp_try=OTP_MAX_TRY)
        await msg.edit_text(f"✅ OTP dikirim ke {num}. Kirim 6 digit kode OTP:", reply_markup=Markup([BACK]))
