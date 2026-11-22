# -*- coding: utf-8 -*-
"""
make_session.py — يولّد TG_SESSION (StringSession) لربط الحساب (userbot.py).

شغّله على جهازك:
    pip install telethon
    python3 make_session.py

ملاحظات:
  • أنشئ الجلسة على بيئة تيليجرام العادية (وليست التجريبية) لأن userbot.py يعمل عليها.
  • الجلسة = دخول كامل للحساب: لا ترفعها لجيتهوب ولا تشاركها مع أحد، ضعها في متغير TG_SESSION فقط.
"""
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

api_id = int(input("API_ID (من my.telegram.org): ").strip())
api_hash = input("API_HASH: ").strip()
with TelegramClient(StringSession(), api_id, api_hash) as c:
    print("\n✅ انسخ السطر التالي وضعه في متغير البيئة TG_SESSION:\n")
    print(c.session.save())
