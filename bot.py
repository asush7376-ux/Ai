# -*- coding: utf-8 -*-
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
MAX_RELAY_BYTES = 49 * 1024 * 1024

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
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
    lib = d.setdefault("lib", {})
    lib.setdefault("visible", False)
    lib.setdefault("name", "المكتبة")
    lib.setdefault("sources", {})
    return d

def save_data() -> None:
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(DB, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)

DB = load_data()
HISTORY: dict = {}
LOCKS: dict = {}
LIB_STATE: dict = {}
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

# ============================ الأزرار ============================
def b(text: str, data: str, style: str | None = None) -> Btn:
    return Btn(text, callback_data=data, style=style)

def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([list(r) for r in rows])

CANCEL_KB = kb([b("❌ إلغاء", "cancel", "danger")])
BACK_TO_CHAT = b("🎭 العودة للمحادثة", "chg", "primary")

def lib_button_label() -> str:
    L = DB["lib"]
    return ("" if L["visible"] else "🔒 ") + f"📚 {L['name']}"

def reply_kb(chat_id: int) -> InlineKeyboardMarkup:
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

def picker_view(chat_id: int, admin: bool, intro: str = "🎭 اختر المود:"):
    allowed = allowed_modes(admin)
    lib_ok = lib_available(admin)
    if not allowed and not lib_ok:
        return "⚠️ لا توجد مودات متاحة.", None
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
        f"الحالة: {'🟢 يعمل' if DB['enabled'] else '🔴 متوقف'}\n"
        f"🎭 المودات: {len(modes)} (ظاهر: {shown})\n"
        f"⭐ الافتراضي: {default['name'] if default else 'لا يوجد'}\n"
        f"📚 المكتبة: {len(L['sources'])} مصدر · {library.count()} عنصر\n"
        f"👥 المستخدمون: {len(DB['users'])}\n"
    )
    markup = kb(
        [b("➕ إضافة مود", "add", "success")],
        [b("📋 إدارة المودات", "list", "primary")],
        [b("📚 إدارة المكتبة", "la", "primary")],
        [b("⏸ إيقاف البوت", "togglebot", "danger") if DB["enabled"] else b("▶️ تشغيل البوت", "togglebot", "success")],
    )
    return text, markup

def list_view():
    if not DB["modes"]:
        return "📭 لا توجد مودات.", kb([b("➕ إضافة مود", "add", "success")], [b("🏠 الرئيسية", "home")])
    rows = []
    for mid, m in DB["modes"].items():
        visible = m.get("visible", True)
        star = "⭐ " if DB.get("default") == mid else ""
        rows.append([b(("👁 " if visible else "🚫 ") + star + m["name"], f"m:{mid}", "success" if visible else None)])
    rows.append([b("➕ إضافة مود", "add", "success"), b("🏠 الرئيسية", "home")])
    return "📋 المودات:", kb(*rows)

def mode_view(mid: str, notice: str = ""):
    m = DB["modes"][mid]
    visible = m.get("visible", True)
    is_default = DB.get("default") == mid
    preview = m["prompt"] if len(m["prompt"]) <= 500 else m["prompt"][:500] + "…"
    text = (f"{notice}\n\n" if notice else "") + (
        f"🎭 المود: {m['name']}\n"
        f"الظهور: {'👁 ظاهر' if visible else '🚫 مخفي'}\n"
        f"الافتراضي: {'⭐ نعم' if is_default else 'لا'}\n\n"
        f"📝 البرومبت:\n{preview}"
    )
    markup = kb(
        [b("🚫 إخفاء", f"vis:{mid}", "danger") if visible else b("👁 إظهار", f"vis:{mid}", "success")],
        [b("☆ إلغاء الافتراضي", f"def:{mid}", "primary") if is_default else b("⭐ جعله افتراضي", f"def:{mid}", "success")],
        [b("✏️ تعديل الاسم", f"en:{mid}", "primary"), b("📝 تعديل البرومبت", f"ep:{mid}", "primary")],
        [b("📄 عرض البرومبت", f"show:{mid}")],
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

async def call_gemini(model: str, system_prompt: str, contents: list, timeout: float = 30.0, json_mode: bool = False) -> str:
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
        raise GeminiError(f"تعذر الاتصال ({type(e).__name__})")

    if r.status_code != 200:
        try:
            msg = r.json()["error"]["message"]
        except Exception:
            msg = r.text[:200]
        hints = {400: "طلب غير صحيح", 401: "مفتاح غير صالح", 403: "لا صلاحية", 404: "موديل غير موجود", 429: "تجاوز الحد", 503: "ضغط عالٍ"}
        raise GeminiError(f"خطأ Gemini ({r.status_code}): {hints.get(r.status_code, '')}\n{msg[:300]}", fatal=r.status_code in (400, 401, 403))

    data = r.json()
    cands = data.get("candidates") or []
    if not cands:
        raise GeminiError("لم يصل رد", fatal=True)
    parts = cands[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
    if not text:
        raise GeminiError("رد فارغ")
    return text

async def ask_gemini(system_prompt: str, contents: list, smart: bool = False, json_mode: bool = False):
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

# ============================ الرد ============================
async def respond(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, text: str) -> None:
    bot = context.bot
    admin = is_admin(user_id)

    if not DB["enabled"] and not admin:
        await bot.send_message(chat_id, "⏸ البوت متوقف.")
        return

    p = prefs(chat_id)
    if p.get("lib"):
        if lib_available(admin):
            await lib_handle(context, chat_id, admin, text)
            return
        p["lib"] = False
        save_data()

    mid = resolve_mode(chat_id, admin)
    if not mid:
        context.user_data["pending"] = text
        t, m = picker_view(chat_id, admin, "🎭 اختر المود أولاً:")
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
            await bot.send_message(chat_id, f"❌ {e}" if admin else "⚠️ خطأ مؤقت.", reply_markup=reply_kb(chat_id))
            return

        msgs = (contents + [{"role": "model", "parts": [{"text": reply}]}])[-MAX_HISTORY:]
        while msgs and msgs[0]["role"] != "user":
            msgs.pop(0)
        h["msgs"] = msgs

    parts = list(chunks(clean(reply)))
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        await bot.send_message(chat_id, part, reply_markup=reply_kb(chat_id) if last else None)

# ============================ المكتبة ============================
LIB_PARSE_PROMPT = """أنت محلل طلبات لمكتبة وسائط.
حلل رسالة المستخدم وأعد JSON فقط:
{"intent":"search","query":"عبارة قصيرة","terms":[["كلمة","مرادف"],["كلمة2"]],"media":"any","numbers":[],"want_all":false}

القواعد:
- intent واحد من: search، select، send_all، more، other.
- terms: مفاهيم البحث. **مهم جداً:** لا تضف أي كلمة لم يذكرها المستخدم صراحة في رسالته (مثل إضافة "سورة" إذا لم يقلها، أو "كتاب" إذا لم يقلها). استخرج الكلمات كما هي فقط.
  مثال: «سورة البقرة» => [["سورة","surah"],["البقرة","baqarah"]]
  مثال: «كتاب الفتن» => [["كتاب"],["الفتن"]]
  مثال: «نشيد عن الجهاد» => [["نشيد"],["الجهاد"]]
- media: audio، video، photo، document، text، وإلا any.
- want_all=true فقط إذا طلب صراحة إرسال كل النتائج.
- numbers: أرقام القائمة التي اختارها مثل «الثالث» => [3]."""

_STOP = {"ارسل", "ارسلي", "اعطني", "عطني", "ابغي", "ابي", "اريد", "هات", "ممكن", "لو", "سمحت", "من", "في", "عن", "علي", "الي", "هل", "يوجد", "عندك", "عندكم", "لي", "لنا"}

def fallback_terms(text: str) -> list:
    return [[w] for w in library.normalize(text).split() if w not in _STOP and len(w) >= 2][:5]

async def lib_understand(text: str, st) -> dict:
    plan = {"intent": "search", "query": text[:40], "terms": [], "media": "any", "numbers": [], "want_all": False}
    if st and re.fullmatch(r"\d{1,3}", library.normalize(text)):
        plan.update(intent="select", numbers=[int(library.normalize(text))])
        return plan

    ctx = f"قائمة سابقة: {'نعم' if st else 'لا'}\nرسالة: {text}"
    try:
        raw, _ = await ask_gemini(LIB_PARSE_PROMPT, [{"role": "user", "parts": [{"text": ctx}]}], json_mode=True)
        raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
        data = json.loads(raw)
        if isinstance(data, list) and data: data = data[0]
        if not isinstance(data, dict): raise ValueError
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
        if g: terms.append(g)
    plan["terms"] = terms or fallback_terms(text)
    if data.get("media") in library.KIND_GROUPS: plan["media"] = data["media"]
    plan["numbers"] = [int(n) for n in (data.get("numbers") or []) if str(n).lstrip("-").isdigit()]
    plan["want_all"] = bool(data.get("want_all"))
    plan["query"] = str(data.get("query") or text)[:40]
    return plan

def lib_intro_text() -> str:
    return f"📚 {DB['lib']['name']}\n\nاكتب ما تبحث عنه (صوتيات، فيديو، صور، ملفات…)\nمثال: «كتاب الفتن» أو «سورة البقرة»."

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
        if not r: continue
        num = page * LIB_PAGE + off + 1
        lines.append(f"{num}. {library.label(r)}")
        num_btns.append(b(str(num), f"lp:{rid}", "primary"))

    text = f"🔎 وجدت {n} نتيجة عن «{st['query']}»\n{st['summary']}\n\n" + "\n".join(lines) + f"\n\n📄 صفحة {page + 1}/{pages}\nاختر رقماً أو اكتب: «أرسل الثالث»."
    rows = [num_btns[i:i + 4] for i in range(0, len(num_btns), 4)]
    nav = []
    if page > 0: nav.append(b("◀️ السابق", f"lpg:{page - 1}"))
    if page < pages - 1: nav.append(b("التالي ▶️", f"lpg:{page + 1}"))
    if nav: rows.append(nav)
    rows.append([b(f"📤 إرسال الكل ({min(n, LIB_SEND_LIMIT)})", "lall", "success"), b("❌ إغلاق", "closepick", "danger")])
    return text, kb(*rows)

def lib_after_kb(chat_id: int):
    rows = []
    st = LIB_STATE.get(chat_id)
    if st and len(st["ids"]) > 1: rows.append([b("🔎 الرجوع للنتائج", f"lpg:{st['page']}", "primary")])
    rows.append([BACK_TO_CHAT])
    return kb(*rows)

async def relay_item(bot, chat_id: int, it: dict, src: dict) -> None:
    if it.get("size") and it["size"] > MAX_RELAY_BYTES: raise ValueError("big")
    k = it["kind"]
    if k == "text":
        await bot.send_message(chat_id, (it.get("text") or it.get("name") or "")[:4096])
        return
    data = await userbot.download(int(it["chat_id"]), src.get("access_hash"), it["msg_id"])
    if len(data) > MAX_RELAY_BYTES: raise ValueError("big")
    cap = (it.get("caption") or "")[:1000] or None
    fname = it.get("filename") or f"{k}_{it['msg_id']}{it.get('ext') or ''}"
    if k == "audio": await bot.send_audio(chat_id, data, caption=cap, title=it.get("title"), performer=it.get("performer"), duration=it.get("duration"), filename=fname)
    elif k == "voice": await bot.send_voice(chat_id, data, caption=cap, duration=it.get("duration"))
    elif k == "video": await bot.send_video(chat_id, data, caption=cap, duration=it.get("duration"), filename=fname)
    elif k == "animation": await bot.send_animation(chat_id, data, caption=cap, filename=fname)
    elif k == "photo": await bot.send_photo(chat_id, data, caption=cap)
    else: await bot.send_document(chat_id, data, caption=cap, filename=fname)

async def deliver(context, chat_id: int, rid: int) -> bool:
    bot = context.bot
    it = library.get(rid)
    if not it: return False
    src = DB["lib"]["sources"].get(str(it["chat_id"]))
    if not src: return False

    # 1. التحويل المباشر عبر الحساب (الأسرع والأخف) - الميزة الجديدة
    if src.get("session") and userbot.configured():
        try:
            await userbot.forward_to_user(int(it["chat_id"]), src.get("access_hash"), it["msg_id"], chat_id)
            return True
        except Exception as e:
            log.warning("Userbot forward failed: %s", e)

    # 2. النسخ المباشر من البوت
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
                    library.delete_item(rid)
                    return False
                break

    # 3. التحميل والرفع (كخيار أخير)
    if src.get("session") and userbot.configured():
        try:
            await relay_item(bot, chat_id, it, src)
            return True
        except ValueError: pass
        except Exception as e: log.warning("relay failed: %s: %s", type(e).__name__, e)

    # 4. رابط
    link = library.link_for(src, it)
    if link:
        await bot.send_message(chat_id, f"🔗 {library.label(it)}\n{link}")
        return True
    return False

async def lib_send_many(context, chat_id: int, ids: list, total: int | None = None) -> None:
    bot = context.bot
    note = f"📤 جاري إرسال {len(ids)} ملف…"
    if total and total > len(ids): note += f"\n(الحد {LIB_SEND_LIMIT} من أصل {total})"
    await bot.send_message(chat_id, note)
    ok = 0
    for rid in ids:
        if await deliver(context, chat_id, rid): ok += 1
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

    rows = library.search(plan["terms"], plan["media"])
    query = plan["query"]
    if not rows:
        await bot.send_message(chat_id, f"😕 لم أجد شيئاً عن «{query}».\nجرّب كلمات أخرى.", reply_markup=back)
        return

    n = len(rows)
    if n == 1:
        await bot.send_message(chat_id, f"✅ وجدت ملفاً واحداً:\n{library.label(rows[0])}\n⏳ جاري الإرسال…")
        ok = await deliver(context, chat_id, rows[0]["id"])
        await bot.send_message(chat_id, "✅ تم الإرسال." if ok else "⚠️ تعذر إرسال الملف.", reply_markup=back)
        return

    LIB_STATE[chat_id] = {"ids": [r["id"] for r in rows], "query": query, "summary": library.summarize(rows), "page": 0, "ts": time.time()}
    if plan["want_all"] and n <= LIB_SEND_LIMIT:
        await bot.send_message(chat_id, f"🔎 وجدت {n} نتيجة عن «{query}».")
        await lib_send_many(context, chat_id, LIB_STATE[chat_id]["ids"])
        return
    t, m = lib_list_view(chat_id, 0)
    await bot.send_message(chat_id, t, reply_markup=m)

# ============================ إدارة المصادر ============================
async def on_any_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg: return
    if str(msg.chat_id) not in DB["lib"]["sources"]: return
    try:
        it = library.item_from_ptb(msg, include_text=(msg.chat.type == "channel"))
        if it: library.upsert([it])
    except Exception:
        log.exception("index failed")

async def sync_source(bot, cid: str, notify_id: int | None = None) -> int:
    src = DB["lib"]["sources"].get(cid)
    if not src or not src.get("session") or not userbot.configured(): return 0
    total = 0
    def on_batch(batch):
        nonlocal total
        total += library.upsert(batch)
    try:
        await userbot.fetch(int(cid), src.get("access_hash"), library.max_msg_id(int(cid)), on_batch)
    except Exception as e:
        if notify_id: await bot.send_message(notify_id, f"❌ فشلت مزامنة «{src['title']}»:\n{type(e).__name__}: {e}")
        return total
    src["last_sync"] = int(time.time())
    save_data()
    if notify_id: await bot.send_message(notify_id, f"✅ انتهت مزامنة «{src['title']}»: {total} عنصر جديد.")
    return total

async def sync_loop(app: Application):
    await asyncio.sleep(30)
    while True:
        if userbot.configured():
            for cid, src in list(DB["lib"]["sources"].items()):
                if src.get("session"):
                    try: await sync_source(app.bot, cid)
                    except Exception: log.exception("sync_loop error")
        await asyncio.sleep(max(5, LIB_SYNC_MINUTES) * 60)

def parse_bot_target(ref: str):
    ref = (ref or "").strip()
    if not ref: return None
    if re.fullmatch(r"-?\d{6,}", ref): return int(ref)
    m = re.search(r"t\.me/c/(\d+)", ref)
    if m: return int("-100" + m.group(1))
    kind, val = userbot.parse_ref(ref)
    return "@" + val if kind == "user" else None

def forward_chat_id(msg):
    fo = getattr(msg, "forward_origin", None)
    if fo is None: return None
    ch = getattr(fo, "chat", None) or getattr(fo, "sender_chat", None)
    return ch.id if ch else None

async def try_add_source(bot, ref: str, fwd, expect_cid=None, notify_id=None):
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
            else: errs.append("البوت ليس عضواً")
        except TelegramError as e: errs.append(f"البوت: {e}")

    if ref and userbot.configured():
        try:
            pid, ttl, uname, ahash = await userbot.resolve_and_join(ref)
            if cid is None or cid == pid:
                cid, title, username, sess_ok = pid, title or ttl, username or uname, True
            else: errs.append("معرّف المصدر لا يطابق")
        except Exception as e: errs.append(f"الحساب: {type(e).__name__}: {e}")

    if cid is None or (expect_cid and str(cid) != str(expect_cid)):
        hint = "\n\n• أضف البوت كمشرف.\n• أو أرسل رابط دعوة ليدخل الحساب."
        return False, "❌ تعذر الربط.\n" + "\n".join(errs) + hint

    key = str(cid)
    src = DB["lib"]["sources"].setdefault(key, {"title": title, "username": username, "bot": False, "session": False, "access_hash": None, "last_sync": 0})
    src["title"] = title or src["title"]
    if username: src["username"] = username
    if bot_ok: src["bot"] = True
    if sess_ok:
        src["session"] = True
        src["access_hash"] = ahash
    save_data()

    msg = [f"✅ تم ربط «{src['title']}»", f"{'🤖 بوت ' if src['bot'] else ''}{'👤 حساب' if src['session'] else ''}"]
    if sess_ok:
        spawn(sync_source(bot, key, notify_id))
        msg.append("⏳ بدأت الفهرسة في الخلفية.")
    elif bot_ok:
        msg.append("ℹ️ سيفهرس البوت الجديد فقط. للمحتوى القديم اربط حساب.")
    if errs: msg.append("⚠️ " + " | ".join(errs))
    return True, "\n".join(msg)

async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cm = update.my_chat_member
    chat = cm.chat
    if chat.type == "private": return
    cid = str(chat.id)
    new = cm.new_chat_member.status
    src = DB["lib"]["sources"].get(cid)
    if new in ("administrator", "member"):
        if src:
            src["bot"] = True; save_data(); return
        for aid in ADMIN_IDS:
            try:
                await context.bot.send_message(aid, f"🤖 تمت إضافتي إلى «{chat.title}».\nهل تريد اعتماده؟", reply_markup=kb([b("✅ اعتماده", f"lsrc:{cid}", "success"), b("❌ تجاهل", "lskip", "danger")]))
            except TelegramError: pass
    elif new in ("left", "kicked") and src:
        src["bot"] = False; save_data()

def lib_admin_view():
    L = DB["lib"]
    sess = "✅ مُهيّأ" if userbot.configured() else f"⚪ غير مُهيّأ"
    text = f"📚 إدارة المكتبة\n\nالزر: «{L['name']}» — {'👁 ظاهر' if L['visible'] else '🚫 مخفي'}\n📂 المصادر: {len(L['sources'])}\n🗂 العناصر: {library.count()}\n👤 الحساب: {sess}"
    rows = [
        [b("🚫 إخفاء الزر", "lavis", "danger") if L["visible"] else b("👁 إظهار الزر", "lavis", "success")],
        [b("✏️ تسمية الزر", "laname", "primary")],
        [b("➕ إضافة مصدر", "laadd", "success"), b("📂 المصادر", "lasrc", "primary")],
    ]
    if userbot.configured(): rows.append([b("🔄 مزامنة الكل", "lasync:all", "primary"), b("🧪 فحص الحساب", "lacheck")])
    rows.append([b("🏠 الرئيسية", "home")])
    return text, kb(*rows)

def lib_sources_view():
    srcs = DB["lib"]["sources"]
    if not srcs: return "📭 لا توجد مصادر.", kb([b("➕ إضافة مصدر", "laadd", "success")], [b("⬅️ رجوع", "la")])
    rows = []
    for cid, s in srcs.items():
        flags = ("🤖" if s.get("bot") else "") + ("👤" if s.get("session") else "")
        rows.append([b(f"{flags} {s['title']} ({library.count(int(cid))})", f"las:{cid}", "primary")])
    rows.append([b("➕ إضافة مصدر", "laadd", "success"), b("⬅️ رجوع", "la")])
    return "📂 المصادر:", kb(*rows)

def lib_source_view(cid: str, notice: str = ""):
    s = DB["lib"]["sources"][cid]
    last = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["last_sync"])) if s.get("last_sync") else "—"
    text = (notice + "\n\n" if notice else "") + f"📂 {s['title']}\n🆔 {cid}\n{'🔗 @' + s['username'] if s.get('username') else '🔒 خاص'}\n🤖 البوت: {'✅' if s.get('bot') else '❌'}\n👤 الحساب: {'✅' if s.get('session') else '❌'}\n🗂 العناصر: {library.count(int(cid))}\nآخر مزامنة: {last}"
    rows = []
    if s.get("session") and userbot.configured(): rows.append([b("🔄 مزامنة الآن", f"lasync:{cid}", "success")])
    rows.append([b("👤 ربط/تحديث الحساب", f"lalink:{cid}", "primary")])
    rows.append([b("🗑 إزالة المصدر", f"ladel:{cid}", "danger")])
    rows.append([b("⬅️ رجوع", "lasrc")])
    return text, kb(*rows)

# ============================ الأوامر ============================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    context.user_data.pop("state", None)
    if not ADMIN_IDS:
        await update.message.reply_text(f"⚠️ ضع رقمك في ADMIN_IDS:\n{uid}")
        return
    if is_admin(uid):
        text, markup = home_view()
        await update.message.reply_text(text, reply_markup=markup)
        return
    if not DB["enabled"]:
        await update.message.reply_text("⏸ متوقف.")
        return
    allowed = allowed_modes(False)
    lib_ok = lib_available(False)
    if not allowed and not lib_ok: await update.message.reply_text("⚠️ لا مودات.")
    elif len(allowed) + (1 if lib_ok else 0) == 1 and allowed:
        resolve_mode(chat_id, False)
        await update.message.reply_text("👋 أهلاً بك! اكتب رسالتك.")
    else:
        t, m = picker_view(chat_id, False, "👋 أهلاً! اختر المود:")
        await update.message.reply_text(t, reply_markup=m)

async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = is_admin(update.effective_user.id)
    if not DB["enabled"] and not admin: return
    t, m = picker_view(update.effective_chat.id, admin)
    await update.message.reply_text(t, reply_markup=m)

async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"🆔 رقمك: {update.effective_user.id}")

async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    HISTORY.pop(update.effective_chat.id, None)
    LIB_STATE.pop(update.effective_chat.id, None)
    await update.message.reply_text("🔄 محادثة جديدة.")

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("state", None)
    await update.message.reply_text("تم الإلغاء.")

# ============================ الرسائل ============================
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    text = update.message.text.strip()
    user = update.effective_user
    chat = update.effective_chat
    if str(chat.id) in DB["lib"]["sources"]: return
    if is_admin(user.id) and chat.type == "private" and context.user_data.get("state"):
        await handle_admin_input(update, context, text)
        return
    await respond(context, chat.id, user.id, text)

async def on_nontext(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not is_admin(update.effective_user.id): return
    st = context.user_data.get("state")
    if st and st.get("step") in ("lib_add", "lib_link"): await handle_admin_input(update, context, "")

async def handle_admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    st = context.user_data["state"]
    step = st["step"]
    reply = update.message.reply_text

    if step == "add_name":
        if len(text) > 40:
            await reply("⚠️ الاسم طويل:", reply_markup=CANCEL_KB); return
        context.user_data["state"] = {"step": "add_prompt", "name": text}
        await reply(f"📝 أرسل البرومبت لمود «{text}»:", reply_markup=CANCEL_KB)
    elif step == "add_prompt":
        mid = uuid.uuid4().hex[:8]
        DB["modes"][mid] = {"name": st["name"], "prompt": text, "visible": True}
        save_data()
        context.user_data.pop("state", None)
        await reply(f"✅ تم حفظ المود.", reply_markup=kb([b("⭐ جعله افتراضي", f"def:{mid}", "success")], [b("➕ إضافة آخر", "add", "primary"), b("📋 المودات", "list", "primary")], [b("🏠 الرئيسية", "home")]))
    elif step in ("edit_name", "edit_prompt"):
        mid = st["id"]
        context.user_data.pop("state", None)
        if mid not in DB["modes"]: await reply("⚠️ غير موجود."); return
        if step == "edit_name":
            if len(text) > 40: await reply("⚠️ الاسم طويل."); return
            DB["modes"][mid]["name"] = text
        else: DB["modes"][mid]["prompt"] = text
        save_data()
        await reply(*mode_view(mid, "✅ تم التعديل"))
    elif step == "lib_name":
        if not text or len(text) > 30: await reply("⚠️ 1-30 حرف:", reply_markup=CANCEL_KB); return
        DB["lib"]["name"] = text
        save_data()
        context.user_data.pop("state", None)
        await reply("✅ تم التغيير.", reply_markup=lib_admin_view()[1])
    elif step in ("lib_add", "lib_link"):
        fwd = forward_chat_id(update.message)
        ref = "" if fwd else text
        await reply("⏳ جاري الفحص…")
        ok, report = await try_add_source(context.bot, ref, fwd, expect_cid=st.get("cid"), notify_id=update.effective_user.id)
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
        HISTORY.pop(chat_id, None); LIB_STATE.pop(chat_id, None); await q.answer("🔄 جديدة")
    elif cmd == "closepick":
        await q.answer()
        try: await q.message.delete()
        except BadRequest: pass
    elif cmd == "chg":
        if not DB["enabled"] and not admin: return await q.answer("⏸ متوقف", show_alert=True)
        t, m = picker_view(chat_id, admin)
        if not m: return await q.answer("لا مودات", show_alert=True)
        await q.answer()
        await context.bot.send_message(chat_id, t, reply_markup=m)
    elif cmd == "pick":
        mode = allowed_modes(admin).get(arg)
        if not mode: return await q.answer("غير متاح", show_alert=True)
        p = prefs(chat_id); p["mode"] = arg; p["lib"] = False; save_data()
        await q.answer(f"✅ {mode['name']}")
        pending = context.user_data.pop("pending", None)
        if pending:
            await edit(q, f"✅ «{mode['name']}» — جاري الرد…"); await respond(context, chat_id, uid, pending)
        else: await edit(q, f"✅ «{mode['name']}»\nأرسل رسالتك.")
    elif cmd == "smart":
        if not DB["smart_enabled"]: return await q.answer("غير متاح", show_alert=True)
        p = prefs(chat_id); p["smart"] = not p["smart"]; save_data()
        await q.answer("🧠 أذكى" if p["smart"] else "⚡ سريع")
        try: await q.edit_message_reply_markup(reply_markup=reply_kb(chat_id))
        except BadRequest: pass
    elif cmd in ("lib", "libp"):
        if not DB["enabled"] and not admin: return await q.answer("⏸", show_alert=True)
        if not lib_available(admin): return await q.answer("غير متاحة", show_alert=True)
        p = prefs(chat_id); p["lib"] = True; save_data()
        LIB_STATE.pop(chat_id, None); await q.answer()
        markup = kb([BACK_TO_CHAT])
        if cmd == "libp": await edit(q, lib_intro_text(), markup)
        else: await context.bot.send_message(chat_id, lib_intro_text(), reply_markup=markup)
    elif cmd in ("lp", "lpg", "lall"):
        if not lib_available(admin): return await q.answer("غير متاحة", show_alert=True)
        if cmd == "lp":
            if not arg.isdigit(): return await q.answer()
            await q.answer("⏳ إرسال…")
            if not await deliver(context, chat_id, int(arg)): await context.bot.send_message(chat_id, "⚠️ تعذر الإرسال.")
            return
        st = LIB_STATE.get(chat_id)
        if not st: return await q.answer("انتهت الصلاحية", show_alert=True)
        await q.answer()
        if cmd == "lpg": await edit(q, *lib_list_view(chat_id, int(arg) if arg.lstrip("-").isdigit() else 0))
        else: await lib_send_many(context, chat_id, st["ids"][:LIB_SEND_LIMIT], total=len(st["ids"]))

async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.message is None: return await q.answer()
    cmd, _, arg = (q.data or "").partition(":")
    if cmd in USER_CMDS: return await on_user_button(update, context, cmd, arg)
    if not is_admin(q.from_user.id): return await q.answer("للأدمن فقط", show_alert=True)

    if cmd == "lacheck":
        await q.answer("⏳ فحص…")
        try:
            name = await userbot.check()
            await context.bot.send_message(q.message.chat_id, f"✅ متصل: {name}")
        except Exception as e: await context.bot.send_message(q.message.chat_id, f"❌ {type(e).__name__}: {e}")
        return

    await q.answer()
    admin_chat = q.message.chat_id

    if cmd in ("home", "cancel"):
        context.user_data.pop("state", None); await edit(q, *home_view())
    elif cmd == "add":
        context.user_data["state"] = {"step": "add_name"}; await edit(q, "✏️ اسم المود:", CANCEL_KB)
    elif cmd == "list":
        context.user_data.pop("state", None); await edit(q, *list_view())
    elif cmd == "togglebot":
        DB["enabled"] = not DB["enabled"]; save_data(); await edit(q, *home_view())
    elif cmd == "togglesmart":
        DB["smart_enabled"] = not DB["smart_enabled"]; save_data(); await edit(q, *home_view())
    elif cmd == "la":
        context.user_data.pop("state", None); await edit(q, *lib_admin_view())
    elif cmd == "lavis":
        DB["lib"]["visible"] = not DB["lib"]["visible"]; save_data(); await edit(q, *lib_admin_view())
    elif cmd == "laname":
        context.user_data["state"] = {"step": "lib_name"}; await edit(q, "✏️ اسم الزر الجديد:", CANCEL_KB)
    elif cmd == "laadd":
        context.user_data["state"] = {"step": "lib_add"}; await edit(q, "➕ أرسل @username أو رابط أو حوّل رسالة:", CANCEL_KB)
    elif cmd == "lasrc":
        context.user_data.pop("state", None); await edit(q, *lib_sources_view())
    elif cmd == "lasync":
        if not userbot.configured(): return await context.bot.send_message(admin_chat, "⚠️ الحساب غير مهيأ.")
        targets = [arg] if arg != "all" else [c for c, s in DB["lib"]["sources"].items() if s.get("session")]
        if not targets: return await context.bot.send_message(admin_chat, "⚠️ لا مصادر مربوطة.")
        await context.bot.send_message(admin_chat, "⏳ مزامنة في الخلفية…")
        for cid in targets: spawn(sync_source(context.bot, cid, admin_chat))
    elif cmd in ("las", "lalink", "ladel", "ladelok"):
        if arg not in DB["lib"]["sources"]: return await edit(q, "⚠️ غير موجود.", kb([b("⬅️ رجوع", "lasrc")]))
        if cmd == "las": await edit(q, *lib_source_view(arg))
        elif cmd == "lalink":
            if not userbot.configured(): return await edit(q, "⚠️ الحساب غير مهيأ.", kb([b("⬅️ رجوع", f"las:{arg}")]))
            context.user_data["state"] = {"step": "lib_link", "cid": arg}
            await edit(q, "👤 أرسل الرابط ليدخله الحساب:", CANCEL_KB)
        elif cmd == "ladel":
            await edit(q, f"⚠️ إزالة «{DB['lib']['sources'][arg]['title']}»؟", kb([b("🗑 نعم", f"ladelok:{arg}", "danger"), b("↩️ تراجع", f"las:{arg}", "primary")]))
        else:
            DB["lib"]["sources"].pop(arg, None); library.delete_chat(int(arg)); save_data(); await edit(q, *lib_sources_view())
    elif cmd == "lsrc":
        ok, report = await try_add_source(context.bot, "", int(arg), notify_id=admin_chat)
        await edit(q, report, kb([b("📂 المصادر", "lasrc", "primary")]) if ok else None)
    elif cmd == "lskip":
        await edit(q, "تم التجاهل.")
    elif cmd in ("m", "vis", "def", "en", "ep", "show", "del", "delok"):
        if arg not in DB["modes"]: return await edit(q, "⚠️ غير موجود.", kb([b("⬅️ رجوع", "list")]))
        if cmd == "m": await edit(q, *mode_view(arg))
        elif cmd == "vis":
            m = DB["modes"][arg]; m["visible"] = not m.get("visible", True); save_data()
            await edit(q, *mode_view(arg, "👁 ظاهر" if m["visible"] else "🚫 مخفي"))
        elif cmd == "def":
            DB["default"] = None if DB.get("default") == arg else arg; save_data()
            await edit(q, *mode_view(arg, "⭐ افتراضي" if DB["default"] == arg else "☆ ألغي"))
        elif cmd == "en":
            context.user_data["state"] = {"step": "edit_name", "id": arg}; await edit(q, "✏️ الاسم الجديد:", CANCEL_KB)
        elif cmd == "ep":
            context.user_data["state"] = {"step": "edit_prompt", "id": arg}; await edit(q, "📝 البرومبت الجديد:", CANCEL_KB)
        elif cmd == "show":
            for part in chunks(DB["modes"][arg]["prompt"]): await q.message.reply_text(part)
        elif cmd == "del":
            await edit(q, f"⚠️ حذف «{DB['modes'][arg]['name']}»؟", kb([b("🗑 نعم", f"delok:{arg}", "danger"), b("↩️ تراجع", f"m:{arg}", "primary")]))
        elif cmd == "delok":
            DB["modes"].pop(arg, None)
            if DB.get("default") == arg: DB["default"] = None
            save_data(); await edit(q, *list_view())

# ============================ التشغيل ============================
async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    if isinstance(context.error, Conflict): log.warning("409 Conflict: نسخة أخرى تعمل.")
    else: log.error("Unhandled exception", exc_info=context.error)

async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "بدء"), BotCommand("mode", "تغيير المود"),
        BotCommand("new", "محادثة جديدة"), BotCommand("id", "معرفة رقمك"),
    ])
    if LIB_SYNC_MINUTES > 0: spawn(sync_loop(app))

def start_health_server():
    port = os.environ.get("PORT")
    if not port: return
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers(); self.wfile.write(b"OK")
        do_HEAD = do_GET
        def log_message(self, *args): pass
    server = HTTPServer(("0.0.0.0", int(port)), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()

def main():
    if not TELEGRAM_TOKEN or not GEMINI_API_KEY:
        raise SystemExit("❌ ضع TELEGRAM_TOKEN و GEMINI_API_KEY")
    start_health_server()
    app = (Application.builder().token(TELEGRAM_TOKEN).concurrent_updates(True).post_init(post_init).build())
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
    log.info("Bot v3 started. Session: %s", "on" if userbot.configured() else "off")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
