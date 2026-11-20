"""Moliyachi Telegram bot: kirim-chiqim nazorati va oylik hisobotlar."""
import asyncio
import csv
import hashlib
import html
import io
import logging
import os
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F, Router
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiogram.client.default import DefaultBotProperties
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
)
from dotenv import load_dotenv

import db

load_dotenv()
TOKEN = os.getenv("BOT_TOKEN")
try:
    TZ = ZoneInfo(os.getenv("TIMEZONE", "Asia/Tashkent"))
except Exception:  # tzdata o'rnatilmagan bo'lsa — Toshkent vaqti (UTC+5)
    TZ = timezone(timedelta(hours=5))

KIRIM_CATS = ["Maosh", "Biznes", "Sovg'a", "Qarz qaytdi", "Boshqa"]
CHIQIM_CATS = [
    "Oziq-ovqat", "Transport", "Uy-joy", "Kommunal", "Aloqa", "Kiyim",
    "Sog'liq", "Ta'lim", "Ko'ngilochar", "Qarz berildi", "Boshqa",
]
OYLAR = ["Yanvar", "Fevral", "Mart", "Aprel", "May", "Iyun", "Iyul",
         "Avgust", "Sentabr", "Oktabr", "Noyabr", "Dekabr"]

BTN_KIRIM = "➕ Kirim"
BTN_CHIQIM = "➖ Chiqim"
BTN_BALANS = "💰 Balans"
BTN_HISOBOT = "📅 Oylik hisobot"
BTN_OXIRGI = "📜 Oxirgi amallar"
BTN_ARXIV = "🗂 Oylar arxivi"

MENU = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=BTN_KIRIM), KeyboardButton(text=BTN_CHIQIM)],
        [KeyboardButton(text=BTN_BALANS), KeyboardButton(text=BTN_HISOBOT)],
        [KeyboardButton(text=BTN_OXIRGI), KeyboardButton(text=BTN_ARXIV)],
    ],
    resize_keyboard=True,
)

MENU_BUTTONS = {BTN_KIRIM, BTN_CHIQIM, BTN_BALANS, BTN_HISOBOT, BTN_OXIRGI, BTN_ARXIV}

router = Router()


class AddTx(StatesGroup):
    category = State()
    amount = State()
    note = State()


# ---------- Yordamchi funksiyalar ----------

def now_str() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def current_month() -> str:
    return datetime.now(TZ).strftime("%Y-%m")


def shift_month(month: str, delta: int) -> str:
    y, m = map(int, month.split("-"))
    idx = y * 12 + (m - 1) + delta
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def month_title(month: str) -> str:
    y, m = month.split("-")
    return f"{OYLAR[int(m) - 1]} {y}"


def money(v: float) -> str:
    s = f"{v:,.0f}".replace(",", " ")
    return f"{s} so'm"


def parse_amount(text: str):
    """'50000', '50 000', '50k', '1.5mln' kabi yozuvlarni tushunadi."""
    t = text.lower().replace(" ", "").replace(",", "")
    for suffix in ("so'm", "som", "сум"):
        t = t.removesuffix(suffix)
    mult = 1
    if t.endswith("mln"):
        mult, t = 1_000_000, t[:-3]
    elif t.endswith("k"):
        mult, t = 1_000, t[:-1]
    try:
        value = float(t) * mult
    except ValueError:
        return None
    return value if value > 0 else None


def cats_for(tx_type: str):
    return KIRIM_CATS if tx_type == "kirim" else CHIQIM_CATS


def category_kb(tx_type: str) -> InlineKeyboardMarkup:
    cats = cats_for(tx_type)
    rows, row = [], []
    for i, name in enumerate(cats):
        row.append(InlineKeyboardButton(text=name, callback_data=f"cat:{i}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="❌ Bekor qilish", callback_data="cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


SKIP_KB = InlineKeyboardMarkup(inline_keyboard=[[
    InlineKeyboardButton(text="⏭ Izohsiz saqlash", callback_data="skipnote")
]])


def build_report(user_id: int, month: str) -> str:
    rows = db.month_by_category(user_id, month)
    kirim = sum(r["total"] for r in rows if r["type"] == "kirim")
    chiqim = sum(r["total"] for r in rows if r["type"] == "chiqim")
    sof = kirim - chiqim

    lines = [f"📅 <b>{month_title(month)} hisoboti</b>", ""]
    if not rows:
        lines.append("Bu oyda hech qanday amal yo'q.")
        return "\n".join(lines)

    lines += [
        f"🟢 Kirim: <b>{money(kirim)}</b>",
        f"🔴 Chiqim: <b>{money(chiqim)}</b>",
        f"{'✅' if sof >= 0 else '⚠️'} Sof qoldiq: <b>{money(sof)}</b>",
    ]
    if kirim > 0:
        lines.append(f"📈 Tejash darajasi: {sof / kirim * 100:.0f}%")

    for tx_type, title, total in (("kirim", "Kirim manbalari", kirim),
                                  ("chiqim", "Xarajatlar", chiqim)):
        part = [r for r in rows if r["type"] == tx_type]
        if not part:
            continue
        lines += ["", f"<b>{title}:</b>"]
        for r in part:
            pct = r["total"] / total * 100 if total else 0
            lines.append(
                f"• {html.escape(r['category'])} — {money(r['total'])} "
                f"({pct:.0f}%, {r['cnt']} ta)"
            )
    return "\n".join(lines)


def report_kb(month: str) -> InlineKeyboardMarkup:
    nav = [InlineKeyboardButton(text="◀️ Oldingi", callback_data=f"rep:{shift_month(month, -1)}")]
    if month < current_month():
        nav.append(InlineKeyboardButton(text="Keyingi ▶️", callback_data=f"rep:{shift_month(month, 1)}"))
    return InlineKeyboardMarkup(inline_keyboard=[
        nav,
        [InlineKeyboardButton(text="📥 Excel (CSV) yuklab olish", callback_data=f"csv:{month}")],
    ])


def save_tx(user_id, tx_type, amount, category, note):
    tx_id = db.add_transaction(user_id, tx_type, amount, category, note, now_str())
    kirim, chiqim = db.balance(user_id)
    icon = "🟢" if tx_type == "kirim" else "🔴"
    text = (
        f"{icon} <b>Saqlandi</b> (#{tx_id})\n"
        f"Turi: {tx_type.capitalize()}\n"
        f"Kategoriya: {html.escape(category)}\n"
        f"Summa: {money(amount)}\n"
    )
    if note:
        text += f"Izoh: {html.escape(note)}\n"
    text += f"\n💰 Joriy balans: <b>{money(kirim - chiqim)}</b>"
    return text


# ---------- Buyruqlar ----------

@router.message(CommandStart())
async def cmd_start(msg: Message, state: FSMContext):
    await state.clear()
    db.add_user(msg.from_user.id, msg.from_user.full_name, now_str())
    await msg.answer(
        f"Assalomu alaykum, {html.escape(msg.from_user.first_name)}! 👋\n\n"
        "Men sizning shaxsiy <b>moliyachi</b> botingizman. "
        "Kirim va chiqimlaringizni yozib boraman, har oy hisobot tayyorlayman.\n\n"
        "<b>Tez kiritish:</b>\n"
        "<code>+5000000 maosh</code> — kirim\n"
        "<code>-25000 transport taksi</code> — chiqim\n"
        "<code>-50k oziq-ovqat</code> — 50 000 so'm\n\n"
        "Yoki pastdagi tugmalardan foydalaning 👇",
        reply_markup=MENU,
    )


@router.message(Command("help"))
async def cmd_help(msg: Message):
    await msg.answer(
        "<b>Buyruqlar:</b>\n"
        "/start — bosh menyu\n"
        "/hisobot — joriy oy hisoboti\n"
        "/hisobot 2026-10 — tanlangan oy hisoboti\n"
        "/arxiv — barcha oylar jamlanmasi\n"
        "/ochir 15 — #15 raqamli amalni o'chirish\n"
        "/bekor — joriy kiritishni bekor qilish\n\n"
        "<b>Tez kiritish:</b> <code>+summa kategoriya izoh</code> yoki "
        "<code>-summa kategoriya izoh</code>",
        reply_markup=MENU,
    )


@router.message(Command("bekor"))
async def cmd_cancel(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Bekor qilindi.", reply_markup=MENU)


@router.callback_query(F.data == "cancel")
async def cb_cancel(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    await cb.message.edit_text("❌ Bekor qilindi.")
    await cb.answer()


# ---------- Bosqichma-bosqich kiritish ----------

@router.message(F.text.in_({BTN_KIRIM, BTN_CHIQIM}))
async def start_add(msg: Message, state: FSMContext):
    tx_type = "kirim" if msg.text == BTN_KIRIM else "chiqim"
    await state.clear()
    await state.update_data(tx_type=tx_type)
    await state.set_state(AddTx.category)
    await msg.answer(
        f"{'🟢 Kirim' if tx_type == 'kirim' else '🔴 Chiqim'} — kategoriyani tanlang:",
        reply_markup=category_kb(tx_type),
    )


@router.callback_query(AddTx.category, F.data.startswith("cat:"))
async def pick_category(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    category = cats_for(data["tx_type"])[int(cb.data.split(":")[1])]
    await state.update_data(category=category)
    await state.set_state(AddTx.amount)
    await cb.message.edit_text(
        f"Kategoriya: <b>{html.escape(category)}</b>\n\n"
        "Summani kiriting (masalan: <code>150000</code>, <code>150k</code>, <code>1.2mln</code>):"
    )
    await cb.answer()


@router.message(AddTx.amount, F.text.in_(MENU_BUTTONS))
@router.message(AddTx.note, F.text.in_(MENU_BUTTONS))
async def interrupted(msg: Message, state: FSMContext):
    await state.clear()
    await msg.answer("Kiritish bekor qilindi. Tugmani qaytadan bosing.", reply_markup=MENU)


@router.message(AddTx.amount)
async def enter_amount(msg: Message, state: FSMContext):
    amount = parse_amount(msg.text or "")
    if amount is None:
        await msg.answer("⚠️ Summani to'g'ri kiriting. Masalan: <code>150000</code>")
        return
    await state.update_data(amount=amount)
    await state.set_state(AddTx.note)
    await msg.answer(f"Summa: <b>{money(amount)}</b>\n\nIzoh yozing yoki o'tkazib yuboring:",
                     reply_markup=SKIP_KB)


async def finish_add(user_id: int, state: FSMContext, note):
    data = await state.get_data()
    await state.clear()
    return save_tx(user_id, data["tx_type"], data["amount"], data["category"], note)


@router.message(AddTx.note)
async def enter_note(msg: Message, state: FSMContext):
    text = await finish_add(msg.from_user.id, state, (msg.text or "").strip()[:200] or None)
    await msg.answer(text, reply_markup=MENU)


@router.callback_query(AddTx.note, F.data == "skipnote")
async def skip_note(cb: CallbackQuery, state: FSMContext):
    text = await finish_add(cb.from_user.id, state, None)
    await cb.message.edit_text(text)
    await cb.answer()


# ---------- Ko'rish ----------

@router.message(F.text == BTN_BALANS)
async def show_balance(msg: Message):
    kirim, chiqim = db.balance(msg.from_user.id)
    month = current_month()
    rows = db.month_by_category(msg.from_user.id, month)
    mk = sum(r["total"] for r in rows if r["type"] == "kirim")
    mc = sum(r["total"] for r in rows if r["type"] == "chiqim")
    await msg.answer(
        f"💰 <b>Umumiy balans: {money(kirim - chiqim)}</b>\n"
        f"Jami kirim: {money(kirim)}\n"
        f"Jami chiqim: {money(chiqim)}\n\n"
        f"<b>{month_title(month)}:</b>\n"
        f"🟢 {money(mk)}   🔴 {money(mc)}"
    )


@router.message(F.text == BTN_HISOBOT)
@router.message(Command("hisobot"))
async def show_report(msg: Message):
    month = current_month()
    parts = (msg.text or "").split()
    if len(parts) > 1 and re.fullmatch(r"\d{4}-\d{2}", parts[1]):
        month = parts[1]
    await msg.answer(build_report(msg.from_user.id, month), reply_markup=report_kb(month))


@router.callback_query(F.data.startswith("rep:"))
async def nav_report(cb: CallbackQuery):
    month = cb.data.split(":")[1]
    await cb.message.edit_text(build_report(cb.from_user.id, month), reply_markup=report_kb(month))
    await cb.answer()


@router.callback_query(F.data.startswith("csv:"))
async def export_csv(cb: CallbackQuery):
    month = cb.data.split(":")[1]
    rows = db.month_transactions(cb.from_user.id, month)
    if not rows:
        await cb.answer("Bu oyda ma'lumot yo'q", show_alert=True)
        return
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["ID", "Sana", "Turi", "Kategoriya", "Summa", "Izoh"])
    for r in rows:
        w.writerow([r["id"], r["created_at"], r["type"], r["category"],
                    f"{r['amount']:.0f}", r["note"] or ""])
    data = ("﻿" + buf.getvalue()).encode("utf-8")  # BOM — Excel to'g'ri ochishi uchun
    await cb.message.answer_document(
        BufferedInputFile(data, filename=f"hisobot_{month}.csv"),
        caption=f"📥 {month_title(month)} — barcha amallar",
    )
    await cb.answer()


@router.message(F.text == BTN_OXIRGI)
async def show_recent(msg: Message):
    rows = db.recent(msg.from_user.id, 10)
    if not rows:
        await msg.answer("Hali hech qanday amal kiritilmagan.")
        return
    lines = ["📜 <b>Oxirgi 10 ta amal:</b>", ""]
    buttons = []
    for r in rows:
        icon = "🟢" if r["type"] == "kirim" else "🔴"
        note = f" — {html.escape(r['note'])}" if r["note"] else ""
        lines.append(
            f"{icon} <b>#{r['id']}</b> {r['created_at'][:16]}\n"
            f"    {html.escape(r['category'])}: {money(r['amount'])}{note}"
        )
        buttons.append(InlineKeyboardButton(text=f"🗑 #{r['id']}", callback_data=f"del:{r['id']}"))
    kb = InlineKeyboardMarkup(inline_keyboard=[buttons[i:i + 5] for i in range(0, len(buttons), 5)])
    await msg.answer("\n".join(lines), reply_markup=kb)


@router.callback_query(F.data.startswith("del:"))
async def cb_delete(cb: CallbackQuery):
    tx_id = int(cb.data.split(":")[1])
    ok = db.delete_transaction(cb.from_user.id, tx_id)
    await cb.answer(f"#{tx_id} o'chirildi" if ok else "Topilmadi (allaqachon o'chirilgan)",
                    show_alert=True)


@router.message(Command("ochir"))
async def cmd_delete(msg: Message):
    parts = (msg.text or "").split()
    if len(parts) < 2 or not parts[1].lstrip("#").isdigit():
        await msg.answer("Foydalanish: <code>/ochir 15</code>")
        return
    tx_id = int(parts[1].lstrip("#"))
    ok = db.delete_transaction(msg.from_user.id, tx_id)
    await msg.answer(f"🗑 #{tx_id} o'chirildi." if ok else "Bunday amal topilmadi.")


@router.message(F.text == BTN_ARXIV)
@router.message(Command("arxiv"))
async def show_archive(msg: Message):
    rows = db.monthly_archive(msg.from_user.id)
    if not rows:
        await msg.answer("Arxiv bo'sh.")
        return
    lines = ["🗂 <b>Oylar bo'yicha jamlanma:</b>", ""]
    for r in rows:
        sof = r["kirim"] - r["chiqim"]
        lines.append(
            f"<b>{month_title(r['month'])}</b>\n"
            f"   🟢 {money(r['kirim'])}  🔴 {money(r['chiqim'])}  "
            f"{'✅' if sof >= 0 else '⚠️'} {money(sof)}"
        )
    lines.append("\nBatafsil ko'rish: <code>/hisobot YYYY-MM</code>")
    await msg.answer("\n".join(lines))


# ---------- Tez kiritish: "+50000 maosh" / "-20000 ovqat" ----------

QUICK_RE = re.compile(r"^([+-])\s*([\d\s.,]+(?:k|mln)?)\s*(.*)$", re.IGNORECASE)


def match_category(tx_type: str, text: str):
    """Izohning birinchi so'zi kategoriyaga mos kelsa — o'sha kategoriya."""
    if not text:
        return "Boshqa", None
    first = text.split()[0].lower()
    if len(first) < 3:
        return "Boshqa", text
    for cat in cats_for(tx_type):
        if cat.lower().startswith(first) or first.startswith(cat.lower().split("-")[0]):
            rest = text[len(text.split()[0]):].strip()
            return cat, rest or None
    return "Boshqa", text


@router.message(F.text.regexp(QUICK_RE))
async def quick_add(msg: Message, state: FSMContext):
    await state.clear()
    m = QUICK_RE.match(msg.text.strip())
    amount = parse_amount(m.group(2))
    if amount is None:
        await msg.answer("⚠️ Summani tushunmadim. Masalan: <code>-25000 transport</code>")
        return
    tx_type = "kirim" if m.group(1) == "+" else "chiqim"
    category, note = match_category(tx_type, m.group(3).strip())
    await msg.answer(save_tx(msg.from_user.id, tx_type, amount, category, note[:200] if note else None),
                     reply_markup=MENU)


@router.message()
async def fallback(msg: Message):
    await msg.answer(
        "Tushunmadim 🤔 Tugmalardan foydalaning yoki shunday yozing:\n"
        "<code>+500000 maosh</code> yoki <code>-30000 oziq-ovqat</code>\n\n/help — yordam",
        reply_markup=MENU,
    )


# ---------- O'tgan oy hisobotini avtomatik yuborish ----------

async def send_monthly_if_due(bot: Bot, user_id: int):
    """O'tgan oy hisoboti hali yuborilmagan bo'lsa — yuboradi (bir marta)."""
    prev = shift_month(current_month(), -1)
    u = db.get_user(user_id)
    if not u or u["last_report"] == prev:
        return
    db.set_last_report(user_id, prev)
    if not db.month_by_category(user_id, prev):
        return  # o'tgan oyda amal bo'lmagan — bo'sh hisobot yubormaymiz
    try:
        await bot.send_message(
            user_id,
            "🔔 <b>O'tgan oy yakunlandi!</b>\n\n" + build_report(user_id, prev),
            reply_markup=report_kb(prev),
        )
    except Exception as e:  # foydalanuvchi botni bloklagan bo'lishi mumkin
        logging.warning("Hisobot yuborilmadi %s: %s", user_id, e)


@router.message.outer_middleware()
async def monthly_report_middleware(handler, event: Message, data: dict):
    # Webhook rejimida fon jarayon yo'q: yangi oydagi birinchi xabarda hisobot yuboriladi
    if event.from_user:
        db.add_user(event.from_user.id, event.from_user.full_name, now_str())
        await send_monthly_if_due(data["bot"], event.from_user.id)
    return await handler(event, data)


async def monthly_report_loop(bot: Bot):
    """Polling rejimi uchun: har oyning 1-sanasida soat 9:00 dan keyin yuboradi."""
    while True:
        now = datetime.now(TZ)
        if now.day == 1 and now.hour >= 9:
            for u in db.all_users():
                await send_monthly_if_due(bot, u["user_id"])
                await asyncio.sleep(0.05)
        await asyncio.sleep(1800)  # har 30 daqiqada tekshiradi


async def keep_awake(url: str):
    """Render tekin tarifi 15 daqiqa so'rov bo'lmasa uxlaydi — har 10 daqiqada o'zimizni chaqiramiz."""
    async with aiohttp.ClientSession() as s:
        while True:
            await asyncio.sleep(600)
            try:
                async with s.get(url, timeout=aiohttp.ClientTimeout(total=30)) as r:
                    logging.info("keep-awake: %s", r.status)
            except Exception as e:
                logging.warning("keep-awake xato: %s", e)


async def run_webhook(bot: Bot, dp: Dispatcher, base_url: str):
    secret = os.getenv("WEBHOOK_SECRET") or hashlib.sha256(TOKEN.encode()).hexdigest()[:40]
    app = web.Application()

    async def health(_request):
        return web.Response(text="Moliyachi bot ishlayapti ✅")

    app.router.add_get("/", health)
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=secret).register(app, path="/webhook")
    setup_application(app, dp, bot=bot)

    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logging.info("Webhook server %s portda ishga tushdi", port)

    await bot.set_webhook(
        f"{base_url}/webhook",
        secret_token=secret,
        allowed_updates=dp.resolve_used_update_types(),
    )
    logging.info("Webhook o'rnatildi: %s/webhook", base_url)

    asyncio.create_task(monthly_report_loop(bot))
    asyncio.create_task(keep_awake(base_url + "/"))
    await asyncio.Event().wait()  # abadiy ishlaydi


async def main():
    if not TOKEN:
        raise SystemExit("BOT_TOKEN topilmadi. .env faylini yarating (.env.example ga qarang).")
    logging.basicConfig(level=logging.INFO)
    db.init_db()
    logging.info("Baza: %s", "PostgreSQL" if db.PG else db.DB_PATH)
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
    dp = Dispatcher()
    dp.include_router(router)

    # Render o'zi RENDER_EXTERNAL_URL beradi -> webhook rejimi; aks holda polling
    base_url = os.getenv("WEBHOOK_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL")
    if base_url:
        await run_webhook(bot, dp, base_url.rstrip("/"))
    else:
        asyncio.create_task(monthly_report_loop(bot))
        await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
