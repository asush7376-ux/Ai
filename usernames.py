# -*- coding: utf-8 -*-
"""
usernames.py — صيد يوزرات تيليجرام القصيرة (4 أو 5 أحرف)

يفحص التوفر عبر الدالة الرسمية account.checkUsername باستخدام حساب Telethon (Session):
  ✅ free      متاح للتسجيل مباشرة (عادةً 5 أحرف)
  💎 fragment  معروض للشراء على fragment.com (USERNAME_PURCHASE_AVAILABLE)
  ⛔ taken     مأخوذ     ⛔ invalid  غير متاح (محجوز/غير قابل للتسجيل)

يدعم الأسماء بشرطة سفلية مثل rq_54 (نمط us، أو نمط مخصص مثل rq_##).
قواعد الاسم: يبدأ بحرف، أحرف a-z وأرقام و _ فقط، لا ينتهي بـ _ ولا يحتوي __

الحماية:
  • تأخير عشوائي 1.5–2.6 ثانية بين كل فحص (لتفادي FloodWait)
  • إيقاف تلقائي إذا طلب تيليجرام انتظاراً أطول من الحد، أو عند أخطاء متتالية
  • ذاكرة مؤقتة: لا يعيد فحص الأسماء المأخوذة قبل 7 أيام، فتتقدم كل جولة في أسماء جديدة

المتغيرات الاختيارية: USERNAMES_CACHE (مسار ملف الذاكرة، الافتراضي usernames_cache.json)
"""
import asyncio
import itertools
import json
import logging
import os
import random
import re
import string
import time

import userbot

log = logging.getLogger("usernames")

CACHE_FILE = os.environ.get("USERNAMES_CACHE", "usernames_cache.json")
FREE, FRAG, TAKEN, INVALID, ERROR = "free", "fragment", "taken", "invalid", "error"
TTL = {TAKEN: 7 * 86400, INVALID: 30 * 86400, FRAG: 12 * 3600}  # مدة تجاهل إعادة الفحص
LETTERS = string.ascii_lowercase
CONS = "bcdfghjklmnprstvwxyz"
VOWELS = "aeiou"
_NAME_RE = re.compile(r"^[a-z](?:[a-z0-9]|_(?!_))*[a-z0-9]$")  # يبدأ بحرف ولا ينتهي بـ _ ولا __

# ============================ الأنماط الجميلة ============================
TEMPLATES = {
    4: ["aaaa", "abab", "abba", "aabb", "aaab", "abbb", "aaba", "abaa"],
    5: ["aaaaa", "ababa", "abbba", "abcba", "aabaa", "abbbb", "aaaab", "aabbb", "aaabb", "abaab"],
}

WORDS = """
king lion wolf gold fire ruby jade star moon love hero dark luna nova zeus odin thor ares loki rain snow wind rock
sand wave lake blue pink mint rose lily iris sage fast hawk bear deer puma lynx orca crow dove swan hunt real pure
true zero bold cool epic byte code data game play tech news chat talk bank cash coin gems vibe mood soul free best
rich boss chef kiwi lime plum pear leaf tree wild fury rage everever apex flux grid halo hype icon jazz kick lava
omar amir zain nour noor saif rami adam yara lina dana sara hana maya nada raed majd fahd sami jabr rayan
queen royal magic tiger eagle ninja pearl black white green ocean storm flame light night ghost angel devil lucky
super ultra prime elite quick smart alpha omega delta sigma hyper turbo rapid cyber pixel robot atlas titan cobra
viper panda zebra camel horse shark whale dream happy smile heart peace honor glory power force brave noble sunny
cloud river stone steel ivory amber coral ebony jewel crown sword arrow blade money trade forex token block chain
vault coins music video photo voice radio party drive speed sport games malik habib majed jamal kamal wasim yazan
ziyad sadiq faris tariq basil nabil hadi walid anwar
""".split()


# أسماء بشرطة سفلية مثل rq_54  (؟ = حرف، # = رقم) مع أوزان لكثرة الاستخدام
US_TEMPLATES = {
    4: [("?_##", 3), ("??_#", 3), ("?_??", 1), ("??_?", 1)],
    5: [("??_##", 5), ("?_###", 2), ("???_#", 2), ("??_??", 1)],
}


def gen_us(lengths, rnd=None):
    rnd = rnd or random
    seen = set()
    for _ in range(300_000):
        n = rnd.choice(sorted(lengths))
        tpls, weights = zip(*US_TEMPLATES[n])
        tpl = rnd.choices(tpls, weights)[0]
        name = "".join(rnd.choice(LETTERS) if ch == "?" else rnd.choice("0123456789") if ch == "#" else ch
                       for ch in tpl)
        if name not in seen:
            seen.add(name)
            yield name


def gen_rep(lengths):
    for n in sorted(lengths):
        for tpl in TEMPLATES.get(n, []):
            order = sorted(set(tpl), key=tpl.index)
            for perm in itertools.permutations(LETTERS, len(order)):
                m = dict(zip(order, perm))
                yield "".join(m[ch] for ch in tpl)


def gen_cv(lengths, rnd=None):
    rnd = rnd or random
    patterns = {4: "cvcv", 5: "cvcvc"}
    seen = set()
    for _ in range(200_000):
        n = rnd.choice(sorted(lengths))
        name = "".join(rnd.choice(CONS if ch == "c" else VOWELS) for ch in patterns[n])
        if name not in seen:
            seen.add(name)
            yield name


def parse_pattern(p: str):
    """? = أي حرف، # = أي رقم. مثال: ab?? أو ?a?e? أو zz#"""
    p = (p or "").strip().lower()
    return p if re.fullmatch(r"[a-z0-9_?#]{4,5}", p) else None


def gen_pattern(p: str):
    pools = [LETTERS if ch == "?" else "0123456789" if ch == "#" else ch for ch in p]
    for combo in itertools.product(*pools):
        yield "".join(combo)


def parse_list(text: str):
    for w in re.split(r"[\s,،;]+", (text or "").lower()):
        w = w.strip().lstrip("@")
        if w:
            yield w


def valid(name: str, lengths) -> bool:
    return len(name) in lengths and bool(_NAME_RE.fullmatch(name))


# ============================ الذاكرة المؤقتة ============================
_cache: dict = {}
_loaded = False


def load_cache() -> None:
    global _cache, _loaded
    if _loaded:
        return
    _loaded = True
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            _cache = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        _cache = {}


def save_cache() -> None:
    now = time.time()
    keep = {n: v for n, v in _cache.items() if v[0] == FREE or now - v[1] < TTL.get(v[0], 0)}
    try:
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(keep, f)
        os.replace(tmp, CACHE_FILE)
    except OSError as e:
        log.warning("cache save failed: %s", e)


def is_fresh(name: str) -> bool:
    v = _cache.get(name)
    return bool(v) and v[0] in TTL and time.time() - v[1] < TTL[v[0]]


def remember(name: str, status: str) -> None:
    if status != ERROR:
        _cache[name] = [status, time.time()]


# ============================ بناء قائمة المرشحين ============================
def build(mode: str, lengths, limit: int, payload=None) -> list:
    """يرجع حتى limit اسماً جديداً. mode: rep | us | cv | words | pat | mine"""
    load_cache()
    lengths = set(lengths)
    skip_fresh = True
    if mode == "rep":
        gen = gen_rep(lengths)
    elif mode == "cv":
        gen = gen_cv(lengths)
    elif mode == "us":
        gen = gen_us(lengths)
    elif mode == "words":
        gen = iter(WORDS)
    elif mode == "pat":
        p = parse_pattern(payload)
        if not p:
            raise ValueError("نمط غير صالح — استخدم 4 أو 5 رموز من a-z و 0-9 و _ و ? (أي حرف) و # (أي رقم)")
        gen, lengths, skip_fresh = gen_pattern(p), {len(p)}, False
    elif mode == "mine":
        gen, lengths, skip_fresh = parse_list(payload), {4, 5}, False
    else:
        raise ValueError("نمط غير معروف")

    out, seen = [], set()
    for name in gen:
        if name in seen or not valid(name, lengths):
            continue
        seen.add(name)
        if skip_fresh and is_fresh(name):
            continue
        out.append(name)
        if len(out) >= limit:
            break
    return out


# ============================ الفحص ============================
def classify(e: Exception) -> str:
    s = f"{type(e).__name__} {getattr(e, 'message', '')} {e}".upper().replace(" ", "").replace("_", "")
    if "PURCHASE" in s:
        return FRAG
    if "OCCUPIED" in s:
        return TAKEN
    if "INVALID" in s:
        return INVALID
    return ERROR


async def check_one(client, name: str):
    """يرجع (الحالة، ثواني الانتظار إن فرض تيليجرام FloodWait طويلاً)"""
    from telethon.tl.functions.account import CheckUsernameRequest

    try:
        ok = await client(CheckUsernameRequest(name))
        return (FREE if ok else TAKEN), 0
    except Exception as e:  # noqa: BLE001
        sec = getattr(e, "seconds", None)
        if sec and type(e).__name__.startswith("FloodWait"):
            return ERROR, int(sec)
        return classify(e), 0


class _OwnSession:
    """بديل إن لم يوفر userbot.py الدالة session()"""
    _lock = None

    async def __aenter__(self):
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        if _OwnSession._lock is None:
            _OwnSession._lock = asyncio.Lock()
        await _OwnSession._lock.acquire()
        try:
            ss = os.environ.get("TG_SESSION", "").strip()
            sf = os.environ.get("TG_SESSION_FILE", "").strip()
            client = TelegramClient(StringSession(ss) if ss else sf, int(os.environ["TG_API_ID"]),
                                    os.environ["TG_API_HASH"], receive_updates=False)
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                raise RuntimeError("الجلسة غير مسجلة الدخول — أنشئ TG_SESSION من جديد")
            self.client = client
            return client
        except BaseException:
            _OwnSession._lock.release()
            raise

    async def __aexit__(self, *exc):
        try:
            await self.client.disconnect()
        finally:
            _OwnSession._lock.release()


def session():
    s = getattr(userbot, "session", None)
    return s() if s else _OwnSession()


def configured() -> bool:
    f = getattr(userbot, "configured", None)
    if f:
        return bool(f())
    return bool(os.environ.get("TG_API_ID", "").isdigit() and os.environ.get("TG_API_HASH")
                and (os.environ.get("TG_SESSION") or os.environ.get("TG_SESSION_FILE")))


def why_not() -> str:
    f = getattr(userbot, "why_not", None)
    return f() if f else "ضع TG_API_ID و TG_API_HASH و TG_SESSION"


async def hunt(names: list, on_progress=None, should_stop=lambda: False,
               delay=(1.5, 2.6), batch: int = 40, max_consec_errors: int = 5) -> dict:
    """يفحص الأسماء بالتتابع. يرجع ملخصاً: free/fragment (قوائم) + عدّادات + flood/aborted"""
    load_cache()
    res = {FREE: [], FRAG: [], TAKEN: 0, INVALID: 0, ERROR: 0, "checked": 0, "flood": 0, "aborted": "",
           "all": []}  # all = [[الاسم، الحالة], ...] لكل ما فُحص
    consec = 0
    i = 0
    while i < len(names) and not should_stop() and not res["flood"] and not res["aborted"]:
        chunk = names[i:i + batch]
        i += batch
        try:
            async with session() as c:  # اتصال قصير لكل دفعة فلا يحتجز الحساب طويلاً
                for name in chunk:
                    if should_stop():
                        break
                    st, wait = await check_one(c, name)
                    if wait:
                        res["flood"] = wait
                        break
                    res["checked"] += 1
                    remember(name, st)
                    if st == ERROR:
                        res[ERROR] += 1
                        consec += 1
                        if consec >= max_consec_errors:
                            res["aborted"] = "أخطاء متتالية في الفحص — تحقق من الجلسة والاتصال"
                            break
                    else:
                        consec = 0
                        res["all"].append([name, st])
                        if st in (FREE, FRAG):
                            res[st].append(name)
                        else:
                            res[st] += 1
                    if on_progress:
                        await on_progress(res, len(names))
                    await asyncio.sleep(random.uniform(*delay))
        except Exception as e:  # noqa: BLE001
            res["aborted"] = f"{type(e).__name__}: {e}"
            break
    save_cache()
    return res


# ============================ تنسيق النتائج ============================
def format_results(free: list, frag: list, limit: int = 3500) -> list:
    """يرجع قائمة رسائل HTML (كل واحدة تحت حد تيليجرام)"""
    lines = []
    if free:
        lines.append(f"✅ <b>متاحة للتسجيل ({len(free)})</b> — سجّلها فوراً من إعدادات حسابك:")
        lines += [f"<code>@{n}</code>" for n in free]
    if frag:
        if lines:
            lines.append("")
        lines.append(f"💎 <b>معروضة للشراء على Fragment ({len(frag)})</b>:")
        lines += [f'<a href="https://fragment.com/username/{n}">@{n}</a>' for n in frag]
    out, cur = [], ""
    for ln in lines:
        if len(cur) + len(ln) + 1 > limit:
            out.append(cur)
            cur = ""
        cur += ln + "\n"
    if cur.strip():
        out.append(cur)
    return out


# ============================ ملفات التصدير ============================
STATUS_AR = {FREE: "متاح للتسجيل", FRAG: "معروض للشراء على Fragment", TAKEN: "مأخوذ", INVALID: "غير متاح/محجوز"}


def report_files(free: list, frag: list, allc: list) -> list:
    """يرجع [(اسم الملف، النص، الوصف)] — ملف بالمتاح، وملف بكل ما فُحص"""
    stamp = time.strftime("%Y%m%d_%H%M")
    when = time.strftime("%Y-%m-%d %H:%M")
    out = []
    if free or frag:
        lines = [f"# يوزرات متاحة — {when}", ""]
        if free:
            lines += [f"## ✅ متاحة للتسجيل ({len(free)})"] + [f"@{n}" for n in free] + [""]
        if frag:
            lines += [f"## 💎 معروضة للشراء على Fragment ({len(frag)})"]
            lines += [f"@{n}  https://fragment.com/username/{n}" for n in frag]
        out.append((f"available_{stamp}.txt", "\n".join(lines) + "\n",
                    f"📎 المتاح: ✅ {len(free)} · 💎 {len(frag)}"))
    if allc:
        lines = [f"# كل ما فُحص — {when} ({len(allc)} اسم)", ""]
        lines += [f"@{n} — {STATUS_AR.get(st, st)}" for n, st in allc]
        out.append((f"checked_{stamp}.txt", "\n".join(lines) + "\n", f"📎 كل الأسماء المفحوصة ({len(allc)})"))
    return out
