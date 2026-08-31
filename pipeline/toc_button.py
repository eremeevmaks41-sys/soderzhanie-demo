#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
«Оглавление телеграм-канала» · кнопка оглавления в канале
=========================================
Публикует в канал пост с кнопкой «📖 Оглавление» и закрепляет его.
Запускается из GitHub Actions (workflow button.yml) или вручную локально.

Как это работает (Bot API 10.x, проверено живым тестом):
  web_app-кнопки в постах каналов ЗАПРЕЩЕНЫ («Available in private chats
  only»), поэтому используется официальный паттерн «главный мини-апп»:
    1. скрипт привязывает Pages-адрес к боту как главный мини-апп
       (setChatMenuButton) — достаточно одного раза, повтор безвреден;
    2. публикует пост с ОБЫЧНОЙ url-кнопкой на прямую ссылку
       https://t.me/<бот>?startapp — она открывает мини-апп в Telegram;
    3. закрепляет пост в шапке канала.

Аргументы:
    --url   https://<логин>.github.io/<репо>/   адрес мини-аппа (Pages)
    --app-link  https://t.me/<бот>/<имя>  прямая ссылка Direct-Link Mini App
                (создаётся один раз в BotFather через /newapp; ЛУЧШИЙ UX:
                кнопка открывает каталог сразу, без чата с ботом и /start)
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
    ap.add_argument("--app-link", default="",
                    help="прямая ссылка Direct-Link Mini App (t.me/бот/имя) — приоритетнее")
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

    # 0. Юзернейм бота — для прямой ссылки на мини-апп
    me = http_json(api + "/getMe", {})
    if not me.get("ok"):
        print(f"!! getMe: {me.get('description')}")
        return 1
    bot = me["result"]["username"]

    # Выбираем ссылку кнопки: прямая (Direct-Link) > фолбэк ?startapp
    if args.app_link:
        app_link = args.app_link.strip()
        if not app_link.startswith("https://t.me/"):
            print("!! --app-link должен начинаться с https://t.me/")
            return 1
        print("✓ кнопка ведёт на Direct-Link Mini App:", app_link)
    else:
        app_link = f"https://t.me/{bot}?startapp"
        print("! прямая ссылка не задана — используется фолбэк t.me/{}?startapp".format(bot))
        print("  (лучший UX — Direct-Link: BotFather → /newapp, см. гайд, раздел 7)")

    # 1. Привязываем мини-апп к боту (главный мини-апп) — делает ссылку
    #    t.me/<бот>?startapp рабочей. Повторный вызов безвреден.
    menu = http_json(api + "/setChatMenuButton", {
        "menu_button": {"type": "web_app", "text": args.text,
                        "web_app": {"url": args.url}},
    })
    print("✓ мини-апп привязан к боту (главный мини-апп)" if menu.get("ok")
          else f"!! setChatMenuButton: {menu.get('description')} — кнопка в посте может не открыться")

    # 2. Пост с обычной url-кнопкой (web_app-кнопки в каналах запрещены)
    resp = http_json(api + "/sendMessage", {
        "chat_id": "@" + chat,
        "text": args.caption,
        "reply_markup": {"inline_keyboard": [[
            {"text": args.text, "url": app_link}
        ]]},
    })
    if not resp.get("ok"):
        print(f"!! Telegram: {resp.get('description')}")
        return 1
    msg_id = resp["result"]["message_id"]
    print(f"✓ пост с кнопкой опубликован: t.me/{chat}/{msg_id}")

    # 3. Закрепление
    if not args.no_pin:
        pin = http_json(api + "/pinChatMessage", {
            "chat_id": "@" + chat, "message_id": msg_id, "disable_notification": True,
        })
        print("✓ закреплён в канале" if pin.get("ok") else f"!! не закрепился: {pin.get('description')} — закрепите вручную")
    print(f"\nГотово: у читателей канала кнопка «{args.text}» открывает оглавление.")
    return 0

if __name__ == "__main__":
    sys.exit(main())
