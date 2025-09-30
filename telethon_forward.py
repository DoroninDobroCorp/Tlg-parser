#!/usr/bin/env python3
"""Скрипт для пересылки всех сообщений из одного Telegram чата в другой."""
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
    from telethon.errors import RPCError, FloodWaitError, ChatWriteForbiddenError
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
    """Разрешает ссылку на чат/канал в entity."""
    try:
        if ref.isdigit():
            return await client.get_entity(int(ref))
        return await client.get_entity(ref)
    except Exception as e:
        logging.error(f"Не удалось найти чат/канал '{ref}': {e}")
        raise


async def forward_all(args):
    """Основная функция пересылки сообщений."""
    assert TelegramClient is not None
    
    if not args.source or not args.target:
        logging.error("Нужно указать --source и --target")
        return 2
    
    client = TelegramClient(args.session, args.api_id, args.api_hash)
    
    try:
        await client.start()
        logging.info("✓ Успешное подключение к Telegram")
        
        # Получаем информацию о чатах
        logging.info(f"Разрешаю источник: {args.source}")
        src = await resolve_peer(client, args.source)
        logging.info(f"✓ Найден источник: {getattr(src, 'title', getattr(src, 'username', str(src.id)))}")
        
        logging.info(f"Разрешаю получателя: {args.target}")
        dst = await resolve_peer(client, args.target)
        logging.info(f"✓ Найден получатель: {getattr(dst, 'title', getattr(dst, 'username', str(dst.id)))}")
        
        date_from = parse_date(args.from_date)
        date_to = parse_date(args.to_date)
        
        if date_from:
            logging.info(f"Фильтр: сообщения с {date_from.strftime('%Y-%m-%d')}")
        if date_to:
            logging.info(f"Фильтр: сообщения до {date_to.strftime('%Y-%m-%d')}")
        
        # Подсчитываем общее количество сообщений для progress bar
        total_count = 0
        logging.info("Подсчитываем общее количество сообщений...")
        async for msg in client.iter_messages(src, limit=args.limit, reverse=args.reverse):
            if date_from and msg.date and msg.date.replace(tzinfo=None) < date_from:
                continue
            if date_to and msg.date and msg.date.replace(tzinfo=None) > date_to:
                continue
            total_count += 1
        
        logging.info(f"Всего сообщений для обработки: {total_count}")
        
        if total_count == 0:
            logging.warning("Нет сообщений для обработки")
            return 0
        
        # Пересылаем сообщения
        count = 0
        success_count = 0
        error_count = 0
        skipped_count = 0
        
        async for msg in client.iter_messages(
            src,
            limit=args.limit,
            reverse=args.reverse,
            offset_date=None,
        ):
            # Фильтр по датам
            if date_from and msg.date and msg.date.replace(tzinfo=None) < date_from:
                skipped_count += 1
                continue
            if date_to and msg.date and msg.date.replace(tzinfo=None) > date_to:
                skipped_count += 1
                continue
            
            count += 1
            progress = (count / total_count * 100) if total_count > 0 else 0
            
            if args.dry_run:
                msg_preview = (msg.message or '')[:50].replace('\n', ' ') if msg.message else '<медиа>'
                logging.info(
                    f"[{count}/{total_count}] ({progress:.1f}%) DRY: "
                    f"msg_id={msg.id} date={msg.date} text={msg_preview!r}"
                )
                success_count += 1
                continue
            
            try:
                if args.no_forward:
                    # Копируем содержимое от своего имени
                    if msg.media:
                        await client.send_file(dst, msg.media, caption=msg.message or "")
                    elif msg.message:
                        await client.send_message(dst, msg.message)
                    else:
                        # Пустое сообщение (например, только стикер/реакция)
                        skipped_count += 1
                        logging.debug(f"Пропущено пустое сообщение msg_id={msg.id}")
                        continue
                else:
                    # Нативная пересылка (с сохранением автора)
                    await client.forward_messages(dst, msg)
                
                success_count += 1
                if count % 10 == 0 or count == total_count:
                    logging.info(
                        f"[{count}/{total_count}] ({progress:.1f}%) ✓ Переслано успешно | "
                        f"Успешно: {success_count}, Ошибок: {error_count}, Пропущено: {skipped_count}"
                    )
                    
            except FloodWaitError as e:
                wait_seconds = e.seconds
                logging.warning(
                    f"FloodWait: нужно подождать {wait_seconds} секунд. "
                    f"Пауза... (msg_id={msg.id})"
                )
                await asyncio.sleep(wait_seconds + 1)
                # Повторная попытка
                try:
                    if args.no_forward:
                        if msg.media:
                            await client.send_file(dst, msg.media, caption=msg.message or "")
                        elif msg.message:
                            await client.send_message(dst, msg.message)
                    else:
                        await client.forward_messages(dst, msg)
                    success_count += 1
                except Exception as retry_err:
                    error_count += 1
                    logging.error(f"Ошибка при повторной отправке msg_id={msg.id}: {retry_err}")
                    
            except ChatWriteForbiddenError as e:
                error_count += 1
                logging.error(
                    f"Нет прав на запись в целевой чат! msg_id={msg.id}: {e}\n"
                    f"Проверьте, что вы администратор или участник канала/группы."
                )
                # Продолжаем попытки или прерываем?
                if error_count > 5:
                    logging.error("Слишком много ошибок записи. Останавливаюсь.")
                    break
                    
            except RPCError as e:
                error_count += 1
                logging.error(f"RPC ошибка при отправке msg_id={msg.id}: {e}")
                await asyncio.sleep(1)
                
            except Exception as e:
                error_count += 1
                logging.error(f"Неожиданная ошибка при отправке msg_id={msg.id}: {e}")
                await asyncio.sleep(1)
        
        # Итоговая статистика
        logging.info("="*60)
        logging.info("ИТОГОВАЯ СТАТИСТИКА:")
        logging.info(f"  Всего обработано: {count}")
        logging.info(f"  ✓ Успешно: {success_count}")
        logging.info(f"  ✗ Ошибок: {error_count}")
        logging.info(f"  ⊘ Пропущено: {skipped_count}")
        logging.info("="*60)
        
        return 0 if error_count == 0 else 1
        
    except Exception as e:
        logging.error(f"Критическая ошибка: {e}", exc_info=True)
        return 1
    finally:
        await client.disconnect()
        logging.info("Отключение от Telegram")


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
