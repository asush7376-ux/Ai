# -*- coding: utf-8 -*-
"""
ai.py — Grok (xAI) + تصميم الصور بـ Gemini و Grok

المتغيرات:
  GROK_API_KEY            مفتاح xAI (من console.x.ai)
  GROK_MODEL              اختياري (الافتراضي grok-4-fast-non-reasoning)
  GROK_FALLBACK_MODEL     اختياري (الافتراضي grok-3-mini)
  GROK_IMAGE_MODEL        اختياري (الافتراضي grok-2-image)
  GEMINI_IMAGE_MODEL      اختياري (الافتراضي gemini-2.5-flash-image)
"""
import base64
import os

import httpx

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GROK_API_KEY = os.environ.get("GROK_API_KEY", "").strip()
GROK_MODEL = os.environ.get("GROK_MODEL", "grok-4-fast-non-reasoning")
GROK_FALLBACK_MODEL = os.environ.get("GROK_FALLBACK_MODEL", "grok-3-mini")
GROK_IMAGE_MODEL = os.environ.get("GROK_IMAGE_MODEL", "grok-2-image")
GEMINI_IMAGE_MODEL = os.environ.get("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")

NAMES = {"gemini": "Gemini", "grok": "Grok"}


class AIError(Exception):
    def __init__(self, msg: str, fatal: bool = False):
        super().__init__(msg)
        self.fatal = fatal


def available(provider: str) -> bool:
    return bool(GEMINI_API_KEY if provider == "gemini" else GROK_API_KEY)


def _err(r, name: str, model: str) -> AIError:
    try:
        j = r.json()
        msg = (j.get("error") or {}).get("message") if isinstance(j.get("error"), dict) else j.get("error")
        msg = msg or r.text[:200]
    except Exception:
        msg = r.text[:200]
    hints = {401: "المفتاح غير صالح", 403: "لا توجد صلاحية أو رصيد", 404: "اسم الموديل غير موجود",
             429: "تجاوزت الحد المسموح", 503: "ضغط عالٍ على الخدمة"}
    return AIError(f"خطأ {name} ({r.status_code}) [{model}]: {hints.get(r.status_code, '')}\n{str(msg)[:300]}",
                   fatal=r.status_code in (400, 401, 403))


# ============================ Grok — محادثة ============================
def _to_openai(system_prompt: str, contents: list) -> list:
    msgs = [{"role": "system", "content": system_prompt}] if system_prompt else []
    for c in contents:
        text = "".join(p.get("text", "") for p in c.get("parts", []))
        msgs.append({"role": "assistant" if c.get("role") == "model" else "user", "content": text})
    return msgs


async def _grok_once(model: str, system_prompt: str, contents: list, timeout: float, json_mode: bool) -> str:
    payload = {"model": model, "messages": _to_openai(system_prompt, contents),
               "temperature": 0.1 if json_mode else 0.8, "max_tokens": 4096}
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            r = await c.post("https://api.x.ai/v1/chat/completions", json=payload,
                             headers={"Authorization": f"Bearer {GROK_API_KEY}"})
    except httpx.HTTPError as e:
        raise AIError(f"تعذر الاتصال بـ Grok ({type(e).__name__}) [{model}]")
    if r.status_code != 200:
        raise _err(r, "Grok", model)
    try:
        text = (r.json()["choices"][0]["message"].get("content") or "").strip()
    except Exception:
        text = ""
    if not text:
        raise AIError(f"وصل رد فارغ من Grok [{model}]")
    return text


async def ask_grok(system_prompt: str, contents: list, smart: bool = False, json_mode: bool = False):
    if not GROK_API_KEY:
        raise AIError("Grok غير مُفعّل — ضع GROK_API_KEY", fatal=True)
    plan = [GROK_MODEL] + ([GROK_FALLBACK_MODEL] if GROK_FALLBACK_MODEL and GROK_FALLBACK_MODEL != GROK_MODEL else [])
    last = None
    for m in plan:
        try:
            return await _grok_once(m, system_prompt, contents, 60.0 if smart else 40.0, json_mode), m
        except AIError as e:
            last = e
            if e.fatal and e.args and "401" in str(e):
                raise
    raise last


# ============================ تصميم الصور ============================
async def _gemini_image(prompt: str) -> bytes:
    if not GEMINI_API_KEY:
        raise AIError("Gemini غير مُفعّل", fatal=True)
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_IMAGE_MODEL}:generateContent"
    payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
               "generationConfig": {"responseModalities": ["IMAGE", "TEXT"]}}
    try:
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post(url, json=payload, headers={"x-goog-api-key": GEMINI_API_KEY})
    except httpx.HTTPError as e:
        raise AIError(f"تعذر الاتصال بـ Gemini ({type(e).__name__})")
    if r.status_code != 200:
        raise _err(r, "Gemini", GEMINI_IMAGE_MODEL)
    for cand in r.json().get("candidates") or []:
        for p in cand.get("content", {}).get("parts", []):
            d = p.get("inlineData") or p.get("inline_data")
            if d and d.get("data"):
                return base64.b64decode(d["data"])
    raise AIError("لم يُرجع Gemini أي صورة (ربما رُفض الوصف)")


async def _grok_image(prompt: str) -> bytes:
    if not GROK_API_KEY:
        raise AIError("Grok غير مُفعّل", fatal=True)
    try:
        async with httpx.AsyncClient(timeout=120) as c:
            r = await c.post("https://api.x.ai/v1/images/generations",
                             json={"model": GROK_IMAGE_MODEL, "prompt": prompt, "n": 1,
                                   "response_format": "b64_json"},
                             headers={"Authorization": f"Bearer {GROK_API_KEY}"})
            if r.status_code != 200:
                raise _err(r, "Grok", GROK_IMAGE_MODEL)
            d = (r.json().get("data") or [{}])[0]
            if d.get("b64_json"):
                return base64.b64decode(d["b64_json"])
            if d.get("url"):
                img = await c.get(d["url"])
                if img.status_code == 200:
                    return img.content
    except httpx.HTTPError as e:
        raise AIError(f"تعذر الاتصال بـ Grok ({type(e).__name__})")
    raise AIError("لم يُرجع Grok أي صورة")


async def make_image(provider: str, prompt: str):
    """يرجع (bytes, المزوّد الذي نجح). يجرّب الآخر إن فشل الأول."""
    order = [provider] + [p for p in ("gemini", "grok") if p != provider]
    last = None
    for p in order:
        if not available(p):
            continue
        try:
            return await (_gemini_image if p == "gemini" else _grok_image)(prompt), p
        except AIError as e:
            last = e
    raise last or AIError("لا يوجد مزوّد صور مُفعّل")
