# -*- coding: utf-8 -*-
"""
ربط حساب حقيقي (Telethon) للانضمام إلى القنوات/المجموعات وقراءة محتواها القديم.

المتغيرات:
  TG_API_ID, TG_API_HASH : من my.telegram.org  (API development tools)
  TG_SESSION             : نص الجلسة StringSession (يُنشأ بالسكربت make_session.py)
  TG_SESSION_FILE        : بديل: مسار ملف .session (بدون الامتداد) — لا تضعه في GitHub أبداً

ملاحظة أمان: الجلسة = صلاحية كاملة على الحساب. استخدم حساباً ثانوياً، ولا تشاركها مع أحد.
الاتصال هنا «عند الحاجة فقط» (يتصل ثم يقطع) لتقليل خطر تعارض النسختين وقت إعادة النشر.
"""
import asyncio
import logging
import os
import re

import library

log = logging.getLogger("userbot")

API_ID = os.environ.get("TG_API_ID", "").strip()
API_HASH = os.environ.get("TG_API_HASH", "").strip()
SESSION_STRING = os.environ.get("TG_SESSION", "").strip()
SESSION_FILE = os.environ.get("TG_SESSION_FILE", "").strip()

try:
    from telethon import TelegramClient, utils
    from telethon.errors import UserAlreadyParticipantError
    from telethon.sessions import StringSession
    from telethon.tl.functions.channels import JoinChannelRequest
    from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
    from telethon.tl.types import InputPeerChannel, InputPeerChat, PeerChannel, PeerChat

    HAVE_TELETHON = True
except Exception:  # غير مثبتة
    HAVE_TELETHON = False


class UserbotError(Exception):
    pass


def configured() -> bool:
    return bool(HAVE_TELETHON and API_ID.isdigit() and API_HASH and (SESSION_STRING or SESSION_FILE))


def why_not() -> str:
    if not HAVE_TELETHON:
        return "مكتبة telethon غير مثبتة (أضفها في requirements.txt)"
    if not (API_ID.isdigit() and API_HASH):
        return "ضع TG_API_ID و TG_API_HASH"
    if not (SESSION_STRING or SESSION_FILE):
        return "ضع TG_SESSION (أنشئه بالسكربت make_session.py)"
    return ""


# ============================ الجلسة عند الحاجة ============================
_lock = asyncio.Lock()


class session:
    """async with session() as client: ..."""

    async def __aenter__(self):
        if not configured():
            raise UserbotError(why_not())
        await _lock.acquire()
        try:
            src = StringSession(SESSION_STRING) if SESSION_STRING else SESSION_FILE
            client = TelegramClient(src, int(API_ID), API_HASH, receive_updates=False)
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                raise UserbotError("الجلسة غير مسجلة الدخول أو انتهت — أنشئ TG_SESSION من جديد")
            self.client = client
            return client
        except BaseException:
            _lock.release()
            raise

    async def __aexit__(self, *exc):
        try:
            await self.client.disconnect()
        finally:
            _lock.release()


# ============================ أدوات ============================
def parse_ref(ref: str):
    """يرجع ('invite', hash) أو ('user', username) أو (None, None)"""
    ref = (ref or "").strip()
    m = re.search(r"(?:t\.me/\+|t\.me/joinchat/|tg://join\?invite=)([\w-]+)", ref)
    if m:
        return "invite", m.group(1)
    m = re.search(r"(?:t\.me/|telegram\.me/)([A-Za-z]\w{3,})", ref)
    if m and m.group(1) not in ("joinchat",):
        return "user", m.group(1)
    m = re.fullmatch(r"@([A-Za-z]\w{3,})", ref)  # الاسم المجرد يتطلب @ حتى لا تُفسَّر أي كلمة كاسم مستخدم
    if m:
        return "user", m.group(1)
    return None, None


def peer_for(chat_id: int, access_hash):
    real_id, kind = utils.resolve_id(int(chat_id))
    if kind is PeerChannel:
        if not access_hash:
            raise UserbotError("لا يوجد access_hash لهذا المصدر — اربط الحساب به من جديد")
        return InputPeerChannel(real_id, int(access_hash))
    if kind is PeerChat:
        return InputPeerChat(real_id)
    raise UserbotError("نوع مصدر غير مدعوم")


def item_from_tl(m, chat_id: int):
    f = m.file
    date = int(m.date.timestamp()) if m.date else 0
    cap = m.message or None
    if m.voice:
        kind = "voice"
    elif m.audio:
        kind = "audio"
    elif m.video_note or m.video:
        kind = "video"
    elif m.gif:
        kind = "animation"
    elif m.photo:
        kind = "photo"
    elif m.sticker:
        return None
    elif m.document:
        kind = "document"
    elif m.message:
        return library.make_item(chat_id, m.id, "text", text=m.message, date=date)
    else:
        return None
    return library.make_item(
        chat_id, m.id, kind,
        title=getattr(f, "title", None), performer=getattr(f, "performer", None),
        filename=getattr(f, "name", None), ext=getattr(f, "ext", None),
        caption=cap, duration=getattr(f, "duration", None), size=getattr(f, "size", None), date=date,
    )


# ============================ العمليات ============================
async def check() -> str:
    async with session() as c:
        me = await c.get_me()
        name = " ".join(x for x in (me.first_name, me.last_name) if x)
        return name + (f" (@{me.username})" if me.username else "")


async def resolve_and_join(ref: str):
    """ينضم للمصدر ويرجع (chat_id, title, username, access_hash)"""
    kind, val = parse_ref(ref)
    if not kind:
        raise UserbotError("أرسل @username أو رابط القناة أو رابط دعوة (t.me/+…)")
    async with session() as c:
        if kind == "invite":
            try:
                upd = await c(ImportChatInviteRequest(val))
                chat = upd.chats[0]
            except UserAlreadyParticipantError:
                info = await c(CheckChatInviteRequest(val))
                chat = getattr(info, "chat", None)
                if chat is None:
                    raise UserbotError("الحساب منضم مسبقاً لكن تعذر جلب بيانات المصدر")
        else:
            chat = await c.get_entity(val)
            try:
                await c(JoinChannelRequest(chat))
            except Exception as e:  # منضم مسبقاً أو مجموعة عادية
                log.info("join skipped: %s", e)
        return (
            utils.get_peer_id(chat),
            utils.get_display_name(chat),
            getattr(chat, "username", None),
            getattr(chat, "access_hash", None),
        )


async def fetch(chat_id: int, access_hash, min_id: int, on_batch, batch_size: int = 200) -> None:
    """يجلب الرسائل الأحدث من min_id ويفهرسها دفعات عبر on_batch(list)"""
    async with session() as c:
        peer = peer_for(chat_id, access_hash)
        batch = []
        async for m in c.iter_messages(peer, min_id=min_id, wait_time=1):
            it = item_from_tl(m, chat_id)
            if it:
                batch.append(it)
            if len(batch) >= batch_size:
                on_batch(batch)
                batch = []
        if batch:
            on_batch(batch)


async def download(chat_id: int, access_hash, msg_id: int) -> bytes:
    async with session() as c:
        peer = peer_for(chat_id, access_hash)
        m = await c.get_messages(peer, ids=msg_id)
        if not m or not m.media:
            raise UserbotError("الرسالة غير موجودة أو بلا ملف")
        data = await c.download_media(m, file=bytes)
        if not data:
            raise UserbotError("فشل تحميل الملف")
        return data


# ============================ مجموعة التحويل ============================
_relay_peer = {}


async def _relay_entity(c, relay_id: int):
    if relay_id in _relay_peer:
        return _relay_peer[relay_id]
    try:
        ent = await c.get_input_entity(int(relay_id))
    except ValueError:
        await c.get_dialogs(limit=300)  # الجلسة لا تحفظ الكيانات: نحمّل المحادثات مرة
        try:
            ent = await c.get_input_entity(int(relay_id))
        except ValueError:
            raise UserbotError("الحساب ليس عضواً في مجموعة التحويل")
    _relay_peer[relay_id] = ent
    return ent


async def forward_to_relay(chat_id: int, access_hash, msg_ids: list, relay_id: int) -> list:
    """يحوّل الرسائل دفعة واحدة إلى المجموعة، ويرجع أرقامها الجديدة (None للفاشل)"""
    async with session() as c:
        peer = peer_for(chat_id, access_hash)
        dest = await _relay_entity(c, relay_id)
        res = await c.forward_messages(dest, list(msg_ids), from_peer=peer)
        if not isinstance(res, list):
            res = [res]
        return [getattr(m, "id", None) if m else None for m in res]


async def check_relay(relay_id: int) -> str:
    async with session() as c:
        ent = await c.get_entity(await _relay_entity(c, relay_id))
        return utils.get_display_name(ent)


async def send_test(relay_id: int) -> int:
    """يرسل الحساب رسالة تجربة؛ يرجع رقمها ليحذفها البوت (يثبت أن البوت يصل لرسائل الحساب)"""
    async with session() as c:
        m = await c.send_message(await _relay_entity(c, relay_id), "🧪 فحص اتصال")
        return m.id
