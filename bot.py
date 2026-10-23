# -*- coding: utf-8 -*-
"""
بوت تيليجرام ذكاء اصطناعي (Gemini) مع مودات برومبت يديرها الأدمن.

المتغيرات المطلوبة (Environment Variables):
  TELEGRAM_TOKEN  : توكن البوت من @BotFather
  GEMINI_API_KEY  : مفتاح Gemini (AQ.… أو AIza…)
  ADMIN_IDS       : رقم/أرقام الأدمن مفصولة بفواصل (احصل عليه بإرسال /id للبوت)
اختيارية:
  GEMINI_MODEL    : الافتراضي gemini-flash-latest
  DATA_FILE       : مسار ملف حفظ المودات (الافتراضي data.json)
"""
import asyncio
import json
import logging
import os
import re
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
from telegram import BotCommand
from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import BadRequest, Conflict
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ============================ الإعدادات ============================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
ADMIN_IDS = {int(x) for x in re.findall(r"\d+", os.environ.get("ADMIN_IDS", ""))}
DATA_FILE = os.environ.get("DATA_FILE", "data.json")
MAX_HISTORY = 20  # عدد الرسائل المحفوظة لكل محادثة (سؤال + جواب = 2)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# مهم: مكتبة httpx تطبع رابط الطلب وفيه توكن البوت، لذلك نخفض مستوى طباعتها
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("bot")


# ============================ التخزين ============================
def load_data() -> dict:
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            d = json.load(f)
        d.setdefault("modes", {})
        d.setdefault("active", None)
        return d
    except (FileNotFoundError, json.JSONDecodeError):
        return {"modes": {}, "active": None}


def save_data() -> None:
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(DB, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)


DB = load_data()  # {"modes": {id: {"name":..., "prompt":...}}, "active": id | None}
HISTORY: dict = {}  # chat_id -> {"mode": id, "prompt": str, "msgs": [...]}
LOCKS: dict = {}  # chat_id -> asyncio.Lock


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


# ============================ الأزرار ============================
def b(text: str, data: str, style: str | None = None) -> Btn:
    """style: 'success' أخضر | 'danger' أحمر | 'primary' أزرق (تظهر في تيليجرام الحديث)"""
    return Btn(text, callback_data=data, style=style)


def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([list(r) for r in rows])


CANCEL_KB = kb([b("❌ إلغاء", "cancel", "danger")])


def home_view():
    mode = DB["modes"].get(DB["active"])
    status = (
        f"🟢 المود النشط: «{mode['name']}»"
        if mode
        else "🔴 لا يوجد مود نشط — البوت لا يرد على أحد"
    )
    text = f"🎛 لوحة تحكم الأدمن\n\n{status}\n📦 عدد المودات: {len(DB['modes'])}"
    markup = kb(
        [b("➕ إضافة مود جديد", "add", "success")],
        [b("📋 المودات", "list", "primary")],
        [b("⛔ إيقاف الرد", "off", "danger")],
    )
    return text, markup


def list_view():
    if not DB["modes"]:
        return "📭 لا توجد مودات بعد.", kb(
            [b("➕ إضافة مود", "add", "success")], [b("🏠 الرئيسية", "home")]
        )
    rows = []
    for mid, m in DB["modes"].items():
        active = DB["active"] == mid
        rows.append(
            [b(("🟢 " if active else "⚪ ") + m["name"], f"m:{mid}", "success" if active else None)]
        )
    rows.append([b("➕ إضافة مود", "add", "success"), b("🏠 الرئيسية", "home")])
    return "📋 اختر مود لإدارته:", kb(*rows)


def mode_view(mid: str, notice: str = ""):
    m = DB["modes"][mid]
    active = DB["active"] == mid
    preview = m["prompt"] if len(m["prompt"]) <= 500 else m["prompt"][:500] + "…"
    text = (f"{notice}\n\n" if notice else "") + (
        f"🎭 المود: {m['name']}\n"
        f"الحالة: {'🟢 نشط (هو الذي يرد الآن)' if active else '⚪ غير نشط'}\n\n"
        f"📝 البرومبت:\n{preview}"
    )
    first_row = (
        [b("⏸ إلغاء التفعيل", "off", "danger")]
        if active
        else [b("✅ تفعيل هذا المود", f"act:{mid}", "success")]
    )
    markup = kb(
        first_row,
        [b("✏️ تعديل الاسم", f"en:{mid}", "primary"), b("📝 تعديل البرومبت", f"ep:{mid}", "primary")],
        [b("📄 عرض البرومبت كاملاً", f"show:{mid}")],
        [b("🗑 حذف", f"del:{mid}", "danger")],
        [b("⬅️ رجوع", "list")],
    )
    return text, markup


async def edit(q, text: str, markup=None) -> None:
    try:
        await q.edit_message_text(text, reply_markup=markup)
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


# ============================ Gemini ============================
class GeminiError(Exception):
    pass


async def ask_gemini(system_prompt: str, contents: list) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    payload = {
        "system_instruction": {"parts": [{"text": system_prompt}]},
        "contents": contents,
        "generationConfig": {"temperature": 0.8, "maxOutputTokens": 4096},
    }
    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            r = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as e:
        raise GeminiError(f"تعذر الاتصال بـ Gemini ({type(e).__name__})")

    if r.status_code != 200:
        try:
            msg = r.json()["error"]["message"]
        except Exception:
            msg = r.text[:200]
        hints = {
            401: "المفتاح غير صالح، تأكد من GEMINI_API_KEY",
            403: "المفتاح لا يملك صلاحية لهذا الموديل",
            404: "اسم الموديل غير موجود، غيّر GEMINI_MODEL",
            429: "تجاوزت الحد المسموح (Quota)، انتظر قليلاً",
        }
        raise GeminiError(
            f"خطأ Gemini ({r.status_code}): {hints.get(r.status_code, '')}\n{msg[:300]}"
        )

    data = r.json()
    cands = data.get("candidates") or []
    if not cands:
        block = data.get("promptFeedback", {}).get("blockReason", "غير معروف")
        raise GeminiError(f"لم يصل رد من Gemini (سبب الحظر: {block})")
    parts = cands[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
    if not text:
        raise GeminiError("وصل رد فارغ (ربما حظره فلتر الأمان)")
    return text


def clean(t: str) -> str:
    """تنظيف بسيط لتنسيق Markdown حتى يظهر النص جميلاً في تيليجرام"""
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t, flags=re.S)
    t = re.sub(r"^\s*[\*\-]\s+", "• ", t, flags=re.M)
    t = re.sub(r"^#{1,6}\s*", "", t, flags=re.M)
    return t


def chunks(t: str, n: int = 4000):
    while len(t) > n:
        cut = t.rfind("\n", 0, n)
        if cut < n // 2:
            cut = n
        yield t[:cut]
        t = t[cut:].lstrip("\n")
    if t.strip():
        yield t


async def chat_with_ai(update: Update, text: str) -> None:
    msg = update.message
    chat_id = update.effective_chat.id
    admin = is_admin(update.effective_user.id)
    active = DB["active"]
    mode = DB["modes"].get(active)

    if not mode:
        await msg.reply_text(
            "⚠️ لا يوجد مود نشط. افتح /start وفعّل مود."
            if admin
            else "⏸ البوت متوقف حالياً، حاول لاحقاً."
        )
        return

    lock = LOCKS.setdefault(chat_id, asyncio.Lock())
    async with lock:
        h = HISTORY.get(chat_id)
        # عند تغيير المود أو تعديل برومبته تبدأ المحادثة من جديد
        if not h or h["mode"] != active or h["prompt"] != mode["prompt"]:
            h = HISTORY[chat_id] = {"mode": active, "prompt": mode["prompt"], "msgs": []}

        contents = h["msgs"] + [{"role": "user", "parts": [{"text": text}]}]
        await update.get_bot().send_chat_action(chat_id, ChatAction.TYPING)
        try:
            reply = await ask_gemini(mode["prompt"], contents)
        except GeminiError as e:
            log.error("Gemini error: %s", e)
            await msg.reply_text(
                f"❌ {e}" if admin else "⚠️ حدث خطأ مؤقت، حاول مرة أخرى بعد قليل."
            )
            return

        msgs = (contents + [{"role": "model", "parts": [{"text": reply}]}])[-MAX_HISTORY:]
        while msgs and msgs[0]["role"] != "user":
            msgs.pop(0)
        h["msgs"] = msgs

    for part in chunks(clean(reply)):
        await msg.reply_text(part)


# ============================ الأوامر ============================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    context.user_data.pop("state", None)

    if not ADMIN_IDS:
        await update.message.reply_text(
            f"⚠️ لم يتم تعيين أدمن بعد.\nرقمك: {uid}\n"
            "ضعه في متغير ADMIN_IDS ثم أعد تشغيل البوت."
        )
        return

    if is_admin(uid):
        text, markup = home_view()
        await update.message.reply_text(text, reply_markup=markup)
        return

    if DB["modes"].get(DB["active"]):
        await update.message.reply_text(
            "👋 أهلاً بك! أنا مساعدك الذكي.\nاكتب رسالتك وسأرد عليك مباشرة.",
            reply_markup=kb([b("🔄 محادثة جديدة", "newchat", "primary")]),
        )
    else:
        await update.message.reply_text("⏸ البوت متوقف حالياً، حاول لاحقاً.")


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🆔 رقمك: {update.effective_user.id}")


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    HISTORY.pop(update.effective_chat.id, None)
    await update.message.reply_text("🔄 بدأنا محادثة جديدة.")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("state", None)
    await update.message.reply_text("تم الإلغاء.")


# ============================ الرسائل النصية ============================
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return
    text = update.message.text.strip()
    user = update.effective_user

    in_private = update.effective_chat.type == "private"
    if is_admin(user.id) and in_private and context.user_data.get("state"):
        await handle_admin_input(update, context, text)
        return

    await chat_with_ai(update, text)


async def handle_admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    st = context.user_data["state"]
    step = st["step"]
    reply = update.message.reply_text

    if step == "add_name":
        if len(text) > 40:
            await reply("⚠️ الاسم طويل (الحد 40 حرف). أرسل اسماً أقصر:", reply_markup=CANCEL_KB)
            return
        context.user_data["state"] = {"step": "add_prompt", "name": text}
        await reply(
            f"📝 الآن أرسل البرومبت (التعليمات) الخاص بمود «{text}»:\n"
            "مثال: أنت مساعد مبيعات ودود، تجيب بالعربية باختصار…",
            reply_markup=CANCEL_KB,
        )

    elif step == "add_prompt":
        mid = uuid.uuid4().hex[:8]
        DB["modes"][mid] = {"name": st["name"], "prompt": text}
        save_data()
        context.user_data.pop("state", None)
        await reply(
            f"✅ تم حفظ المود «{st['name']}».",
            reply_markup=kb(
                [b("✅ تفعيله الآن", f"act:{mid}", "success")],
                [b("➕ إضافة آخر", "add", "primary"), b("🏠 الرئيسية", "home")],
            ),
        )

    elif step in ("edit_name", "edit_prompt"):
        mid = st["id"]
        context.user_data.pop("state", None)
        if mid not in DB["modes"]:
            await reply("⚠️ هذا المود لم يعد موجوداً.")
            return
        if step == "edit_name":
            if len(text) > 40:
                await reply("⚠️ الاسم طويل (الحد 40 حرف).")
                return
            DB["modes"][mid]["name"] = text
        else:
            DB["modes"][mid]["prompt"] = text
        save_data()
        view_text, markup = mode_view(mid, "✅ تم التعديل")
        await reply(view_text, reply_markup=markup)


# ============================ الأزرار ============================
async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    data = q.data or ""

    if data == "newchat":
        HISTORY.pop(q.message.chat_id, None)
        await q.answer("🔄 بدأنا محادثة جديدة")
        return

    if not is_admin(q.from_user.id):
        await q.answer("هذه الأزرار للأدمن فقط", show_alert=True)
        return
    await q.answer()

    cmd, _, arg = data.partition(":")

    if cmd in ("home", "cancel"):
        context.user_data.pop("state", None)
        await edit(q, *home_view())

    elif cmd == "add":
        context.user_data["state"] = {"step": "add_name"}
        await edit(q, "✏️ أرسل اسم المود الجديد\nمثال: مساعد مبيعات", CANCEL_KB)

    elif cmd == "list":
        context.user_data.pop("state", None)
        await edit(q, *list_view())

    elif cmd == "off":
        DB["active"] = None
        save_data()
        await edit(q, *home_view())

    elif cmd in ("m", "act", "en", "ep", "show", "del", "delok"):
        if arg not in DB["modes"]:
            await edit(q, "⚠️ هذا المود لم يعد موجوداً.", kb([b("⬅️ رجوع", "list")]))
            return

        if cmd == "m":
            await edit(q, *mode_view(arg))

        elif cmd == "act":
            DB["active"] = arg
            save_data()
            await edit(q, *mode_view(arg, "✅ تم التفعيل — هذا المود هو الذي يرد الآن"))

        elif cmd == "en":
            context.user_data["state"] = {"step": "edit_name", "id": arg}
            await edit(q, "✏️ أرسل الاسم الجديد للمود:", CANCEL_KB)

        elif cmd == "ep":
            context.user_data["state"] = {"step": "edit_prompt", "id": arg}
            await edit(q, "📝 أرسل البرومبت الجديد (سيستبدل القديم):", CANCEL_KB)

        elif cmd == "show":
            for part in chunks(DB["modes"][arg]["prompt"]):
                await q.message.reply_text(part)

        elif cmd == "del":
            name = DB["modes"][arg]["name"]
            await edit(
                q,
                f"⚠️ هل تريد حذف المود «{name}» نهائياً؟",
                kb([b("🗑 نعم، احذف", f"delok:{arg}", "danger"), b("↩️ تراجع", f"m:{arg}", "primary")]),
            )

        elif cmd == "delok":
            DB["modes"].pop(arg, None)
            if DB["active"] == arg:
                DB["active"] = None
            save_data()
            await edit(q, *list_view())


# ============================ تشغيل ============================
async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    if isinstance(context.error, Conflict):
        log.warning("409 Conflict: توجد نسخة أخرى من البوت تعمل بنفس التوكن، أوقفها.")
    else:
        log.error("Unhandled exception", exc_info=context.error)


async def post_init(app: Application):
    await app.bot.set_my_commands(
        [
            BotCommand("start", "بدء"),
            BotCommand("new", "محادثة جديدة"),
            BotCommand("id", "معرفة رقمك"),
        ]
    )


def start_health_server():
    """خادم صغير يفتح المنفذ PORT (مطلوب إذا نشرته على Render كـ Web Service)"""
    port = os.environ.get("PORT")
    if not port:
        return

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"OK")

        do_HEAD = do_GET

        def log_message(self, *args):
            pass

    server = HTTPServer(("0.0.0.0", int(port)), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()


def main():
    if not TELEGRAM_TOKEN or not GEMINI_API_KEY:
        raise SystemExit("❌ ضع TELEGRAM_TOKEN و GEMINI_API_KEY في Environment Variables")

    start_health_server()

    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .concurrent_updates(True)
        .post_init(post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("admin", cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)

    log.info("Bot started. Admins: %s | Model: %s", sorted(ADMIN_IDS), GEMINI_MODEL)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
