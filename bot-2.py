# -*- coding: utf-8 -*-
"""
بوت تيليجرام ذكاء اصطناعي (Gemini) — الإصدار 3

  • مودات برومبت يختارها المستخدم + أزرار ملونة تحت كل رد + نسخة أذكى
  • 📚 المكتبة: يفهرس قنوات/مجموعات تتبع لك، ويبحث فيها بالذكاء الاصطناعي ويرسل
    الصوتيات/الفيديو/الصور/الملفات للمستخدم (اختيار واحد أو إرسال الكل)
  • إن تعذّر دخول البوت للمصدر: ربط حساب حقيقي عبر Session (Telethon)

الملفات: bot.py + library.py + userbot.py (+ make_session.py تُشغَّل على جهازك)

المتغيرات المطلوبة:
  TELEGRAM_TOKEN, GEMINI_API_KEY, ADMIN_IDS
اختيارية:
  GEMINI_MODEL, GEMINI_SMART_MODEL, GEMINI_FALLBACK_MODEL, DATA_FILE, LIBRARY_DB
  TG_API_ID, TG_API_HASH, TG_SESSION      (ربط الحساب — انظر userbot.py)
  LIB_SEND_LIMIT (25)  LIB_SYNC_MINUTES (60، و0 يعطّل المزامنة الدورية)
"""
import asyncio
import json
import logging
import math
import os
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
from telegram import BotCommand
from telegram import InlineKeyboardButton as Btn
from telegram import InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import BadRequest, Conflict, Forbidden, RetryAfter, TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import library
import userbot

# ============================ الإعدادات ============================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
SMART_MODEL = os.environ.get("GEMINI_SMART_MODEL", "gemini-pro-latest")
FALLBACK_MODEL = os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-flash-lite-latest")
ADMIN_IDS = {int(x) for x in re.findall(r"\d+", os.environ.get("ADMIN_IDS", ""))}
DATA_FILE = os.environ.get("DATA_FILE", "data.json")
MAX_HISTORY = 20
LIB_SEND_LIMIT = int(os.environ.get("LIB_SEND_LIMIT", "25"))
LIB_SYNC_MINUTES = int(os.environ.get("LIB_SYNC_MINUTES", "60"))
LIB_PAGE = 8
MAX_RELAY_BYTES = 49 * 1024 * 1024  # حد رفع البوت عبر Bot API

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
# مهم: httpx يطبع رابط الطلب وفيه توكن البوت
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("bot")


# ============================ التخزين ============================
def load_data() -> dict:
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            d = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        d = {}
    d.setdefault("modes", {})
    old_active = d.pop("active", None)
    d.setdefault("default", old_active)
    d.setdefault("enabled", True)
    d.setdefault("smart_enabled", True)
    d.setdefault("users", {})
    for m in d["modes"].values():
        m.setdefault("visible", True)
    # المكتبة: الزر مخفي افتراضياً حتى يظهره الأدمن
    lib = d.setdefault("lib", {})
    lib.setdefault("visible", False)
    lib.setdefault("name", "المكتبة")
    lib.setdefault("sources", {})  # {chat_id: {title, username, bot, session, access_hash, last_sync}}
    return d


def save_data() -> None:
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(DB, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)


DB = load_data()
HISTORY: dict = {}
LOCKS: dict = {}
LIB_STATE: dict = {}  # chat_id -> {"ids","query","summary","page","ts"}
TASKS: set = set()


def spawn(coro):
    t = asyncio.create_task(coro)
    TASKS.add(t)
    t.add_done_callback(TASKS.discard)
    return t


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def prefs(chat_id: int) -> dict:
    key = str(chat_id)
    if key not in DB["users"]:
        DB["users"][key] = {"mode": None, "smart": False, "lib": False}
        save_data()
    return DB["users"][key]


def allowed_modes(admin: bool) -> dict:
    return {mid: m for mid, m in DB["modes"].items() if admin or m.get("visible", True)}


def lib_available(admin: bool) -> bool:
    L = DB["lib"]
    return bool(L["sources"]) and (L["visible"] or admin)


def resolve_mode(chat_id: int, admin: bool):
    allowed = allowed_modes(admin)
    p = prefs(chat_id)
    if p["mode"] in allowed:
        return p["mode"]
    chosen = None
    if DB.get("default") in allowed:
        chosen = DB["default"]
    elif len(allowed) == 1:
        chosen = next(iter(allowed))
    if chosen:
        p["mode"] = chosen
        save_data()
    return chosen


# ============================ الأزرار والواجهات ============================
def b(text: str, data: str, style: str | None = None) -> Btn:
    """style: 'success' أخضر | 'danger' أحمر | 'primary' أزرق"""
    return Btn(text, callback_data=data, style=style)


def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([list(r) for r in rows])


CANCEL_KB = kb([b("❌ إلغاء", "cancel", "danger")])
BACK_TO_CHAT = b("🎭 العودة للمحادثة مع الذكاء", "chg", "primary")


def lib_button_label() -> str:
    L = DB["lib"]
    return ("" if L["visible"] else "🔒 ") + f"📚 {L['name']}"


def reply_kb(chat_id: int) -> InlineKeyboardMarkup:
    """الأزرار الملونة تحت كل رد"""
    admin = is_admin(chat_id)
    p = prefs(chat_id)
    mode = DB["modes"].get(p["mode"])
    name = mode["name"] if mode else "—"
    rows = [[b(f"🎭 تغيير المود · {name}", "chg", "primary")]]
    if lib_available(admin):
        rows.append([b(lib_button_label(), "lib", "success")])
    second = []
    if DB["smart_enabled"]:
        if p["smart"]:
            second.append(b("⚡ نسخة سريعة", "smart", "primary"))
        else:
            second.append(b("🧠 نسخة أذكى", "smart", "success"))
    second.append(b("🔄 محادثة جديدة", "newchat", "danger"))
    rows.append(second)
    return kb(*rows)


def picker_view(chat_id: int, admin: bool, intro: str = "🎭 اختر المود الذي تريد التحدث معه:"):
    allowed = allowed_modes(admin)
    lib_ok = lib_available(admin)
    if not allowed and not lib_ok:
        return "⚠️ لا توجد مودات متاحة حالياً.", None
    p = prefs(chat_id)
    in_lib = bool(p.get("lib")) and lib_ok
    current = None if in_lib else p["mode"]
    rows = []
    if lib_ok:
        rows.append([b(("✅ " if in_lib else "") + lib_button_label(), "libp", "success")])
    for mid, m in allowed.items():
        if mid == current:
            label, style = "✅ " + m["name"], "success"
        elif not m.get("visible", True):
            label, style = "🔒 " + m["name"], None
        else:
            label, style = "🎭 " + m["name"], "primary"
        rows.append([b(label, f"pick:{mid}", style)])
    rows.append([b("❌ إغلاق", "closepick", "danger")])
    return intro, kb(*rows)


def home_view():
    modes = DB["modes"]
    shown = sum(1 for m in modes.values() if m.get("visible", True))
    default = DB["modes"].get(DB.get("default"))
    L = DB["lib"]
    text = (
        "🎛 لوحة تحكم الأدمن\n\n"
        f"الحالة: {'🟢 البوت يعمل' if DB['enabled'] else '🔴 البوت متوقف'}\n"
        f"🎭 المودات: {len(modes)} (ظاهر للمستخدمين: {shown})\n"
        f"⭐ الافتراضي: {default['name'] if default else 'لا يوجد'}\n"
        f"🧠 النسخة الأذكى: {'مفعّلة' if DB['smart_enabled'] else 'معطّلة'}\n"
        f"📚 المكتبة «{L['name']}»: {len(L['sources'])} مصدر · {library.count()} عنصر · "
        f"{'ظاهرة' if L['visible'] else 'مخفية'}\n"
        f"👥 المستخدمون: {len(DB['users'])}\n\n"
        "💡 للتجربة كمستخدم: /mode"
    )
    markup = kb(
        [b("➕ إضافة مود جديد", "add", "success")],
        [b("📋 إدارة المودات", "list", "primary")],
        [b("📚 إدارة المكتبة", "la", "primary")],
        [
            b("⏸ إيقاف البوت", "togglebot", "danger")
            if DB["enabled"]
            else b("▶️ تشغيل البوت", "togglebot", "success")
        ],
        [
            b("🧠 تعطيل النسخة الأذكى", "togglesmart", "danger")
            if DB["smart_enabled"]
            else b("🧠 تفعيل النسخة الأذكى", "togglesmart", "success")
        ],
    )
    return text, markup


def list_view():
    if not DB["modes"]:
        return "📭 لا توجد مودات بعد.", kb(
            [b("➕ إضافة مود", "add", "success")], [b("🏠 الرئيسية", "home")]
        )
    rows = []
    for mid, m in DB["modes"].items():
        visible = m.get("visible", True)
        star = "⭐ " if DB.get("default") == mid else ""
        rows.append(
            [b(("👁 " if visible else "🚫 ") + star + m["name"], f"m:{mid}", "success" if visible else None)]
        )
    rows.append([b("➕ إضافة مود", "add", "success"), b("🏠 الرئيسية", "home")])
    return "📋 المودات (👁 ظاهر · 🚫 مخفي · ⭐ افتراضي):", kb(*rows)


def mode_view(mid: str, notice: str = ""):
    m = DB["modes"][mid]
    visible = m.get("visible", True)
    is_default = DB.get("default") == mid
    preview = m["prompt"] if len(m["prompt"]) <= 500 else m["prompt"][:500] + "…"
    text = (f"{notice}\n\n" if notice else "") + (
        f"🎭 المود: {m['name']}\n"
        f"الظهور: {'👁 ظاهر للمستخدمين' if visible else '🚫 مخفي (للأدمن فقط)'}\n"
        f"الافتراضي: {'⭐ نعم' if is_default else 'لا'}\n\n"
        f"📝 البرومبت:\n{preview}"
    )
    markup = kb(
        [
            b("🚫 إخفاء عن المستخدمين", f"vis:{mid}", "danger")
            if visible
            else b("👁 إظهار للمستخدمين", f"vis:{mid}", "success")
        ],
        [
            b("☆ إلغاء الافتراضي", f"def:{mid}", "primary")
            if is_default
            else b("⭐ جعله الافتراضي", f"def:{mid}", "success")
        ],
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
    def __init__(self, msg: str, fatal: bool = False):
        super().__init__(msg)
        self.fatal = fatal


async def call_gemini(model: str, system_prompt: str, contents: list,
                      timeout: float = 30.0, json_mode: bool = False) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    gen = {"temperature": 0.8, "maxOutputTokens": 4096}
    if json_mode:
        gen = {"temperature": 0.1, "maxOutputTokens": 2048, "responseMimeType": "application/json"}
    payload = {"contents": contents, "generationConfig": gen}
    if system_prompt:
        payload["system_instruction"] = {"parts": [{"text": system_prompt}]}
    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as e:
        raise GeminiError(f"تعذر الاتصال بـ Gemini ({type(e).__name__}) [{model}]")

    if r.status_code != 200:
        try:
            msg = r.json()["error"]["message"]
        except Exception:
            msg = r.text[:200]
        hints = {
            400: "الطلب غير صحيح",
            401: "المفتاح غير صالح، تأكد من GEMINI_API_KEY",
            403: "المفتاح لا يملك صلاحية لهذا الموديل",
            404: "اسم الموديل غير موجود",
            429: "تجاوزت الحد المسموح (Quota) أو الموديل يحتاج خطة مدفوعة",
            503: "الموديل عليه ضغط عالٍ",
        }
        raise GeminiError(
            f"خطأ Gemini ({r.status_code}) [{model}]: {hints.get(r.status_code, '')}\n{msg[:300]}",
            fatal=r.status_code in (400, 401, 403),
        )

    data = r.json()
    cands = data.get("candidates") or []
    if not cands:
        block = data.get("promptFeedback", {}).get("blockReason", "غير معروف")
        raise GeminiError(f"لم يصل رد من Gemini (سبب الحظر: {block})", fatal=True)
    parts = cands[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
    if not text:
        raise GeminiError(f"وصل رد فارغ [{model}]")
    return text


async def ask_gemini(system_prompt: str, contents: list, smart: bool = False, json_mode: bool = False):
    """يرجع (النص، الموديل الذي ردّ فعلاً)"""
    if smart:
        plan = [(SMART_MODEL, 45.0), (GEMINI_MODEL, 30.0)]
    else:
        plan = [(GEMINI_MODEL, 30.0), (GEMINI_MODEL, 30.0)]
    if FALLBACK_MODEL and FALLBACK_MODEL not in [m for m, _ in plan]:
        plan.append((FALLBACK_MODEL, 30.0))

    last: GeminiError | None = None
    for i, (model, timeout) in enumerate(plan):
        try:
            return await call_gemini(model, system_prompt, contents, timeout, json_mode), model
        except GeminiError as e:
            if e.fatal and model != SMART_MODEL:
                raise
            last = e
            log.warning("المحاولة %d فشلت: %s", i + 1, e)
            if i < len(plan) - 1:
                await asyncio.sleep(1.5)
    raise last


def clean(t: str) -> str:
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t, flags=re.S)
    t = re.sub(r"^\s*[\*\-]\s+", "• ", t, flags=re.M)
    t = re.sub(r"^#{1,6}\s*", "", t, flags=re.M)
    return t


def chunks(t: str, n: int = 3900):
    while len(t) > n:
        cut = t.rfind("\n", 0, n)
        if cut < n // 2:
            cut = n
        yield t[:cut]
        t = t[cut:].lstrip("\n")
    if t.strip():
        yield t


# ============================ الرد على المستخدم ============================
async def respond(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, text: str) -> None:
    bot = context.bot
    admin = is_admin(user_id)

    if not DB["enabled"] and not admin:
        await bot.send_message(chat_id, "⏸ البوت متوقف حالياً، حاول لاحقاً.")
        return

    # وضع المكتبة
    p = prefs(chat_id)
    if p.get("lib"):
        if lib_available(admin):
            await lib_handle(context, chat_id, admin, text)
            return
        p["lib"] = False
        save_data()

    mid = resolve_mode(chat_id, admin)
    if not mid:
        if not allowed_modes(admin) and not lib_available(admin):
            await bot.send_message(
                chat_id,
                "⚠️ لا توجد مودات متاحة حالياً." + (" أضف مود من /start." if admin else ""),
            )
            return
        context.user_data["pending"] = text
        t, m = picker_view(
            chat_id, admin, "🎭 اختر المود الذي تريد التحدث معه أولاً\n(سأرد على رسالتك فور اختيارك):"
        )
        await bot.send_message(chat_id, t, reply_markup=m)
        return

    mode = DB["modes"][mid]
    smart = DB["smart_enabled"] and p["smart"]

    lock = LOCKS.setdefault(chat_id, asyncio.Lock())
    async with lock:
        h = HISTORY.get(chat_id)
        if not h or h["mode"] != mid or h["prompt"] != mode["prompt"]:
            h = HISTORY[chat_id] = {"mode": mid, "prompt": mode["prompt"], "msgs": []}

        contents = h["msgs"] + [{"role": "user", "parts": [{"text": text}]}]
        await bot.send_chat_action(chat_id, ChatAction.TYPING)
        try:
            reply, used = await ask_gemini(mode["prompt"], contents, smart)
        except GeminiError as e:
            log.error("Gemini error: %s", e)
            await bot.send_message(
                chat_id,
                f"❌ {e}" if admin else "⚠️ حدث خطأ مؤقت، حاول مرة أخرى بعد قليل.",
                reply_markup=reply_kb(chat_id),
            )
            return

        msgs = (contents + [{"role": "model", "parts": [{"text": reply}]}])[-MAX_HISTORY:]
        while msgs and msgs[0]["role"] != "user":
            msgs.pop(0)
        h["msgs"] = msgs

    note = ""
    if smart and used != SMART_MODEL:
        note = "\n\nℹ️ النسخة الأذكى غير متاحة الآن، تم الرد بالنسخة السريعة."

    parts = list(chunks(clean(reply)))
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        await bot.send_message(
            chat_id, part + (note if last else ""), reply_markup=reply_kb(chat_id) if last else None
        )


# ============================ 📚 المكتبة: فهم الطلب بالذكاء الاصطناعي ============================
LIB_PARSE_PROMPT = """أنت محلل طلبات لمكتبة وسائط (صوتيات، فيديو، صور، ملفات، نصوص).
حلل رسالة المستخدم وأعد JSON فقط، بدون أي شرح، بهذا الشكل:
{"intent":"search","query":"عبارة قصيرة تصف الطلب","terms":[["كلمة","تهجئة بديلة"],["مفهوم آخر"]],"media":"any","numbers":[],"want_all":false}

القواعد:
- intent واحد من: search (يطلب محتوى)، select (يختار رقماً أو أرقاماً من القائمة السابقة)، send_all (يطلب إرسال كل نتائج القائمة السابقة)، more (يطلب المزيد/الصفحة التالية)، other (تحية أو كلام لا علاقة له بالبحث).
- terms: مفاهيم البحث الأساسية (حتى 4). كل مفهوم قائمة فيها الكلمة وتهجئاتها/مرادفاتها الشائعة (عربي ولاتيني). لا تضع كلمات عامة مثل: أرسل، أريد، ممكن، لو سمحت، ملف.
  مثال: «سورة البقرة» => [["البقرة","baqarah","baqara"],["سورة","surah"]]
- media: audio (صوتية/تلاوة/مقطع صوتي/تسجيل)، video (فيديو/مقطع مرئي)، photo (صورة)، document (كتاب/ملف/pdf)، text (نص)، وإلا any.
- want_all=true فقط إذا طلب صراحة إرسال كل النتائج (مثل: أرسل كل ... / كلها).
- numbers: أرقام القائمة التي اختارها المستخدم مثل «الثالث» => [3] أو «1 و 4» => [1,4]."""

_STOP = {"ارسل", "ارسلي", "اعطني", "عطني", "ابغي", "ابي", "اريد", "هات", "ممكن", "لو", "سمحت",
         "من", "في", "عن", "علي", "الي", "هل", "يوجد", "عندك", "عندكم", "لي", "لنا"}


def fallback_terms(text: str) -> list:
    return [[w] for w in library.normalize(text).split() if w not in _STOP and len(w) >= 2][:5]


async def lib_understand(text: str, st) -> dict:
    plan = {"intent": "search", "query": text[:40], "terms": [], "media": "any", "numbers": [], "want_all": False}

    # اختصار: رقم فقط والقائمة موجودة
    if st and re.fullmatch(r"\d{1,3}", library.normalize(text)):
        plan.update(intent="select", numbers=[int(library.normalize(text))])
        return plan

    ctx = f"قائمة نتائج سابقة: {'نعم، عددها ' + str(len(st['ids'])) if st else 'لا'}\nرسالة المستخدم: {text}"
    try:
        raw, _ = await ask_gemini(
            LIB_PARSE_PROMPT, [{"role": "user", "parts": [{"text": ctx}]}], json_mode=True
        )
        raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
        data = json.loads(raw)
        if isinstance(data, list) and data:
            data = data[0]
        if not isinstance(data, dict):
            raise ValueError("not a dict")
    except Exception as e:
        log.warning("lib_understand fallback: %s", e)
        plan["terms"] = fallback_terms(text)
        return plan

    if data.get("intent") in ("search", "select", "send_all", "more", "other"):
        plan["intent"] = data["intent"]
    terms = []
    for g in (data.get("terms") or [])[:5]:
        g = g if isinstance(g, list) else [g]
        g = [str(v) for v in g if str(v).strip()]
        if g:
            terms.append(g)
    plan["terms"] = terms or fallback_terms(text)
    if data.get("media") in library.KIND_GROUPS:
        plan["media"] = data["media"]
    plan["numbers"] = [int(n) for n in (data.get("numbers") or []) if str(n).lstrip("-").isdigit()]
    plan["want_all"] = bool(data.get("want_all"))
    plan["query"] = str(data.get("query") or text)[:40]
    return plan


# ============================ 📚 المكتبة: العرض والإرسال ============================
def lib_intro_text() -> str:
    return (
        f"📚 {DB['lib']['name']}\n\n"
        "اكتب ما تبحث عنه وسأجده لك (صوتيات، فيديو، صور، ملفات…)\n"
        "مثال: «سورة البقرة» أو «فيديو عن …» أو «أرسل كل …»"
    )


def lib_list_view(chat_id: int, page: int):
    st = LIB_STATE[chat_id]
    ids = st["ids"]
    n = len(ids)
    pages = max(1, math.ceil(n / LIB_PAGE))
    page = max(0, min(page, pages - 1))
    st["page"] = page
    chunk_ids = ids[page * LIB_PAGE:(page + 1) * LIB_PAGE]
    by_id = {r["id"]: r for r in library.get_many(chunk_ids)}

    lines, num_btns = [], []
    for off, rid in enumerate(chunk_ids):
        r = by_id.get(rid)
        if not r:
            continue
        num = page * LIB_PAGE + off + 1
        lines.append(f"{num}. {library.label(r)}")
        num_btns.append(b(str(num), f"lp:{rid}", "primary"))

    text = (
        f"🔎 وجدت {n} نتيجة عن «{st['query']}»\n{st['summary']}\n\n"
        + "\n".join(lines)
        + f"\n\n📄 صفحة {page + 1}/{pages}\n"
        "اختر رقماً من الأزرار، أو اكتب مثل: «أرسل الثالث» / «أرسل الكل»"
    )
    rows = [num_btns[i:i + 4] for i in range(0, len(num_btns), 4)]
    nav = []
    if page > 0:
        nav.append(b("◀️ السابق", f"lpg:{page - 1}"))
    if page < pages - 1:
        nav.append(b("التالي ▶️", f"lpg:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([
        b(f"📤 إرسال الكل ({min(n, LIB_SEND_LIMIT)})", "lall", "success"),
        b("❌ إغلاق", "closepick", "danger"),
    ])
    return text, kb(*rows)


def lib_after_kb(chat_id: int):
    rows = []
    st = LIB_STATE.get(chat_id)
    if st and len(st["ids"]) > 1:
        rows.append([b("🔎 الرجوع للنتائج", f"lpg:{st['page']}", "primary")])
    rows.append([BACK_TO_CHAT])
    return kb(*rows)


async def relay_item(bot, chat_id: int, it: dict, src: dict) -> None:
    """يحمّل الملف عبر الحساب ثم يرسله البوت (عندما لا يستطيع البوت نسخ الرسالة)"""
    if it.get("size") and it["size"] > MAX_RELAY_BYTES:
        raise ValueError("big")
    k = it["kind"]
    if k == "text":
        await bot.send_message(chat_id, (it.get("text") or it.get("name") or "")[:4096])
        return
    data = await userbot.download(int(it["chat_id"]), src.get("access_hash"), it["msg_id"])
    if len(data) > MAX_RELAY_BYTES:
        raise ValueError("big")
    cap = (it.get("caption") or "")[:1000] or None
    fname = it.get("filename") or f"{k}_{it['msg_id']}{it.get('ext') or ''}"
    if k == "audio":
        await bot.send_audio(chat_id, data, caption=cap, title=it.get("title"),
                             performer=it.get("performer"), duration=it.get("duration"), filename=fname)
    elif k == "voice":
        await bot.send_voice(chat_id, data, caption=cap, duration=it.get("duration"))
    elif k == "video":
        await bot.send_video(chat_id, data, caption=cap, duration=it.get("duration"), filename=fname)
    elif k == "animation":
        await bot.send_animation(chat_id, data, caption=cap, filename=fname)
    elif k == "photo":
        await bot.send_photo(chat_id, data, caption=cap)
    else:
        await bot.send_document(chat_id, data, caption=cap, filename=fname)


async def deliver(context, chat_id: int, rid: int) -> bool:
    """يرسل عنصراً من المكتبة للمستخدم: نسخ مباشر ← عبر الحساب ← رابط"""
    bot = context.bot
    it = library.get(rid)
    if not it:
        return False
    src = DB["lib"]["sources"].get(str(it["chat_id"]))
    if not src:
        return False

    if src.get("bot"):
        for _ in range(2):
            try:
                await bot.copy_message(chat_id, from_chat_id=it["chat_id"], message_id=it["msg_id"])
                return True
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except (BadRequest, Forbidden) as e:
                s = str(e).lower()
                log.warning("copy_message failed: %s", e)
                if "message to copy not found" in s or "message_id_invalid" in s:
                    library.delete_item(rid)  # الرسالة حُذفت من المصدر
                    return False
                break

    if src.get("session") and userbot.configured():
        try:
            await relay_item(bot, chat_id, it, src)
            return True
        except ValueError:
            pass  # كبير الحجم → الرابط
        except Exception as e:
            log.warning("relay failed: %s: %s", type(e).__name__, e)

    link = library.link_for(src, it)
    if link:
        await bot.send_message(chat_id, f"🔗 {library.label(it)}\n{link}")
        return True
    return False


async def lib_send_many(context, chat_id: int, ids: list, total: int | None = None) -> None:
    bot = context.bot
    note = f"📤 جاري إرسال {len(ids)} ملف…"
    if total and total > len(ids):
        note += f"\n(الحد الأقصى {LIB_SEND_LIMIT} في المرة، من أصل {total})"
    await bot.send_message(chat_id, note)
    ok = 0
    for rid in ids:
        if await deliver(context, chat_id, rid):
            ok += 1
        await asyncio.sleep(0.4)
    await bot.send_message(chat_id, f"✅ تم إرسال {ok} من {len(ids)}.", reply_markup=lib_after_kb(chat_id))


async def lib_handle(context, chat_id: int, admin: bool, text: str) -> None:
    bot = context.bot
    st = LIB_STATE.get(chat_id)
    if st and time.time() - st["ts"] > 3600:
        LIB_STATE.pop(chat_id, None)
        st = None

    await bot.send_chat_action(chat_id, ChatAction.TYPING)
    plan = await lib_understand(text, st)
    intent = plan["intent"]
    back = kb([BACK_TO_CHAT])

    if intent == "other":
        await bot.send_message(chat_id, lib_intro_text(), reply_markup=back)
        return

    if st and intent == "select":
        nums = [n for n in plan["numbers"] if 1 <= n <= len(st["ids"])]
        if not nums:
            await bot.send_message(chat_id, f"⚠️ اختر رقماً من 1 إلى {len(st['ids'])}.")
            return
        await lib_send_many(context, chat_id, [st["ids"][n - 1] for n in nums])
        return
    if st and intent == "send_all":
        await lib_send_many(context, chat_id, st["ids"][:LIB_SEND_LIMIT], total=len(st["ids"]))
        return
    if st and intent == "more":
        t, m = lib_list_view(chat_id, st["page"] + 1)
        await bot.send_message(chat_id, t, reply_markup=m)
        return

    # ---------- بحث جديد ----------
    rows = library.search(plan["terms"], plan["media"])
    query = plan["query"]
    if not rows:
        await bot.send_message(
            chat_id, f"😕 لم أجد شيئاً عن «{query}» في {DB['lib']['name']}.\nجرّب كلمات أخرى.", reply_markup=back
        )
        return

    n = len(rows)
    if n == 1:
        await bot.send_message(chat_id, f"✅ وجدت ملفاً واحداً:\n{library.label(rows[0])}\n⏳ جاري الإرسال…")
        ok = await deliver(context, chat_id, rows[0]["id"])
        await bot.send_message(
            chat_id, "✅ تم الإرسال." if ok else "⚠️ تعذر إرسال الملف.", reply_markup=back
        )
        return

    LIB_STATE[chat_id] = {
        "ids": [r["id"] for r in rows], "query": query, "summary": library.summarize(rows),
        "page": 0, "ts": time.time(),
    }
    if plan["want_all"] and n <= LIB_SEND_LIMIT:
        await bot.send_message(chat_id, f"🔎 وجدت {n} نتيجة عن «{query}» ({LIB_STATE[chat_id]['summary']}).")
        await lib_send_many(context, chat_id, LIB_STATE[chat_id]["ids"])
        return

    t, m = lib_list_view(chat_id, 0)
    await bot.send_message(chat_id, t, reply_markup=m)


# ============================ 📚 المكتبة: الفهرسة وإدارة المصادر ============================
async def on_any_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """يفهرس كل ما يصل من القنوات/المجموعات المعتمدة (للمنشورات الجديدة فقط)"""
    msg = update.effective_message
    if not msg:
        return
    if str(msg.chat_id) not in DB["lib"]["sources"]:
        return
    try:
        it = library.item_from_ptb(msg, include_text=(msg.chat.type == "channel"))
        if it:
            library.upsert([it])
    except Exception:
        log.exception("index failed")


async def sync_source(bot, cid: str, notify_id: int | None = None) -> int:
    src = DB["lib"]["sources"].get(cid)
    if not src or not src.get("session") or not userbot.configured():
        return 0
    total = 0

    def on_batch(batch):
        nonlocal total
        total += library.upsert(batch)

    try:
        await userbot.fetch(int(cid), src.get("access_hash"), library.max_msg_id(int(cid)), on_batch)
    except Exception as e:
        log.error("sync failed for %s: %s: %s", cid, type(e).__name__, e)
        if notify_id:
            await bot.send_message(notify_id, f"❌ فشلت مزامنة «{src['title']}»:\n{type(e).__name__}: {e}")
        return total
    src["last_sync"] = int(time.time())
    save_data()
    if notify_id:
        await bot.send_message(notify_id, f"✅ انتهت مزامنة «{src['title']}»: {total} عنصر جديد.")
    return total


async def sync_loop(app: Application):
    await asyncio.sleep(30)
    while True:
        if userbot.configured():
            for cid, src in list(DB["lib"]["sources"].items()):
                if src.get("session"):
                    try:
                        await sync_source(app.bot, cid)
                    except Exception:
                        log.exception("sync_loop error")
        await asyncio.sleep(max(5, LIB_SYNC_MINUTES) * 60)


def parse_bot_target(ref: str):
    ref = (ref or "").strip()
    if not ref:
        return None
    if re.fullmatch(r"-?\d{6,}", ref):
        return int(ref)
    m = re.search(r"t\.me/c/(\d+)", ref)
    if m:
        return int("-100" + m.group(1))
    kind, val = userbot.parse_ref(ref)
    return "@" + val if kind == "user" else None


def forward_chat_id(msg):
    fo = getattr(msg, "forward_origin", None)
    if fo is None:
        return None
    ch = getattr(fo, "chat", None) or getattr(fo, "sender_chat", None)
    return ch.id if ch else None


async def try_add_source(bot, ref: str, fwd, expect_cid=None, notify_id=None):
    """يربط مصدراً: البوت أولاً، ثم الحساب (Session). يرجع (نجح؟، رسالة)"""
    cid = title = username = ahash = None
    bot_ok = sess_ok = False
    errs = []

    target = fwd if fwd else parse_bot_target(ref)
    if target is not None and not expect_cid:
        try:
            chat = await bot.get_chat(target)
            me = await bot.get_chat_member(chat.id, bot.id)
            if me.status in ("administrator", "creator", "member"):
                cid, title, username, bot_ok = chat.id, chat.title, chat.username, True
            else:
                errs.append("البوت ليس عضواً/مشرفاً في هذا المصدر")
        except TelegramError as e:
            errs.append(f"البوت: {e}")

    if ref and userbot.configured():
        try:
            pid, ttl, uname, ahash = await userbot.resolve_and_join(ref)
            if cid is None or cid == pid:
                cid, title, username, sess_ok = pid, title or ttl, username or uname, True
            else:
                errs.append("معرّف المصدر من الحساب لا يطابق معرّفه من البوت")
        except Exception as e:
            errs.append(f"الحساب: {type(e).__name__}: {e}")

    if cid is None or (expect_cid and str(cid) != str(expect_cid)):
        hint = (
            "\n\nالحلول:\n• أضف البوت كمشرف في القناة/المجموعة ثم أعد المحاولة.\n"
            + ("• أو أرسل رابط دعوة/@username ليدخل الحساب (Session)."
               if userbot.configured() else
               f"• أو فعّل ربط الحساب ({userbot.why_not()}).")
        )
        return False, "❌ تعذر ربط المصدر.\n" + "\n".join(errs) + hint

    key = str(cid)
    src = DB["lib"]["sources"].setdefault(
        key, {"title": title, "username": username, "bot": False, "session": False,
              "access_hash": None, "last_sync": 0}
    )
    src["title"] = title or src["title"]
    if username:
        src["username"] = username
    if bot_ok:
        src["bot"] = True
    if sess_ok:
        src["session"] = True
        src["access_hash"] = ahash
    save_data()

    msg = [f"✅ تم ربط «{src['title']}»", f"{'🤖 بوت ' if src['bot'] else ''}{'👤 حساب' if src['session'] else ''}"]
    if sess_ok:
        spawn(sync_source(bot, key, notify_id))
        msg.append("⏳ بدأت فهرسة المحتوى القديم في الخلفية وسأخبرك عند الانتهاء.")
    elif bot_ok:
        msg.append("ℹ️ سيفهرس البوت المنشورات الجديدة منذ الآن فقط. للمحتوى القديم اربط حساب Session بهذا المصدر.")
    if errs:
        msg.append("⚠️ " + " | ".join(errs))
    return True, "\n".join(msg)


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cm = update.my_chat_member
    chat = cm.chat
    if chat.type == "private":
        return
    cid = str(chat.id)
    new = cm.new_chat_member.status
    src = DB["lib"]["sources"].get(cid)

    if new in ("administrator", "member"):
        if src:
            src["bot"] = True
            save_data()
            return
        for aid in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    aid,
                    f"🤖 تمت إضافتي إلى «{chat.title}».\nهل تريد اعتماده كمصدر للمكتبة؟",
                    reply_markup=kb([b("✅ اعتماده", f"lsrc:{cid}", "success"), b("❌ تجاهل", "lskip", "danger")]),
                )
            except TelegramError:
                pass
    elif new in ("left", "kicked") and src:
        src["bot"] = False
        save_data()


# ---------- واجهات إدارة المكتبة ----------
def lib_admin_view():
    L = DB["lib"]
    sess = "✅ مُهيّأ" if userbot.configured() else f"⚪ غير مُهيّأ — {userbot.why_not()}"
    text = (
        "📚 إدارة المكتبة\n\n"
        f"الزر للمستخدمين: «{L['name']}» — {'👁 ظاهر' if L['visible'] else '🚫 مخفي'}\n"
        f"📂 المصادر: {len(L['sources'])}\n"
        f"🗂 العناصر المفهرسة: {library.count()}\n"
        f"👤 حساب Session: {sess}\n\n"
        "💡 لكي يرى البوت القناة أضفه إليها كمشرف. للمحتوى القديم أو للمصادر التي لا يدخلها البوت اربط حساب Session."
    )
    rows = [
        [b("🚫 إخفاء الزر", "lavis", "danger") if L["visible"] else b("👁 إظهار الزر", "lavis", "success")],
        [b("✏️ تسمية الزر", "laname", "primary")],
        [b("➕ إضافة مصدر", "laadd", "success"), b("📂 المصادر", "lasrc", "primary")],
    ]
    if userbot.configured():
        rows.append([b("🔄 مزامنة الكل", "lasync:all", "primary"), b("🧪 فحص الحساب", "lacheck")])
    rows.append([b("🏠 الرئيسية", "home")])
    return text, kb(*rows)


def lib_sources_view():
    srcs = DB["lib"]["sources"]
    if not srcs:
        return "📭 لا توجد مصادر بعد.", kb([b("➕ إضافة مصدر", "laadd", "success")], [b("⬅️ رجوع", "la")])
    rows = []
    for cid, s in srcs.items():
        flags = ("🤖" if s.get("bot") else "") + ("👤" if s.get("session") else "")
        rows.append([b(f"{flags} {s['title']} ({library.count(int(cid))})", f"las:{cid}", "primary")])
    rows.append([b("➕ إضافة مصدر", "laadd", "success"), b("⬅️ رجوع", "la")])
    return "📂 المصادر (🤖 البوت داخلها · 👤 الحساب داخلها):", kb(*rows)


def lib_source_view(cid: str, notice: str = ""):
    s = DB["lib"]["sources"][cid]
    last = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["last_sync"])) if s.get("last_sync") else "—"
    text = (notice + "\n\n" if notice else "") + (
        f"📂 {s['title']}\n"
        f"🆔 {cid}\n"
        f"{'🔗 @' + s['username'] if s.get('username') else '🔒 خاص'}\n"
        f"🤖 البوت: {'✅ داخله' if s.get('bot') else '❌'}\n"
        f"👤 الحساب: {'✅ داخله' if s.get('session') else '❌'}\n"
        f"🗂 العناصر: {library.count(int(cid))}\n"
        f"آخر مزامنة: {last}"
    )
    rows = []
    if s.get("session") and userbot.configured():
        rows.append([b("🔄 مزامنة الآن", f"lasync:{cid}", "success")])
    rows.append([b("👤 ربط/تحديث الحساب بهذا المصدر", f"lalink:{cid}", "primary")])
    rows.append([b("🗑 إزالة المصدر", f"ladel:{cid}", "danger")])
    rows.append([b("⬅️ رجوع", "lasrc")])
    return text, kb(*rows)


# ============================ الأوامر ============================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    context.user_data.pop("state", None)

    if not ADMIN_IDS:
        await update.message.reply_text(
            f"⚠️ لم يتم تعيين أدمن بعد.\nرقمك: {uid}\nضعه في متغير ADMIN_IDS ثم أعد تشغيل البوت."
        )
        return

    if is_admin(uid):
        text, markup = home_view()
        await update.message.reply_text(text, reply_markup=markup)
        return

    if not DB["enabled"]:
        await update.message.reply_text("⏸ البوت متوقف حالياً، حاول لاحقاً.")
        return

    allowed = allowed_modes(False)
    lib_ok = lib_available(False)
    if not allowed and not lib_ok:
        await update.message.reply_text("⚠️ لا توجد مودات متاحة حالياً.")
    elif len(allowed) + (1 if lib_ok else 0) == 1 and allowed:
        resolve_mode(chat_id, False)
        await update.message.reply_text("👋 أهلاً بك! أنا مساعدك الذكي.\nاكتب رسالتك وسأرد عليك مباشرة.")
    else:
        t, m = picker_view(chat_id, False, "👋 أهلاً بك! أنا مساعدك الذكي.\n🎭 اختر ما تريد التحدث معه:")
        await update.message.reply_text(t, reply_markup=m)


async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = is_admin(update.effective_user.id)
    if not DB["enabled"] and not admin:
        await update.message.reply_text("⏸ البوت متوقف حالياً، حاول لاحقاً.")
        return
    t, m = picker_view(update.effective_chat.id, admin)
    await update.message.reply_text(t, reply_markup=m)


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🆔 رقمك: {update.effective_user.id}")


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    HISTORY.pop(update.effective_chat.id, None)
    LIB_STATE.pop(update.effective_chat.id, None)
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
    chat = update.effective_chat

    if str(chat.id) in DB["lib"]["sources"]:
        return  # مصدر مكتبة: يُفهرس فقط ولا يُرد فيه

    if is_admin(user.id) and chat.type == "private" and context.user_data.get("state"):
        await handle_admin_input(update, context, text)
        return

    await respond(context, chat.id, user.id, text)


async def on_nontext(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """رسالة محوّلة (وسائط) من الأدمن أثناء إضافة مصدر"""
    if not update.message or not is_admin(update.effective_user.id):
        return
    st = context.user_data.get("state")
    if st and st.get("step") in ("lib_add", "lib_link"):
        await handle_admin_input(update, context, "")


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
        DB["modes"][mid] = {"name": st["name"], "prompt": text, "visible": True}
        save_data()
        context.user_data.pop("state", None)
        await reply(
            f"✅ تم حفظ المود «{st['name']}» وهو ظاهر للمستخدمين الآن.",
            reply_markup=kb(
                [b("⭐ جعله الافتراضي", f"def:{mid}", "success")],
                [b("➕ إضافة آخر", "add", "primary"), b("📋 المودات", "list", "primary")],
                [b("🏠 الرئيسية", "home")],
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

    elif step == "lib_name":
        if not text or len(text) > 30:
            await reply("⚠️ أرسل اسماً من 1 إلى 30 حرفاً:", reply_markup=CANCEL_KB)
            return
        DB["lib"]["name"] = text
        save_data()
        context.user_data.pop("state", None)
        t, m = lib_admin_view()
        await reply("✅ تم تغيير اسم الزر.\n\n" + t, reply_markup=m)

    elif step in ("lib_add", "lib_link"):
        fwd = forward_chat_id(update.message)
        ref = "" if fwd else text
        await reply("⏳ جاري الفحص…")
        ok, report = await try_add_source(
            context.bot, ref, fwd, expect_cid=st.get("cid"), notify_id=update.effective_user.id
        )
        if ok:
            context.user_data.pop("state", None)
            await reply(report, reply_markup=kb([b("📂 المصادر", "lasrc", "primary")], [b("📚 المكتبة", "la")]))
        else:
            await reply(report, reply_markup=CANCEL_KB)


# ============================ الأزرار ============================
USER_CMDS = {"chg", "pick", "smart", "newchat", "closepick", "lib", "libp", "lp", "lpg", "lall"}


async def on_user_button(update: Update, context: ContextTypes.DEFAULT_TYPE, cmd: str, arg: str):
    q = update.callback_query
    chat_id = q.message.chat_id
    uid = q.from_user.id
    admin = is_admin(uid)

    if cmd == "newchat":
        HISTORY.pop(chat_id, None)
        LIB_STATE.pop(chat_id, None)
        await q.answer("🔄 بدأنا محادثة جديدة")

    elif cmd == "closepick":
        await q.answer()
        try:
            await q.message.delete()
        except BadRequest:
            pass

    elif cmd == "chg":
        if not DB["enabled"] and not admin:
            await q.answer("⏸ البوت متوقف حالياً", show_alert=True)
            return
        t, m = picker_view(chat_id, admin)
        if not m:
            await q.answer("لا توجد مودات متاحة", show_alert=True)
            return
        await q.answer()
        await context.bot.send_message(chat_id, t, reply_markup=m)

    elif cmd == "pick":
        mode = allowed_modes(admin).get(arg)
        if not mode:
            await q.answer("هذا المود غير متاح الآن", show_alert=True)
            return
        p = prefs(chat_id)
        p["mode"] = arg
        p["lib"] = False
        save_data()
        await q.answer(f"✅ {mode['name']}")
        pending = context.user_data.pop("pending", None)
        if pending:
            await edit(q, f"✅ تم اختيار «{mode['name']}» — جاري الرد على رسالتك…")
            await respond(context, chat_id, uid, pending)
        else:
            await edit(q, f"✅ تم اختيار المود: «{mode['name']}»\n\nأرسل رسالتك الآن 👇")

    elif cmd == "smart":
        if not DB["smart_enabled"]:
            await q.answer("النسخة الأذكى غير متاحة حالياً", show_alert=True)
            return
        p = prefs(chat_id)
        p["smart"] = not p["smart"]
        save_data()
        await q.answer("🧠 النسخة الأذكى مفعّلة" if p["smart"] else "⚡ النسخة السريعة مفعّلة")
        try:
            await q.edit_message_reply_markup(reply_markup=reply_kb(chat_id))
        except BadRequest:
            pass

    # ---------- المكتبة ----------
    elif cmd in ("lib", "libp"):
        if not DB["enabled"] and not admin:
            await q.answer("⏸ البوت متوقف حالياً", show_alert=True)
            return
        if not lib_available(admin):
            await q.answer("المكتبة غير متاحة حالياً", show_alert=True)
            return
        p = prefs(chat_id)
        p["lib"] = True
        save_data()
        LIB_STATE.pop(chat_id, None)
        await q.answer()
        markup = kb([BACK_TO_CHAT])
        if cmd == "libp":
            await edit(q, lib_intro_text(), markup)
        else:
            await context.bot.send_message(chat_id, lib_intro_text(), reply_markup=markup)

    elif cmd in ("lp", "lpg", "lall"):
        if not lib_available(admin):
            await q.answer("المكتبة غير متاحة حالياً", show_alert=True)
            return
        if cmd == "lp":
            if not arg.isdigit():
                await q.answer()
                return
            await q.answer("⏳ جاري الإرسال…")
            if not await deliver(context, chat_id, int(arg)):
                await context.bot.send_message(chat_id, "⚠️ تعذر إرسال هذا الملف (قد يكون حُذف).")
            return
        st = LIB_STATE.get(chat_id)
        if not st:
            await q.answer("انتهت صلاحية القائمة، ابحث من جديد", show_alert=True)
            return
        await q.answer()
        if cmd == "lpg":
            await edit(q, *lib_list_view(chat_id, int(arg) if arg.lstrip("-").isdigit() else 0))
        else:
            await lib_send_many(context, chat_id, st["ids"][:LIB_SEND_LIMIT], total=len(st["ids"]))


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.message is None:
        await q.answer()
        return
    cmd, _, arg = (q.data or "").partition(":")

    if cmd in USER_CMDS:
        await on_user_button(update, context, cmd, arg)
        return

    # ---------- أزرار الأدمن فقط ----------
    if not is_admin(q.from_user.id):
        await q.answer("هذه الأزرار للأدمن فقط", show_alert=True)
        return

    if cmd == "lacheck":
        await q.answer("⏳ جاري الفحص…")
        try:
            name = await userbot.check()
            await context.bot.send_message(q.message.chat_id, f"✅ الحساب متصل: {name}")
        except Exception as e:
            await context.bot.send_message(q.message.chat_id, f"❌ {type(e).__name__}: {e}")
        return

    await q.answer()
    admin_chat = q.message.chat_id

    if cmd in ("home", "cancel"):
        context.user_data.pop("state", None)
        await edit(q, *home_view())

    elif cmd == "add":
        context.user_data["state"] = {"step": "add_name"}
        await edit(q, "✏️ أرسل اسم المود الجديد\nمثال: مساعد مبيعات", CANCEL_KB)

    elif cmd == "list":
        context.user_data.pop("state", None)
        await edit(q, *list_view())

    elif cmd == "togglebot":
        DB["enabled"] = not DB["enabled"]
        save_data()
        await edit(q, *home_view())

    elif cmd == "togglesmart":
        DB["smart_enabled"] = not DB["smart_enabled"]
        save_data()
        await edit(q, *home_view())

    # ---------- إدارة المكتبة ----------
    elif cmd == "la":
        context.user_data.pop("state", None)
        await edit(q, *lib_admin_view())

    elif cmd == "lavis":
        DB["lib"]["visible"] = not DB["lib"]["visible"]
        save_data()
        await edit(q, *lib_admin_view())

    elif cmd == "laname":
        context.user_data["state"] = {"step": "lib_name"}
        await edit(q, "✏️ أرسل الاسم الجديد لزر المكتبة (مثل: المصحف الصوتي):", CANCEL_KB)

    elif cmd == "laadd":
        context.user_data["state"] = {"step": "lib_add"}
        await edit(
            q,
            "➕ أرسل أحد التالي:\n"
            "• @username أو رابط القناة العامة\n"
            "• رابط دعوة (t.me/+…) — يحتاج حساب Session\n"
            "• أو حوّل (Forward) أي رسالة من القناة إلى هنا\n\n"
            "⚠️ لكي يقرأ البوت القناة أضفه إليها كمشرف.",
            CANCEL_KB,
        )

    elif cmd == "lasrc":
        context.user_data.pop("state", None)
        await edit(q, *lib_sources_view())

    elif cmd == "lasync":
        if not userbot.configured():
            await context.bot.send_message(admin_chat, f"⚠️ {userbot.why_not()}")
            return
        targets = [arg] if arg != "all" else [c for c, s in DB["lib"]["sources"].items() if s.get("session")]
        if not targets:
            await context.bot.send_message(admin_chat, "⚠️ لا توجد مصادر مربوطة بالحساب.")
            return
        await context.bot.send_message(admin_chat, "⏳ بدأت المزامنة في الخلفية، سأخبرك عند الانتهاء.")
        for cid in targets:
            spawn(sync_source(context.bot, cid, admin_chat))

    elif cmd in ("las", "lalink", "ladel", "ladelok"):
        if arg not in DB["lib"]["sources"]:
            await edit(q, "⚠️ هذا المصدر لم يعد موجوداً.", kb([b("⬅️ رجوع", "lasrc")]))
            return
        if cmd == "las":
            await edit(q, *lib_source_view(arg))
        elif cmd == "lalink":
            if not userbot.configured():
                await edit(q, f"⚠️ ربط الحساب غير مُهيّأ: {userbot.why_not()}", kb([b("⬅️ رجوع", f"las:{arg}")]))
                return
            context.user_data["state"] = {"step": "lib_link", "cid": arg}
            await edit(
                q,
                f"👤 أرسل رابط الدعوة أو @username لـ «{DB['lib']['sources'][arg]['title']}» ليدخله الحساب:",
                CANCEL_KB,
            )
        elif cmd == "ladel":
            name = DB["lib"]["sources"][arg]["title"]
            await edit(
                q,
                f"⚠️ إزالة «{name}» وحذف فهرسه؟ (لا يُحذف شيء من القناة نفسها)",
                kb([b("🗑 نعم، أزله", f"ladelok:{arg}", "danger"), b("↩️ تراجع", f"las:{arg}", "primary")]),
            )
        else:
            DB["lib"]["sources"].pop(arg, None)
            library.delete_chat(int(arg))
            save_data()
            await edit(q, *lib_sources_view())

    elif cmd == "lsrc":
        ok, report = await try_add_source(context.bot, "", int(arg), notify_id=admin_chat)
        await edit(q, report, kb([b("📂 المصادر", "lasrc", "primary")]) if ok else None)

    elif cmd == "lskip":
        await edit(q, "تم التجاهل.")

    # ---------- إدارة المودات ----------
    elif cmd in ("m", "vis", "def", "en", "ep", "show", "del", "delok"):
        if arg not in DB["modes"]:
            await edit(q, "⚠️ هذا المود لم يعد موجوداً.", kb([b("⬅️ رجوع", "list")]))
            return

        if cmd == "m":
            await edit(q, *mode_view(arg))

        elif cmd == "vis":
            m = DB["modes"][arg]
            m["visible"] = not m.get("visible", True)
            save_data()
            await edit(
                q,
                *mode_view(arg, "👁 أصبح ظاهراً للمستخدمين" if m["visible"] else "🚫 تم إخفاؤه عن المستخدمين"),
            )

        elif cmd == "def":
            DB["default"] = None if DB.get("default") == arg else arg
            save_data()
            await edit(q, *mode_view(arg, "⭐ تم تعيينه افتراضياً" if DB["default"] == arg else "☆ أُلغي الافتراضي"))

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
            if DB.get("default") == arg:
                DB["default"] = None
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
            BotCommand("mode", "تغيير المود"),
            BotCommand("new", "محادثة جديدة"),
            BotCommand("id", "معرفة رقمك"),
        ]
    )
    if LIB_SYNC_MINUTES > 0:
        spawn(sync_loop(app))


def start_health_server():
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
    # الفهرسة تعمل في مجموعة مستقلة (-1) فلا تعطّل بقية المعالجات
    app.add_handler(MessageHandler(filters.ALL, on_any_update), group=-1)
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("admin", cmd_start))
    app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.TEXT & ~filters.COMMAND, on_nontext))
    app.add_error_handler(on_error)

    log.info(
        "Bot v3 started. Admins: %s | Fast: %s | Smart: %s | Fallback: %s | Session: %s",
        sorted(ADMIN_IDS), GEMINI_MODEL, SMART_MODEL, FALLBACK_MODEL,
        "on" if userbot.configured() else "off",
    )
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
