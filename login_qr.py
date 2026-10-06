"""
Вход в Telegram по QR-коду (если код по номеру не приходит).
Запусти один раз: python login_qr.py
Потом обычный main.py подхватит созданную сессию.
"""
import asyncio
import json
from getpass import getpass
from pathlib import Path

import qrcode
from telethon import TelegramClient, errors

BASE = Path(__file__).parent
cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8"))


async def main():
    client = TelegramClient(str(BASE / cfg["session_name"]), cfg["api_id"], cfg["api_hash"])
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        print(f"Уже вошли как {me.first_name}")
        return

    qr_login = await client.qr_login()
    while True:
        qr = qrcode.QRCode(border=1)
        qr.add_data(qr_login.url)
        qr.print_ascii(invert=True)
        print("\nВ Telegram на телефоне: Настройки -> Устройства -> Подключить устройство")
        print("и отсканируй QR выше (код обновляется раз в ~30 сек)\n")
        try:
            await qr_login.wait(timeout=30)
            break
        except asyncio.TimeoutError:
            await qr_login.recreate()
        except errors.SessionPasswordNeededError:
            await client.sign_in(password=getpass("Облачный пароль (2FA): "))
            break

    me = await client.get_me()
    print(f"Готово, вошли как {me.first_name}. Теперь запускай main.py")
    await client.disconnect()


asyncio.run(main())
