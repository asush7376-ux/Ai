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
    Application, CallbackQueryHandler, ChatMemberHandler, CommandHandler,
    ContextTypes, MessageHandler, filters,
)

import library
import userbot

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
SMART_MODEL = os.environ.get("GEMINI_SMART_MODEL", "gemini-pro-latest")
FALLBACK_MODEL = os.environ.get("GEMINI_FALLBACK_MODEL", "gemini-flash-lite-latest")
ADMIN_IDS = {int(x) for x in re.findall(r"\d+", os.environ.get("ADMIN_IDS", ""))}
DATA_FILE = os.environ.get("DATA_FILE", "data.json")
RELAY_CHAT_ID = int(os.environ.get("RELAY_CHAT_ID", "0")) # آيدي مجموعة العبور

MAX_HISTORY = 20
LIB_SEND_LIMIT = int(os.environ.get("LIB_SEND_LIMIT", "25"))
LIB_SYNC_MINUTES = int(os.environ.get("LIB_SYNC_MINUTES", "60"))
LIB_PAGE = 8

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("bot")

def load_data() -> dict:
    try:
        with open(DATA_FILE, encoding="utf-8") as f: d = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError): d = {}
    d.setdefault("modes", {})
    old_active = d.pop("active", None)
    d.setdefault("default", old_active)
    d.setdefault("enabled", True)
    d.setdefault("smart_enabled", True)
    d.setdefault("users", {})
    for m in d["modes"].values(): m.setdefault("visible", True)
    
    lib = d.setdefault("lib", {})
    lib.setdefault("visible", False)
    lib.setdefault("name", "المكتبة")
    lib.setdefault("sources", {})
    lib.setdefault("folders", {})
    return d

def save_data() -> None:
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f: json.dump(DB, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)

DB = load_data()
HISTORY: dict = {}
LOCKS: dict = {}
LIB_STATE: dict = {}
TASKS: set = set()

def spawn(coro):
    t = asyncio.create_task(coro); TASKS.add(t); t.add_done_callback(TASKS.discard); return t

def is_admin(uid: int) -> bool: return uid in ADMIN_IDS

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
    if p["mode"] in allowed: return p["mode"]
    chosen = None
    if DB.get("default") in allowed: chosen = DB["default"]
    elif len(allowed) == 1: chosen = next(iter(allowed))
    if chosen:
        p["mode"] = chosen; save_data()
    return chosen

def b(text: str, data: str, style: str | None = None) -> Btn:
    return Btn(text, callback_data=data, style=style)

def kb(*rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([list(r) for r in rows])

CANCEL_KB = kb([b("❌ إلغاء", "cancel", "danger")])

def reply_kb(chat_id: int) -> InlineKeyboardMarkup:
    admin = is_admin(chat_id)
    p = prefs(chat_id)
    mode = DB["modes"].get(p["mode"])
    name = mode["name"] if mode else "—"
    rows = [[b(f"🎭 تغيير المود · {name}", "chg", "primary")]]
    
    if lib_available(admin):
        rows.append([b(f"📚 {DB['lib']['name']}", "lib", "success")])
        
    second = []
    if DB["smart_enabled"]:
        second.append(b("⚡ سريع", "smart", "primary") if p["smart"] else b("🧠 أذكى", "smart", "success"))
    second.append(b("🔄 جديدة", "newchat", "danger"))
    rows.append(second)
    return kb(*rows)

def picker_view(chat_id: int, admin: bool, intro: str = "🎭 اختر المود:"):
    allowed = allowed_modes(admin)
    if not allowed: return "⚠️ لا توجد مودات.", None
    p = prefs(chat_id)
    current = p["mode"]
    rows = []
    for mid, m in allowed.items():
        if mid == current: label, style = "✅ " + m["name"], "success"
        elif not m.get("visible", True): label, style = "🔒 " + m["name"], None
        else: label, style = "🎭 " + m["name"], "primary"
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
        f"📚 المكتبة: {len(L['sources'])} مصدر · {len(L['folders'])} مجلد\n"
        f"👥 المستخدمون: {len(DB['users'])}\n"
        f"🚀 مجموعة العبور: {'✅ مُفعلة' if RELAY_CHAT_ID != 0 else '❌ غير مُفعلة'}\n"
    )
    markup = kb(
        [b("➕ إضافة مود", "add", "success")],
        [b("📋 إدارة المودات", "list", "primary")],
        [b("📚 إدارة المكتبة", "la", "primary")],
        [b("⏸ إيقاف", "togglebot", "danger") if DB["enabled"] else b("▶️ تشغيل", "togglebot", "success")],
    )
    return text, markup

def list_view():
    if not DB["modes"]: return "📭 لا توجد مودات.", kb([b("➕ إضافة", "add", "success")], [b("🏠 الرئيسية", "home")])
    rows = []
    for mid, m in DB["modes"].items():
        visible = m.get("visible", True)
        star = "⭐ " if DB.get("default") == mid else ""
        rows.append([b(("👁 " if visible else "🚫 ") + star + m["name"], f"m:{mid}", "success" if visible else None)])
    rows.append([b("➕ إضافة", "add", "success"), b("🏠 الرئيسية", "home")])
    return "📋 المودات:", kb(*rows)

def mode_view(mid: str, notice: str = ""):
    m = DB["modes"][mid]
    visible = m.get("visible", True)
    is_default = DB.get("default") == mid
    preview = m["prompt"] if len(m["prompt"]) <= 500 else m["prompt"][:500] + "…"
    text = (f"{notice}\n\n" if notice else "") + f"🎭 {m['name']}\nالظهور: {'👁' if visible else '🚫'}\nالافتراضي: {'⭐' if is_default else 'لا'}\n\n📝 {preview}"
    markup = kb(
        [b("🚫 إخفاء", f"vis:{mid}", "danger") if visible else b("👁 إظهار", f"vis:{mid}", "success")],
        [b("☆ إلغاء الافتراضي", f"def:{mid}", "primary") if is_default else b("⭐ جعله افتراضي", f"def:{mid}", "success")],
        [b("✏️ الاسم", f"en:{mid}", "primary"), b("📝 البرومبت", f"ep:{mid}", "primary")],
        [b("📄 عرض", f"show:{mid}")],
        [b("🗑 حذف", f"del:{mid}", "danger")],
        [b("⬅️ رجوع", "list")],
    )
    return text, markup

async def edit(q, text: str, markup=None) -> None:
    try: await q.edit_message_text(text, reply_markup=markup)
    except BadRequest as e:
        if "not modified" not in str(e).lower(): raise

class GeminiError(Exception):
    def __init__(self, msg: str, fatal: bool = False):
        super().__init__(msg); self.fatal = fatal

async def call_gemini(model: str, system_prompt: str, contents: list, timeout: float = 30.0, json_mode: bool = False) -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    gen = {"temperature": 0.8, "maxOutputTokens": 4096}
    if json_mode: gen = {"temperature": 0.1, "maxOutputTokens": 2048, "responseMimeType": "application/json"}
    payload = {"contents": contents, "generationConfig": gen}
    if system_prompt: payload["system_instruction"] = {"parts": [{"text": system_prompt}]}
    headers = {"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY}
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            r = await client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as e: raise GeminiError(f"تعذر الاتصال ({type(e).__name__})")
    if r.status_code != 200:
        try: msg = r.json()["error"]["message"]
        except Exception: msg = r.text[:200]
        hints = {400: "طلب غير صحيح", 401: "مفتاح غير صالح", 403: "لا صلاحية", 404: "موديل غير موجود", 429: "تجاوز الحد", 503: "ضغط عالٍ"}
        raise GeminiError(f"خطأ ({r.status_code}): {hints.get(r.status_code, '')}\n{msg[:300]}", fatal=r.status_code in (400, 401, 403))
    data = r.json()
    cands = data.get("candidates") or []
    if not cands: raise GeminiError("لم يصل رد", fatal=True)
    parts = cands[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
    if not text: raise GeminiError("رد فارغ")
    return text

async def ask_gemini(system_prompt: str, contents: list, smart: bool = False, json_mode: bool = False):
    plan = [(SMART_MODEL, 45.0), (GEMINI_MODEL, 30.0)] if smart else [(GEMINI_MODEL, 30.0), (GEMINI_MODEL, 30.0)]
    if FALLBACK_MODEL and FALLBACK_MODEL not in [m for m, _ in plan]: plan.append((FALLBACK_MODEL, 30.0))
    last = None
    for i, (model, timeout) in enumerate(plan):
        try: return await call_gemini(model, system_prompt, contents, timeout, json_mode), model
        except GeminiError as e:
            if e.fatal and model != SMART_MODEL: raise
            last = e
            if i < len(plan) - 1: await asyncio.sleep(1.5)
    raise last

def clean(t: str) -> str:
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t, flags=re.S)
    t = re.sub(r"^\s*[\*\-]\s+", "• ", t, flags=re.M)
    return re.sub(r"^#{1,6}\s*", "", t, flags=re.M)

def chunks(t: str, n: int = 3900):
    while len(t) > n:
        cut = t.rfind("\n", 0, n)
        if cut < n // 2: cut = n
        yield t[:cut]; t = t[cut:].lstrip("\n")
    if t.strip(): yield t

async def respond(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int, text: str) -> None:
    bot = context.bot
    admin = is_admin(user_id)
    if not DB["enabled"] and not admin:
        await bot.send_message(chat_id, "⏸ متوقف."); return
    p = prefs(chat_id)
    if p.get("lib"):
        if lib_available(admin):
            await lib_handle(context, chat_id, admin, text); return
        p["lib"] = False; save_data()
    mid = resolve_mode(chat_id, admin)
    if not mid:
        context.user_data["pending"] = text
        t, m = picker_view(chat_id, admin, "🎭 اختر المود أولاً:")
        await bot.send_message(chat_id, t, reply_markup=m); return
    mode = DB["modes"][mid]
    smart = DB["smart_enabled"] and p["smart"]
    lock = LOCKS.setdefault(chat_id, asyncio.Lock())
    async with lock:
        h = HISTORY.get(chat_id)
        if not h or h["mode"] != mid or h["prompt"] != mode["prompt"]: h = HISTORY[chat_id] = {"mode": mid, "prompt": mode["prompt"], "msgs": []}
        contents = h["msgs"] + [{"role": "user", "parts": [{"text": text}]}]
        await bot.send_chat_action(chat_id, ChatAction.TYPING)
        try: reply, used = await ask_gemini(mode["prompt"], contents, smart)
        except GeminiError as e:
            await bot.send_message(chat_id, f"❌ {e}" if admin else "⚠️ خطأ مؤقت.", reply_markup=reply_kb(chat_id)); return
        msgs = (contents + [{"role": "model", "parts": [{"text": reply}]}])[-MAX_HISTORY:]
        while msgs and msgs[0]["role"] != "user": msgs.pop(0)
        h["msgs"] = msgs
    parts = list(chunks(clean(reply)))
    for i, part in enumerate(parts):
        last = i == len(parts) - 1
        await bot.send_message(chat_id, part, reply_markup=reply_kb(chat_id) if last else None)

LIB_PARSE_PROMPT = """أنت محلل طلبات لمكتبة وسائط.
حلل رسالة المستخدم وأعد JSON فقط:
{"intent":"search","query":"عبارة قصيرة","terms":[["كلمة","مرادف"],["كلمة2"]],"media":"any","numbers":[],"want_all":false}

القواعد:
- terms: مفاهيم البحث. **مهم جداً:** لا تضف أي كلمة لم يذكرها المستخدم صراحة في رسالته (مثل إضافة "سورة" إذا لم يقلها، أو "كتاب" إذا لم يقلها). استخرج الكلمات كما هي فقط.
  مثال: «#كتاب_الفتن» => [["#كتاب_الفتن"]]
  مثال: «سورة البقرة» => [["سورة","surah"],["البقرة","baqarah"]]
- media: audio، video، photo، document، text، وإلا any.
- want_all=true فقط إذا طلب صراحة إرسال كل النتائج.
"""

_STOP = {"ارسل", "ارسلي", "اعطني", "عطني", "ابغي", "ابي", "اريد", "هات", "ممكن", "لو", "سمحت", "من", "في", "عن", "علي", "الي", "هل", "يوجد", "عندك", "عندكم", "لي", "لنا"}

def fallback_terms(text: str) -> list:
    return [[w] for w in library.normalize(text).split() if w not in _STOP and len(w) >= 2][:5]

async def lib_understand(text: str, st) -> dict:
    plan = {"intent": "search", "query": text[:40], "terms": [], "media": "any", "numbers": [], "want_all": False}
    if st and re.fullmatch(r"\d{1,3}", library.normalize(text)):
        plan.update(intent="select", numbers=[int(library.normalize(text))]); return plan
    ctx = f"قائمة سابقة: {'نعم' if st else 'لا'}\nرسالة: {text}"
    try:
        raw, _ = await ask_gemini(LIB_PARSE_PROMPT, [{"role": "user", "parts": [{"text": ctx}]}], json_mode=True)
        raw = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
        data = json.loads(raw)
        if isinstance(data, list) and data: data = data[0]
        if not isinstance(data, dict): raise ValueError
    except Exception as e:
        log.warning("lib_understand fallback: %s", e)
        plan["terms"] = fallback_terms(text); return plan
    if data.get("intent") in ("search", "select", "send_all", "more", "other"): plan["intent"] = data["intent"]
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

def lib_folders_view(chat_id):
    L = DB["lib"]
    rows = [[b("🔍 البحث في جميع المصادر", "libsearchall", "success")]]
    folder_btns = []
    for fid, fname in L["folders"].items():
        folder_btns.append(b(f"📁 {fname}", f"libfolder:{fid}", "primary"))
    for i in range(0, len(folder_btns), 2): rows.append(folder_btns[i:i+2])
    rows.append([b("🎭 العودة للمحادثة", "chg", "danger")])
    return "📚 **المكتبة**\nاختر تصنيفاً أو ابحث في الكل:", kb(*rows)

def lib_intro_text(folder_name="الكل") -> str:
    return f"📚 {DB['lib']['name']} ({folder_name})\n\nاكتب ما تبحث عنه (صوتيات، فيديو، صور، ملفات…)\nمثال: «كتاب الفتن» أو «سورة البقرة»."

async def lib_handle(context, chat_id: int, admin: bool, text: str) -> None:
    bot = context.bot
    st = LIB_STATE.get(chat_id)
    if st and time.time() - st["ts"] > 3600:
        LIB_STATE.pop(chat_id, None); st = None
    await bot.send_chat_action(chat_id, ChatAction.TYPING)
    plan = await lib_understand(text, st)
    intent = plan["intent"]
    back = kb([b("🎭 العودة للمحادثة", "chg", "danger")])

    if intent == "other":
        await bot.send_message(chat_id, lib_intro_text(), reply_markup=back); return
    if st and intent == "select":
        nums = [n for n in plan["numbers"] if 1 <= n <= len(st["ids"])]
        if not nums:
            await bot.send_message(chat_id, f"⚠️ اختر رقماً من 1 إلى {len(st['ids'])}."); return
        await lib_send_many(context, chat_id, [st["ids"][n - 1] for n in nums]); return
    if st and intent == "send_all":
        await lib_send_many(context, chat_id, st["ids"][:LIB_SEND_LIMIT], total=len(st["ids"])); return
    if st and intent == "more":
        t, m = lib_list_view(chat_id, st["page"] + 1); await bot.send_message(chat_id, t, reply_markup=m); return

    allowed_chats = st.get("chats") if st else None
    rows = library.search(plan["terms"], plan["media"], allowed_chats=allowed_chats)
    query = plan["query"]
    
    if not rows:
        await bot.send_message(chat_id, f"😕 لم أجد شيئاً عن «{query}».\nجرّب كلمات أخرى.", reply_markup=back); return

    n = len(rows)
    if n == 1:
        await bot.send_message(chat_id, f"✅ وجدت ملفاً واحداً:\n{library.label(rows[0])}\n⏳ جاري الإرسال…")
        ok = await deliver(context, chat_id, rows[0]["id"])
        await bot.send_message(chat_id, "✅ تم الإرسال." if ok else "⚠️ تعذر إرسال الملف.", reply_markup=back); return

    LIB_STATE[chat_id] = {"ids": [r["id"] for r in rows], "query": query, "summary": library.summarize(rows), "page": 0, "ts": time.time(), "chats": allowed_chats}
    if plan["want_all"] and n <= LIB_SEND_LIMIT:
        await bot.send_message(chat_id, f"🔎 وجدت {n} نتيجة عن «{query}».")
        await lib_send_many(context, chat_id, LIB_STATE[chat_id]["ids"]); return
    t, m = lib_list_view(chat_id, 0)
    await bot.send_message(chat_id, t, reply_markup=m)

def lib_list_view(chat_id: int, page: int):
    st = LIB_STATE[chat_id]
    ids = st["ids"]; n = len(ids)
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
    rows.append([b("🎭 العودة للمحادثة", "chg", "danger")])
    return kb(*rows)

# ---------- دالة الإرسال المحسنة بمجموعة العبور ----------
async def deliver(context, chat_id: int, rid: int) -> bool:
    bot = context.bot
    it = library.get(rid)
    if not it: return False
    src = DB["lib"]["sources"].get(str(it["chat_id"]))
    if not src: return False

    # 1. طريقة مجموعة العبور (الأفضل)
    if RELAY_CHAT_ID != 0 and src.get("session") and userbot.configured():
        try:
            relay_msg_id = await userbot.send_to_relay(int(it["chat_id"]), src.get("access_hash"), it["msg_id"], RELAY_CHAT_ID)
            if relay_msg_id:
                await bot.copy_message(chat_id=chat_id, from_chat_id=RELAY_CHAT_ID, message_id=relay_msg_id)
                try: await bot.delete_message(chat_id=RELAY_CHAT_ID, message_id=relay_msg_id)
                except: pass
                return True
        except Exception as e: log.warning("Relay group failed: %s", e)

    # 2. الحساب يرسل مباشرة للمستخدم (بديل)
    if src.get("session") and userbot.configured():
        try:
            if await userbot.send_file_to_user(int(it["chat_id"]), src.get("access_hash"), it["msg_id"], chat_id): return True
        except Exception as e: log.warning("Userbot send failed: %s", e)

    # 3. البوت ينسخ مباشرة (بديل)
    if src.get("bot"):
        try:
            await bot.copy_message(chat_id, from_chat_id=it["chat_id"], message_id=it["msg_id"]); return True
        except Exception as e: log.warning("Bot copy failed: %s", e)

    # 4. رابط (بديل أخير)
    link = library.link_for(src, it)
    if link:
        await bot.send_message(chat_id, f"🔗 {library.label(it)}\n{link}"); return True
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

async def on_any_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg: return
    if str(msg.chat_id) not in DB["lib"]["sources"]: return
    try:
        it = library.item_from_ptb(msg, include_text=(msg.chat.type == "channel"))
        if it: library.upsert([it])
    except Exception: log.exception("index failed")

async def sync_source(bot, cid: str, notify_id: int | None = None) -> int:
    src = DB["lib"]["sources"].get(cid)
    if not src or not src.get("session") or not userbot.configured(): return 0
    total = 0
    def on_batch(batch):
        nonlocal total
        total += library.upsert(batch)
    try: await userbot.fetch(int(cid), src.get("access_hash"), library.max_msg_id(int(cid)), on_batch)
    except Exception as e:
        if notify_id: await bot.send_message(notify_id, f"❌ فشلت مزامنة «{src['title']}»:\n{type(e).__name__}: {e}")
        return total
    src["last_sync"] = int(time.time()); save_data()
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

def lib_admin_view():
    L = DB["lib"]
    sess = "✅ مُهيّأ" if userbot.configured() else "⚪ غير مُهيّأ"
    relay = "✅ مُفعلة" if RELAY_CHAT_ID != 0 else "❌ غير مُفعلة"
    text = f"📚 إدارة المكتبة\n\nالزر: «{L['name']}» — {'👁 ظاهر' if L['visible'] else '🚫 مخفي'}\n📂 المصادر: {len(L['sources'])}\n📁 المجلدات: {len(L['folders'])}\n🗂 العناصر: {library.count()}\n👤 الحساب: {sess}\n🚀 مجموعة العبور: {relay}"
    rows = [
        [b("🚫 إخفاء", "lavis", "danger") if L["visible"] else b("👁 إظهار", "lavis", "success")],
        [b("✏️ تسمية", "laname", "primary"), b("📁 المجلدات", "lafolders", "primary")],
        [b("➕ إضافة مصدر", "laadd", "success"), b("📂 المصادر", "lasrc", "primary")],
    ]
    if userbot.configured(): rows.append([b("🔄 مزامنة الكل", "lasync:all", "primary"), b("🧪 فحص", "lacheck")])
    rows.append([b("🏠 الرئيسية", "home")])
    return text, kb(*rows)

def lib_folders_admin_view():
    L = DB["lib"]
    rows = []
    for fid, fname in L["folders"].items():
        rows.append([b(f"📁 {fname}", f"lafol:{fid}", "primary")])
    rows.append([b("➕ إضافة مجلد", "lafoldadd", "success"), b("⬅️ رجوع", "la")])
    return "📁 إدارة المجلدات:", kb(*rows)

def lib_folder_view(fid):
    L = DB["lib"]
    if fid not in L["folders"]: return "⚠️ غير موجود.", kb([b("⬅️ رجوع", "lafolders")])
    fname = L["folders"][fid]
    rows = [
        [b("✏️ تعديل الاسم", f"lafolename:{fid}", "primary")],
        [b("🗑 حذف المجلد (لا يحذف المصادر)", f"lafoldel:{fid}", "danger")],
        [b("⬅️ رجوع", "lafolders")]
    ]
    return f"📁 المجلد: {fname}", kb(*rows)

def lib_sources_view():
    srcs = DB["lib"]["sources"]
    if not srcs: return "📭 لا توجد مصادر.", kb([b("➕ إضافة", "laadd", "success")], [b("⬅️ رجوع", "la")])
    L = DB["lib"]
    rows = []
    for cid, s in srcs.items():
        flags = ("🤖" if s.get("bot") else "") + ("👤" if s.get("session") else "")
        folder_name = L["folders"].get(s.get("folder"), "بدون مجلد")
        rows.append([b(f"{flags} {s['title']} [{folder_name}]", f"las:{cid}", "primary")])
    rows.append([b("➕ إضافة", "laadd", "success"), b("⬅️ رجوع", "la")])
    return "📂 المصادر:", kb(*rows)

def lib_source_view(cid: str, notice: str = ""):
    s = DB["lib"]["sources"][cid]
    L = DB["lib"]
    last = time.strftime("%Y-%m-%d %H:%M", time.localtime(s["last_sync"])) if s.get("last_sync") else "—"
    folder_name = L["folders"].get(s.get("folder"), "بدون مجلد")
    text = (notice + "\n\n" if notice else "") + f"📂 {s['title']}\n📁 المجلد: {folder_name}\n🆔 {cid}\n{'🔗 @' + s['username'] if s.get('username') else '🔒 خاص'}\n🤖 البوت: {'✅' if s.get('bot') else '❌'}\n👤 الحساب: {'✅' if s.get('session') else '❌'}\n🗂 العناصر: {library.count(int(cid))}\nآخر مزامنة: {last}"
    rows = []
    if s.get("session") and userbot.configured(): rows.append([b("🔄 مزامنة الآن", f"lasync:{cid}", "success")])
    rows.append([b("📁 تغيير المجلد", f"lasetfolder:{cid}", "primary")])
    rows.append([b("👤 ربط الحساب", f"lalink:{cid}", "primary")])
    rows.append([b("🗑 إزالة", f"ladel:{cid}", "danger")])
    rows.append([b("⬅️ رجوع", "lasrc")])
    return text, kb(*rows)

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    context.user_data.pop("state", None)
    if not ADMIN_IDS:
        await update.message.reply_text(f"⚠️ ضع رقمك في ADMIN_IDS:\n{uid}"); return
    if is_admin(uid):
        text, markup = home_view(); await update.message.reply_text(text, reply_markup=markup); return
    if not DB["enabled"]: await update.message.reply_text("⏸ متوقف."); return
    
    allowed = allowed_modes(False)
    lib_ok = lib_available(False)
    if not allowed and not lib_ok: await update.message.reply_text("⚠️ لا مودات.")
    elif len(allowed) + (1 if lib_ok else 0) == 1 and allowed:
        resolve_mode(chat_id, False)
        await update.message.reply_text("👋 أهلاً! اكتب رسالتك.")
    else:
        t, m = picker_view(chat_id, False, "👋 أهلاً! اختر المود:")
        await update.message.reply_text(t, reply_markup=m)

async def cmd_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin = is_admin(update.effective_user.id)
    if not DB["enabled"] and not admin: return
    t, m = picker_view(update.effective_chat.id, admin)
    await update.message.reply_text(t, reply_markup=m)

async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    HISTORY.pop(update.effective_chat.id, None); LIB_STATE.pop(update.effective_chat.id, None)
    await update.message.reply_text("🔄 محادثة جديدة.")

async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("state", None); await update.message.reply_text("تم الإلغاء.")

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text: return
    text = update.message.text.strip(); user = update.effective_user; chat = update.effective_chat
    if str(chat.id) in DB["lib"]["sources"]: return
    if is_admin(user.id) and chat.type == "private" and context.user_data.get("state"):
        await handle_admin_input(update, context, text); return
    await respond(context, chat.id, user.id, text)

async def on_nontext(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not is_admin(update.effective_user.id): return
    st = context.user_data.get("state")
    if st and st.get("step") in ("lib_add", "lib_link"): await handle_admin_input(update, context, "")

async def handle_admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    st = context.user_data["state"]; step = st["step"]; reply = update.message.reply_text
    if step == "add_name":
        if len(text) > 40: await reply("⚠️ طويل:", reply_markup=CANCEL_KB); return
        context.user_data["state"] = {"step": "add_prompt", "name": text}
        await reply(f"📝 البرومبت لمود «{text}»:", reply_markup=CANCEL_KB)
    elif step == "add_prompt":
        mid = uuid.uuid4().hex[:8]; DB["modes"][mid] = {"name": st["name"], "prompt": text, "visible": True}; save_data()
        context.user_data.pop("state", None)
        await reply(f"✅ تم الحفظ.", reply_markup=kb([b("⭐ افتراضي", f"def:{mid}", "success")], [b("➕ إضافة", "add", "primary"), b("📋 المودات", "list", "primary")], [b("🏠 الرئيسية", "home")]))
    elif step in ("edit_name", "edit_prompt"):
        mid = st["id"]; context.user_data.pop("state", None)
        if mid not in DB["modes"]: await reply("⚠️ غير موجود."); return
        if step == "edit_name":
            if len(text) > 40: await reply("⚠️ طويل."); return
            DB["modes"][mid]["name"] = text
        else: DB["modes"][mid]["prompt"] = text
        save_data(); await reply(*mode_view(mid, "✅ تم التعديل"))
    elif step == "lib_name":
        if not text or len(text) > 30: await reply("⚠️ 1-30 حرف:", reply_markup=CANCEL_KB); return
        DB["lib"]["name"] = text; save_data(); context.user_data.pop("state", None)
        await reply("✅ تم التغيير.", reply_markup=lib_admin_view()[1])
    elif step == "lib_foldname":
        fid = st.get("fid")
        if not text or len(text) > 30: await reply("⚠️ 1-30 حرف:", reply_markup=CANCEL_KB); return
        if fid == "new":
            fid = uuid.uuid4().hex[:6]
            DB["lib"]["folders"][fid] = text
        else:
            DB["lib"]["folders"][fid] = text
        save_data(); context.user_data.pop("state", None)
        await reply("✅ تم الحفظ.", reply_markup=lib_folders_admin_view()[1])
    elif step in ("lib_add", "lib_link"):
        fwd = forward_chat_id(update.message); ref = "" if fwd else text
        await reply("⏳ جاري الفحص…")
        ok, report = await try_add_source(context.bot, ref, fwd, expect_cid=st.get("cid"), notify_id=update.effective_user.id)
        if ok:
            context.user_data.pop("state", None)
            await reply(report, reply_markup=kb([b("📂 المصادر", "lasrc", "primary")], [b("📚 المكتبة", "la")]))
        else: await reply(report, reply_markup=CANCEL_KB)

USER_CMDS = {"chg", "pick", "smart", "newchat", "closepick", "lib", "libp", "lp", "lpg", "lall", "libsearchall", "libfolder"}

async def on_user_button(update: Update, context: ContextTypes.DEFAULT_TYPE, cmd: str, arg: str):
    q = update.callback_query; chat_id = q.message.chat_id; uid = q.from_user.id; admin = is_admin(uid)

    if cmd == "newchat": HISTORY.pop(chat_id, None); LIB_STATE.pop(chat_id, None); await q.answer("🔄")
    elif cmd == "closepick":
        await q.answer()
        try: await q.message.delete()
        except BadRequest: pass
    elif cmd == "chg":
        if not DB["enabled"] and not admin: return await q.answer("⏸", show_alert=True)
        t, m = picker_view(chat_id, admin)
        if not m: return await q.answer("لا مودات", show_alert=True)
        await q.answer(); await context.bot.send_message(chat_id, t, reply_markup=m)
    elif cmd == "pick":
        mode = allowed_modes(admin).get(arg)
        if not mode: return await q.answer("غير متاح", show_alert=True)
        p = prefs(chat_id); p["mode"] = arg; p["lib"] = False; save_data()
        await q.answer(f"✅ {mode['name']}")
        pending = context.user_data.pop("pending", None)
        if pending: await edit(q, f"✅ «{mode['name']}»…"); await respond(context, chat_id, uid, pending)
        else: await edit(q, f"✅ «{mode['name']}»\nأرسل رسالتك.")
    elif cmd == "smart":
        if not DB["smart_enabled"]: return await q.answer("غير متاح", show_alert=True)
        p = prefs(chat_id); p["smart"] = not p["smart"]; save_data()
        await q.answer("🧠" if p["smart"] else "⚡")
        try: await q.edit_message_reply_markup(reply_markup=reply_kb(chat_id))
        except BadRequest: pass
    
    elif cmd == "lib":
        if not DB["enabled"] and not admin: return await q.answer("⏸", show_alert=True)
        if not lib_available(admin): return await q.answer("غير متاحة", show_alert=True)
        p = prefs(chat_id); p["lib"] = True; save_data()
        LIB_STATE.pop(chat_id, None); await q.answer()
        await context.bot.send_message(chat_id, lib_folders_view(chat_id)[0], reply_markup=lib_folders_view(chat_id)[1])
        
    elif cmd == "libsearchall":
        LIB_STATE[chat_id] = {"chats": None}
        await q.answer()
        await edit(q, lib_intro_text("الكل"), kb([b("🎭 العودة", "chg", "danger")]))
        
    elif cmd == "libfolder":
        fid = arg
        if fid not in DB["lib"]["folders"]: return await q.answer("غير موجود", show_alert=True)
        chats_in_folder = [c for c, s in DB["lib"]["sources"].items() if s.get("folder") == fid]
        LIB_STATE[chat_id] = {"chats": chats_in_folder}
        await q.answer()
        await edit(q, lib_intro_text(DB["lib"]["folders"][fid]), kb([b("🎭 العودة", "chg", "danger")]))

    elif cmd in ("lp", "lpg", "lall"):
        if not lib_available(admin): return await q.answer("غير متاحة", show_alert=True)
        if cmd == "lp":
            if not arg.isdigit(): return await q.answer()
            await q.answer("⏳…")
            if not await deliver(context, chat_id, int(arg)): await context.bot.send_message(chat_id, "⚠️ تعذر الإرسال.")
            return
        st = LIB_STATE.get(chat_id)
        if not st or "ids" not in st: return await q.answer("انتهت الصلاحية", show_alert=True)
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
        await q.answer("⏳…")
        try:
            name = await userbot.check(); await context.bot.send_message(q.message.chat_id, f"✅ متصل: {name}")
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
    
    elif cmd == "la":
        context.user_data.pop("state", None); await edit(q, *lib_admin_view())
    elif cmd == "lavis":
        DB["lib"]["visible"] = not DB["lib"]["visible"]; save_data(); await edit(q, *lib_admin_view())
    elif cmd == "laname":
        context.user_data["state"] = {"step": "lib_name"}; await edit(q, "✏️ اسم الزر الجديد:", CANCEL_KB)
    
    elif cmd == "lafolders":
        context.user_data.pop("state", None); await edit(q, *lib_folders_admin_view())
    elif cmd == "lafoldadd":
        context.user_data["state"] = {"step": "lib_foldname", "fid": "new"}; await edit(q, "📁 اسم المجلد الجديد:", CANCEL_KB)
    elif cmd == "lafol":
        await edit(q, *lib_folder_view(arg))
    elif cmd == "lafolename":
        context.user_data["state"] = {"step": "lib_foldname", "fid": arg}; await edit(q, "✏️ الاسم الجديد:", CANCEL_KB)
    elif cmd == "lafoldel":
        DB["lib"]["folders"].pop(arg, None)
        for s in DB["lib"]["sources"].values():
            if s.get("folder") == arg: s["folder"] = None
        save_data(); await edit(q, *lib_folders_admin_view())

    elif cmd == "laadd":
        context.user_data["state"] = {"step": "lib_add"}; await edit(q, "➕ أرسل @username أو رابط أو حوّل رسالة:", CANCEL_KB)
    elif cmd == "lasrc":
        context.user_data.pop("state", None); await edit(q, *lib_sources_view())
    elif cmd == "lasync":
        if not userbot.configured(): return await context.bot.send_message(admin_chat, "⚠️ غير مهيأ.")
        targets = [arg] if arg != "all" else [c for c, s in DB["lib"]["sources"].items() if s.get("session")]
        if not targets: return await context.bot.send_message(admin_chat, "⚠️ لا مصادر.")
        await context.bot.send_message(admin_chat, "⏳ مزامنة في الخلفية…")
        for cid in targets: spawn(sync_source(context.bot, cid, admin_chat))
    
    elif cmd in ("las", "lalink", "ladel", "ladelok", "lasetfolder"):
        if arg not in DB["lib"]["sources"]: return await edit(q, "⚠️ غير موجود.", kb([b("⬅️", "lasrc")]))
        if cmd == "las": await edit(q, *lib_source_view(arg))
        elif cmd == "lalink":
            if not userbot.configured(): return await edit(q, "⚠️ غير مهيأ.", kb([b("⬅️", f"las:{arg}")]))
            context.user_data["state"] = {"step": "lib_link", "cid": arg}
            await edit(q, "👤 أرسل الرابط ليدخله الحساب:", CANCEL_KB)
        elif cmd == "ladel":
            await edit(q, f"⚠️ إزالة «{DB['lib']['sources'][arg]['title']}»؟", kb([b("🗑 نعم", f"ladelok:{arg}", "danger"), b("↩️", f"las:{arg}", "primary")]))
        elif cmd == "ladelok":
            DB["lib"]["sources"].pop(arg, None); library.delete_chat(int(arg)); save_data(); await edit(q, *lib_sources_view())
        elif cmd == "lasetfolder":
            rows = [[b("بدون مجلد", f"lasetfok:{arg}:0", "danger")]]
            for fid, fname in DB["lib"]["folders"].items():
                rows.append([b(f"📁 {fname}", f"lasetfok:{arg}:{fid}", "primary")])
            rows.append([b("⬅️ رجوع", f"las:{arg}")])
            await edit(q, "اختر المجلد:", kb(*rows))

    elif cmd == "lasetfok":
        parts = arg.split(":")
        cid, fid = parts[0], parts[1] if len(parts) > 1 else "0"
        if cid in DB["lib"]["sources"]:
            DB["lib"]["sources"][cid]["folder"] = None if fid == "0" else fid
            save_data()
            await edit(q, *lib_source_view(cid, "✅ تم تغيير المجلد"))
    elif cmd == "lsrc":
        ok, report = await try_add_source(context.bot, "", int(arg), notify_id=admin_chat)
        await edit(q, report, kb([b("📂 المصادر", "lasrc", "primary")]) if ok else None)
    elif cmd == "lskip":
        await edit(q, "تم التجاهل.")
    
    elif cmd in ("m", "vis", "def", "en", "ep", "show", "del", "delok"):
        if arg not in DB["modes"]: return await edit(q, "⚠️ غير موجود.", kb([b("⬅️", "list")]))
        if cmd == "m": await edit(q, *mode_view(arg))
        elif cmd == "vis":
            m = DB["modes"][arg]; m["visible"] = not m.get("visible", True); save_data()
            await edit(q, *mode_view(arg, "👁" if m["visible"] else "🚫"))
        elif cmd == "def":
            DB["default"] = None if DB.get("default") == arg else arg; save_data()
            await edit(q, *mode_view(arg, "⭐" if DB["default"] == arg else "☆"))
        elif cmd == "en":
            context.user_data["state"] = {"step": "edit_name", "id": arg}; await edit(q, "✏️ الاسم:", CANCEL_KB)
        elif cmd == "ep":
            context.user_data["state"] = {"step": "edit_prompt", "id": arg}; await edit(q, "📝 البرومبت:", CANCEL_KB)
        elif cmd == "show":
            for part in chunks(DB["modes"][arg]["prompt"]): await q.message.reply_text(part)
        elif cmd == "del":
            await edit(q, f"⚠️ حذف «{DB['modes'][arg]['name']}»؟", kb([b("🗑", f"delok:{arg}", "danger"), b("↩️", f"m:{arg}", "primary")]))
        elif cmd == "delok":
            DB["modes"].pop(arg, None)
            if DB.get("default") == arg: DB["default"] = None
            save_data(); await edit(q, *list_view())

async def post_init(app: Application):
    await app.bot.set_my_commands([BotCommand("start", "بدء"), BotCommand("mode", "تغيير المود"), BotCommand("new", "محادثة جديدة"), BotCommand("id", "رقمي")])
    if LIB_SYNC_MINUTES > 0: spawn(sync_loop(app))

def main():
    if not TELEGRAM_TOKEN or not GEMINI_API_KEY: raise SystemExit("❌ ضع TELEGRAM_TOKEN و GEMINI_API_KEY")
    app = (Application.builder().token(TELEGRAM_TOKEN).concurrent_updates(True).post_init(post_init).build())
    app.add_handler(MessageHandler(filters.ALL, on_any_update), group=-1)
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(CommandHandler("start", cmd_start)); app.add_handler(CommandHandler("mode", cmd_mode))
    app.add_handler(CommandHandler("new", cmd_new)); app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.TEXT & ~filters.COMMAND, on_nontext))
    log.info("Bot v3 started. Relay: %s | Session: %s", "on" if RELAY_CHAT_ID != 0 else "off", "on" if userbot.configured() else "off")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
