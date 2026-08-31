#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
«Содержание» · кнопка оглавления в канале
=========================================
Публикует в канал пост с кнопкой «📖 Оглавление» (Telegram Mini App)
и закрепляет его. Запускается из GitHub Actions (workflow button.yml)
или вручную локально.

Аргументы:
    --url   https://<логин>.github.io/<репо>/   адрес мини-аппа (Pages)
    --text  надпись на кнопке (по умолчанию «📖 Оглавление»)
    --caption  текст над кнопкой
    --no-pin   не закреплять пост

Секреты: BOT_TOKEN, CHANNEL_USERNAME (как у новостного конвейера).
"""
import argparse
import json
import os
import sys
import urllib.request

def http_json(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"),
                                 method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True, help="адрес мини-аппа на GitHub Pages")
    ap.add_argument("--text", default="📖 Оглавление")
    ap.add_argument("--caption", default="Все посты канала — в одном каталоге.\nПоиск по темам, датам и словам 👇")
    ap.add_argument("--no-pin", action="store_true")
    args = ap.parse_args()

    token = os.environ.get("BOT_TOKEN", "")
    chat = (os.environ.get("CHANNEL_USERNAME", "") or "").strip().lstrip("@").replace("https://t.me/", "")
    if not token or not chat:
        print("!! нужны BOT_TOKEN и CHANNEL_USERNAME (окружение или GitHub Secrets)")
        return 1
    if not args.url.startswith("https://"):
        print("!! --url должен начинаться с https:// (адрес GitHub Pages)")
        return 1

    api = f"https://api.telegram.org/bot{token}"
    resp = http_json(api + "/sendMessage", {
        "chat_id": "@" + chat,
        "text": args.caption,
        "reply_markup": {"inline_keyboard": [[
            {"text": args.text, "web_app": {"url": args.url}}
        ]]},
    })
    if not resp.get("ok"):
        print(f"!! Telegram: {resp.get('description')}")
        return 1
    msg_id = resp["result"]["message_id"]
    print(f"✓ пост с кнопкой опубликован: t.me/{chat}/{msg_id}")

    if not args.no_pin:
        pin = http_json(api + "/pinChatMessage", {
            "chat_id": "@" + chat, "message_id": msg_id, "disable_notification": True,
        })
        print("✓ закреплён в канале" if pin.get("ok") else f"!! не закрепился: {pin.get('description')} — закрепите вручную")
    print("\nГотово: у читателей канала появилась кнопка «Оглавление».")
    return 0

if __name__ == "__main__":
    sys.exit(main())
