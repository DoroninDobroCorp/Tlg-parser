#!/usr/bin/env python3
from __future__ import annotations
import argparse
import asyncio
import logging
import sys
import time
from datetime import datetime
from typing import Optional
import os

try:
    import tomllib  # Python 3.11+
except Exception:
    tomllib = None  # type: ignore

try:
    from telethon import TelegramClient
    from telethon.errors import RPCError
    from telethon.tl.types import InputPeerChannel, InputPeerChat
except Exception as e:
    TelegramClient = None  # type: ignore
    _telethon_import_error = e
else:
    _telethon_import_error = None


def parse_args():
    p = argparse.ArgumentParser(description="Пересылка всех сообщений из чата/канала в другой через Telethon")
    p.add_argument("--config", default="telethon_config.toml", help="Путь к конфигу TOML (по умолчанию telethon_config.toml)")
    p.add_argument("--api-id", type=int, default=None, help="API ID (https://my.telegram.org)")
    p.add_argument("--api-hash", default=None, help="API HASH (https://my.telegram.org)")
    p.add_argument("--session", default=None, help="Имя/путь session-файла для Telethon")
    p.add_argument("--source", required=False, help="Источник: @username | t.me/link | numeric id")
    p.add_argument("--target", required=False, help="Цель: @username | t.me/link | numeric id")
    p.add_argument("--limit", type=int, default=None, help="Ограничить кол-во сообщений")
    p.add_argument("--from-date", default=None, help="Фильтр от даты (YYYY-MM-DD)")
    p.add_argument("--to-date", default=None, help="Фильтр до даты (YYYY-MM-DD)")
    p.add_argument("--reverse", action="store_true", help="Идти от старых к новым")
    p.add_argument("--no-forward", action="store_true", help="Не пересылать, а копировать контент (текст/медиа) от вашего имени")
    p.add_argument("--dry-run", action="store_true", help="Только перечислить, что будет переслано, без отправки")
    p.add_argument("--self-test", action="store_true", help="Самопроверка без подключения к Telegram (10 секунд)")
    p.add_argument("--log", default="INFO", help="Уровень логирования: DEBUG/INFO/WARNING")
    return p.parse_args()


def setup_logging(level: str):
    lvl = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(level=lvl, format="[%(asctime)s] %(levelname)s: %(message)s", datefmt="%H:%M:%S")


def parse_date(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%d")


async def resolve_peer(client: TelegramClient, ref: str):
    if ref.isdigit():
        return await client.get_entity(int(ref))
    return await client.get_entity(ref)


async def forward_all(args):
    assert TelegramClient is not None
    client = TelegramClient(args.session, args.api_id, args.api_hash)
    await client.start()

    if not args.source or not args.target:
        logging.error("Нужно указать --source и --target")
        return 2

    src = await resolve_peer(client, args.source)
    dst = await resolve_peer(client, args.target)

    date_from = parse_date(args.from_date)
    date_to = parse_date(args.to_date)

    count = 0
    async for msg in client.iter_messages(
        src,
        limit=args.limit,
        reverse=args.reverse,
        offset_date=None,
    ):
        # Фильтр по датам
        if date_from and msg.date and msg.date.replace(tzinfo=None) < date_from:
            continue
        if date_to and msg.date and msg.date.replace(tzinfo=None) > date_to:
            continue

        count += 1
        if args.dry_run:
            logging.info(f"DRY: message_id={msg.id} date={msg.date} text={(msg.message or '')[:40]!r}")
            continue

        try:
            if args.no_forward:
                # Копируем содержимое
                if msg.media:
                    await client.send_file(dst, msg.media, caption=msg.message or "")
                else:
                    if msg.message:
                        await client.send_message(dst, msg.message)
            else:
                # Нативная пересылка (с сохранением автора)
                await client.forward_messages(dst, msg)
        except RPCError as e:
            logging.error(f"Ошибка при отправке msg_id={msg.id}: {e}")
            await asyncio.sleep(1)

    logging.info(f"Готово. Обработано сообщений: {count}")
    return 0


async def main_async(args):
    if args.self_test:
        logging.info("Self-test telethon_forward: 10 секунд сна без подключения")
        await asyncio.sleep(10)
        logging.info("Self-test завершён")
        return 0

    if _telethon_import_error:
        logging.error(f"Telethon не установлен или ошибка импорта: {_telethon_import_error}")
        return 1

    # Загрузка конфига TOML (необязательно)
    cfg = {}
    if args.config and os.path.exists(args.config) and tomllib is not None:
        try:
            with open(args.config, "rb") as f:
                cfg = tomllib.load(f) or {}
        except Exception as e:
            logging.warning(f"Не удалось прочитать {args.config}: {e}")

    # Извлекаем значения из конфига, затем перекрываем CLI-аргументами
    def cfg_get(key, default=None):
        return cfg.get(key, default)

    api_id = args.api_id if args.api_id is not None else cfg_get("api_id")
    api_hash = args.api_hash if args.api_hash is not None else cfg_get("api_hash")
    session = args.session if args.session is not None else cfg_get("session", "forward_session")
    source = args.source if args.source is not None else cfg_get("source")
    target = args.target if args.target is not None else cfg_get("target")
    limit = args.limit if args.limit is not None else cfg_get("limit")
    from_date = args.from_date if args.from_date is not None else cfg_get("from_date")
    to_date = args.to_date if args.to_date is not None else cfg_get("to_date")
    reverse = args.reverse or bool(cfg_get("reverse", False))
    no_forward = args.no_forward or bool(cfg_get("no_forward", False))
    dry_run = args.dry_run or bool(cfg_get("dry_run", False))

    # Записываем обратно в args для использования downstream
    args.api_id = api_id
    args.api_hash = api_hash
    args.session = session
    args.source = source
    args.target = target
    args.limit = limit
    args.from_date = from_date
    args.to_date = to_date
    args.reverse = reverse
    args.no_forward = no_forward
    args.dry_run = dry_run

    if not api_id or not api_hash:
        logging.error("Укажите api_id и api_hash через --api-id/--api-hash или в конфиге telethon_config.toml")
        return 2

    return await forward_all(args)


def main():
    args = parse_args()
    setup_logging(args.log)
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        logging.info("Завершено по Ctrl+C")
        return 130


if __name__ == "__main__":
    sys.exit(main())
