# -*- coding: utf-8 -*-
"""
فهرس المكتبة (SQLite) + بحث ذكي يفهم العربية.
لا يعتمد على تيليجرام؛ فقط يخزّن بيانات الملفات ويبحث فيها.
"""
import math
import os
import re
import sqlite3
import threading

DB_PATH = os.environ.get("LIBRARY_DB", "library.db")
_lock = threading.RLock()
_conn = None

ICONS = {
    "audio": "🎵", "voice": "🎙", "video": "🎬", "animation": "🎞",
    "photo": "🖼", "document": "📄", "text": "📝",
}
KIND_AR = {
    "audio": "صوتية", "voice": "رسالة صوتية", "video": "فيديو", "animation": "GIF",
    "photo": "صورة", "document": "ملف", "text": "نص",
}
# نوع الطلب -> أنواع الملفات المخزنة
KIND_GROUPS = {
    "audio": ("audio", "voice"),
    "video": ("video", "animation"),
    "photo": ("photo",),
    "document": ("document",),
    "text": ("text",),
}


# ============================ قاعدة البيانات ============================
def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS items(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                msg_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                name TEXT, title TEXT, performer TEXT, filename TEXT, ext TEXT,
                caption TEXT, text TEXT,
                duration INTEGER, size INTEGER,
                norm TEXT, date INTEGER,
                UNIQUE(chat_id, msg_id)
            );
            CREATE INDEX IF NOT EXISTS idx_items_chat ON items(chat_id);
            CREATE INDEX IF NOT EXISTS idx_items_kind ON items(kind);
            """
        )
        _conn.commit()
    return _conn


_UPSERT = """
INSERT INTO items(chat_id,msg_id,kind,name,title,performer,filename,ext,caption,text,duration,size,norm,date)
VALUES(:chat_id,:msg_id,:kind,:name,:title,:performer,:filename,:ext,:caption,:text,:duration,:size,:norm,:date)
ON CONFLICT(chat_id,msg_id) DO UPDATE SET
  kind=excluded.kind, name=excluded.name, title=excluded.title, performer=excluded.performer,
  filename=excluded.filename, ext=excluded.ext, caption=excluded.caption, text=excluded.text,
  duration=excluded.duration, size=excluded.size, norm=excluded.norm, date=excluded.date
"""


def upsert(items: list) -> int:
    items = [i for i in items if i]
    if not items:
        return 0
    with _lock:
        c = _db()
        c.executemany(_UPSERT, items)
        c.commit()
    return len(items)


def get(rowid: int):
    with _lock:
        r = _db().execute("SELECT * FROM items WHERE id=?", (rowid,)).fetchone()
    return dict(r) if r else None


def get_many(ids: list) -> list:
    """يرجع العناصر بنفس ترتيب المعرفات"""
    if not ids:
        return []
    with _lock:
        q = ",".join("?" * len(ids))
        rows = _db().execute(f"SELECT * FROM items WHERE id IN ({q})", list(ids)).fetchall()
    by_id = {r["id"]: dict(r) for r in rows}
    return [by_id[i] for i in ids if i in by_id]


def delete_item(rowid: int) -> None:
    with _lock:
        c = _db()
        c.execute("DELETE FROM items WHERE id=?", (rowid,))
        c.commit()


def delete_chat(chat_id: int) -> None:
    with _lock:
        c = _db()
        c.execute("DELETE FROM items WHERE chat_id=?", (int(chat_id),))
        c.commit()


def count(chat_id=None) -> int:
    with _lock:
        if chat_id is None:
            return _db().execute("SELECT COUNT(*) FROM items").fetchone()[0]
        return _db().execute("SELECT COUNT(*) FROM items WHERE chat_id=?", (int(chat_id),)).fetchone()[0]


def max_msg_id(chat_id: int) -> int:
    with _lock:
        return _db().execute(
            "SELECT COALESCE(MAX(msg_id),0) FROM items WHERE chat_id=?", (int(chat_id),)
        ).fetchone()[0]


# ============================ التطبيع للعربية ============================
_DIAC = re.compile(r"[\u064B-\u065F\u0670\u06D6-\u06ED\u0640]")  # تشكيل + تطويل
_TRANS = str.maketrans(
    {
        "أ": "ا", "إ": "ا", "آ": "ا", "ٱ": "ا",
        "ى": "ي", "ئ": "ي", "ی": "ي",
        "ؤ": "و", "ة": "ه", "ک": "ك",
        **{chr(0x0660 + i): str(i) for i in range(10)},  # ٠-٩
        **{chr(0x06F0 + i): str(i) for i in range(10)},  # ۰-۹
    }
)


def normalize(text) -> str:
    if not text:
        return ""
    t = _DIAC.sub("", str(text)).translate(_TRANS).lower()
    t = re.sub(r"[^\w\s]|_", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def term_variants(term) -> list:
    n = normalize(term)
    if len(n) < 2:
        return []
    out = [n]
    stripped = " ".join(w[2:] if w.startswith("ال") and len(w) >= 5 else w for w in n.split())
    if len(stripped) >= 2 and stripped not in out:
        out.append(stripped)
    return out


def _natural_key(s: str):
    return [int(p) if i % 2 else p for i, p in enumerate(re.split(r"(\d+)", s or ""))]


# ============================ بناء العناصر ============================
def _first_line(s, n: int = 80) -> str:
    s = (s or "").strip().split("\n")[0].strip()
    return s[:n]


def make_item(chat_id, msg_id, kind, title=None, performer=None, filename=None, ext=None,
              caption=None, text=None, duration=None, size=None, date=None):
    if kind == "text" and not (text or "").strip():
        return None
    base = os.path.splitext(filename)[0] if filename else ""
    if title:
        name = f"{performer} - {title}" if performer else title
    elif base:
        name = base
    else:
        name = _first_line(caption or text)
    norm = normalize(" ".join(x for x in (name, title, performer, base, caption, text) if x))
    return {
        "chat_id": int(chat_id), "msg_id": int(msg_id), "kind": kind, "name": name or "",
        "title": title, "performer": performer, "filename": filename, "ext": ext,
        "caption": caption, "text": text,
        "duration": int(duration) if duration else None,
        "size": int(size) if size else None,
        "norm": norm, "date": int(date or 0),
    }


def item_from_ptb(m, include_text: bool = True):
    """يحوّل رسالة python-telegram-bot إلى عنصر فهرس (أو None إذا لا تصلح)"""
    cid, mid = m.chat_id, m.message_id
    date = int(m.date.timestamp()) if getattr(m, "date", None) else 0
    cap = m.caption
    if m.audio:
        a = m.audio
        return make_item(cid, mid, "audio", title=a.title, performer=a.performer, filename=a.file_name,
                         caption=cap, duration=a.duration, size=a.file_size, date=date)
    if m.voice:
        v = m.voice
        return make_item(cid, mid, "voice", caption=cap, duration=v.duration, size=v.file_size, date=date)
    if m.video:
        v = m.video
        return make_item(cid, mid, "video", filename=v.file_name, caption=cap,
                         duration=v.duration, size=v.file_size, date=date)
    if m.video_note:
        v = m.video_note
        return make_item(cid, mid, "video", caption=cap, duration=v.duration, size=v.file_size, date=date)
    if m.animation:  # قبل document لأن الأنيميشن يحمل الحقلين
        a = m.animation
        return make_item(cid, mid, "animation", filename=a.file_name, caption=cap,
                         duration=a.duration, size=a.file_size, date=date)
    if m.document:
        d = m.document
        return make_item(cid, mid, "document", filename=d.file_name, caption=cap, size=d.file_size, date=date)
    if m.photo:
        return make_item(cid, mid, "photo", caption=cap, size=m.photo[-1].file_size, date=date)
    if include_text and m.text:
        return make_item(cid, mid, "text", text=m.text, date=date)
    return None


# ============================ البحث ============================
def search(terms: list, media: str = "any", limit: int = 100) -> list:
    """
    terms: قائمة مفاهيم، كل مفهوم قائمة كلمات بديلة، مثال: [["البقرة","baqarah"],["سورة"]]
    الترتيب بوزن نُدرة الكلمة (IDF): الكلمة النادرة أهم من الكلمة الشائعة.
    """
    groups = []
    for t in terms or []:
        vs = []
        for v in (t if isinstance(t, (list, tuple)) else [t]):
            vs += term_variants(v)
        vs = list(dict.fromkeys(vs))
        if vs:
            groups.append(vs)
    if not groups:
        return []

    kinds = KIND_GROUPS.get(media)
    kind_sql, kind_args = "", []
    if kinds:
        kind_sql = " AND kind IN (%s)" % ",".join("?" * len(kinds))
        kind_args = list(kinds)

    with _lock:
        c = _db()
        total = c.execute("SELECT COUNT(*) FROM items WHERE 1=1" + kind_sql, kind_args).fetchone()[0]
        if total == 0:
            return []
        active = []
        for vs in groups:
            cond = " OR ".join(["norm LIKE ?"] * len(vs))
            df = c.execute(
                f"SELECT COUNT(*) FROM items WHERE ({cond})" + kind_sql,
                [f"%{v}%" for v in vs] + kind_args,
            ).fetchone()[0]
            if df:
                active.append((vs, max(0.2, math.log((total + 1) / (df + 0.5)))))
        if not active:
            return []
        allv = [v for vs, _ in active for v in vs]
        cond = " OR ".join(["norm LIKE ?"] * len(allv))
        rows = c.execute(
            f"SELECT * FROM items WHERE ({cond})" + kind_sql + " LIMIT 5000",
            [f"%{v}%" for v in allv] + kind_args,
        ).fetchall()

    scored = []
    for r in rows:
        s = sum(w for vs, w in active if any(v in r["norm"] for v in vs))
        scored.append((s, dict(r)))
    top = max(s for s, _ in scored)
    keep = [(s, r) for s, r in scored if s >= top * 0.6]
    keep.sort(key=lambda x: (-round(x[0], 1), _natural_key(x[1]["name"]), x[1]["msg_id"]))
    return [r for _, r in keep[:limit]]


def search_text(query: str, media: str = "any") -> list:
    """بحث بالكلمة/الجملة كما كتبها المستخدم فقط (الهاشتاغ يُعامل ككلمة عادية). بلا حد للنتائج.
    1) العبارة كاملة متتالية  2) إن لم توجد: المنشورات التي تحوي كل الكلمات"""
    phrase = normalize(query)
    if not phrase:
        return []
    kinds = KIND_GROUPS.get(media)
    kind_sql, kind_args = "", []
    if kinds:
        kind_sql = " AND kind IN (%s)" % ",".join("?" * len(kinds))
        kind_args = list(kinds)
    order = " ORDER BY chat_id, msg_id"
    with _lock:
        c = _db()
        rows = c.execute(
            "SELECT * FROM items WHERE (' '||norm||' ') LIKE ?" + kind_sql + order,
            [f"%{phrase}%"] + kind_args,
        ).fetchall()
        words = phrase.split()
        if not rows and len(words) > 1:
            cond = " AND ".join(["norm LIKE ?"] * len(words))
            rows = c.execute(
                f"SELECT * FROM items WHERE {cond}" + kind_sql + order,
                [f"%{w}%" for w in words] + kind_args,
            ).fetchall()
    return [dict(r) for r in rows]


# ============================ العرض ============================
def fmt_duration(sec) -> str:
    sec = int(sec or 0)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def label(it: dict, maxlen: int = 60) -> str:
    icon = ICONS.get(it["kind"], "📄")
    name = it.get("name") or f"{KIND_AR.get(it['kind'], 'ملف')} #{it['msg_id']}"
    if len(name) > maxlen:
        name = name[: maxlen - 1] + "…"
    dur = f" · {fmt_duration(it['duration'])}" if it.get("duration") else ""
    return f"{icon} {name}{dur}"


def summarize(rows: list) -> str:
    counts = {}
    for r in rows:
        counts[r["kind"]] = counts.get(r["kind"], 0) + 1
    return " · ".join(f"{ICONS[k]} {n} {KIND_AR[k]}" for k, n in counts.items())


def link_for(src: dict, it: dict):
    u = src.get("username")
    if u:
        return f"https://t.me/{u}/{it['msg_id']}"
    cid = str(it["chat_id"])
    if cid.startswith("-100"):
        return f"https://t.me/c/{cid[4:]}/{it['msg_id']}"
    return None
