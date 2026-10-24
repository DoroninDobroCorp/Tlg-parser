#!/usr/bin/env python3
"""Скрипт для быстрой и безопасной пересылки сообщений из одного Telegram-чата в другой."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import List, Optional

# Поддержка TOML для Python < 3.11 и 3.11+
try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:
    try:
        import tomli as tomllib  # type: ignore
    except ModuleNotFoundError:
        tomllib = None  # type: ignore

try:
    from telethon import TelegramClient
    from telethon.errors import (
        ChatWriteForbiddenError,
        FloodWaitError,
        MessageIdInvalidError,
        RPCError,
    )
    from telethon.tl.types import (
        InputPeerChannel,
        InputPeerChat,
        Message,
        MessageService,
    )
except Exception as e:
    TelegramClient = None  # type: ignore
    _telethon_import_error = e
else:
    _telethon_import_error = None


DEFAULT_CHECKPOINT_FILE = ".forward_checkpoint.json"


def parse_args():
    p = argparse.ArgumentParser(
        description="Пересылка всех сообщений из чата/канала в другой через Telethon API"
    )
    p.add_argument(
        "--config",
        default="telethon_config.toml",
        help="Путь к конфигу TOML (по умолчанию telethon_config.toml)",
    )
    p.add_argument(
        "--api-id",
        type=int,
        default=None,
        help="API ID (https://my.telegram.org)",
    )
    p.add_argument(
        "--api-hash",
        default=None,
        help="API HASH (https://my.telegram.org)",
    )
    p.add_argument(
        "--session",
        default=None,
        help="Имя/путь session-файла Telethon (по умолчанию forward_session)",
    )
    p.add_argument(
        "--source",
        required=False,
        help="Источник: @username | t.me/link | числовой ID (-100...)",
    )
    p.add_argument(
        "--target",
        required=False,
        help="Цель: @username | t.me/link | числовой ID (-100...) | me",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Ограничить количество пересылаемых сообщений",
    )
    p.add_argument(
        "--from-date",
        default=None,
        help="Фильтр: от даты включительно (формат YYYY-MM-DD или 'YYYY-MM-DD HH:MM:SS')",
    )
    p.add_argument(
        "--to-date",
        default=None,
        help="Фильтр: до даты включительно (формат YYYY-MM-DD или 'YYYY-MM-DD HH:MM:SS')",
    )
    p.add_argument(
        "--reverse",
        action="store_true",
        help="Идти в хронологическом порядке: от старых к новым (рекомендуется)",
    )
    p.add_argument(
        "--delay",
        type=float,
        default=None,
        help="Пауза между отправками в секундах для защиты от бана (по умолчанию 1.0)",
    )
    p.add_argument(
        "--batch-size",
        type=int,
        default=None,
        help="Количество сообщений в пачке при нативной пересылке (1-100, по умолчанию 20)",
    )
    p.add_argument(
        "--no-forward",
        action="store_true",
        help="Копировать контент (текст/медиа) от вашего имени без плашки «Переслано от»",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Только перечислить сообщения, без реальной отправки",
    )
    p.add_argument(
        "--min-id",
        type=int,
        default=None,
        help="Фильтр: только сообщения с ID больше min-id",
    )
    p.add_argument(
        "--max-id",
        type=int,
        default=None,
        help="Фильтр: только сообщения с ID меньше max-id",
    )
    p.add_argument(
        "--offset-id",
        type=int,
        default=None,
        help="Начать итерацию с указанного ID сообщения",
    )
    p.add_argument(
        "--resume",
        action="store_true",
        help="Возобновить пересылку с последнего сохраненного чекпоинта",
    )
    p.add_argument(
        "--checkpoint",
        default=DEFAULT_CHECKPOINT_FILE,
        help=f"Файл сохранения прогресса (по умолчанию {DEFAULT_CHECKPOINT_FILE})",
    )
    p.add_argument(
        "--max-wait",
        type=int,
        default=300,
        help="Максимальное время ожидания FloodWait в секундах перед остановкой (по умолчанию 300)",
    )
    p.add_argument(
        "--self-test",
        action="store_true",
        help="Самопроверка без подключения к Telegram",
    )
    p.add_argument(
        "--log",
        default="INFO",
        help="Уровень логирования: DEBUG / INFO / WARNING / ERROR",
    )
    return p.parse_args()


def setup_logging(level: str):
    lvl = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=lvl,
        format="[%(asctime)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def parse_date_start(s: Optional[str]) -> Optional[datetime]:
    """Парсит начало временного диапазона в UTC."""
    if not s:
        return None
    s = s.strip()
    try:
        if " " in s:
            dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        else:
            dt = datetime.strptime(s, "%Y-%m-%d")
        return dt.replace(tzinfo=timezone.utc)
    except ValueError as e:
        raise ValueError(
            f"Неверный формат даты '{s}'. Используйте YYYY-MM-DD или 'YYYY-MM-DD HH:MM:SS'"
        ) from e


def parse_date_end(s: Optional[str]) -> Optional[datetime]:
    """Парсит конец временного диапазона в UTC (включает весь день до 23:59:59)."""
    if not s:
        return None
    s = s.strip()
    try:
        if " " in s:
            dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
        else:
            dt = datetime.strptime(s, "%Y-%m-%d").replace(
                hour=23, minute=59, second=59, microsecond=999999
            )
        return dt.replace(tzinfo=timezone.utc)
    except ValueError as e:
        raise ValueError(
            f"Неверный формат даты '{s}'. Используйте YYYY-MM-DD или 'YYYY-MM-DD HH:MM:SS'"
        ) from e


def load_checkpoint(filepath: str) -> Optional[dict]:
    """Загружает сохраненное состояние прогресса."""
    if not os.path.exists(filepath):
        return None
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.warning(f"Не удалось загрузить чекпоинт {filepath}: {e}")
        return None


def save_checkpoint(filepath: str, data: dict):
    """Сохраняет состояние прогресса в файл."""
    try:
        temp_file = f"{filepath}.tmp"
        with open(temp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(temp_file, filepath)
    except Exception as e:
        logging.warning(f"Не удалось сохранить чекпоинт {filepath}: {e}")


async def resolve_peer(client: TelegramClient, ref: str):
    """Разрешает ссылку, юзернейм, 'me' или числовой ID (включая отрицательные) в Telegram entity."""
    ref_str = str(ref).strip()
    try:
        # Обработка числовых ID (например -1001234567890 или 123456789)
        int_ref = int(ref_str)
        return await client.get_entity(int_ref)
    except ValueError:
        # Текстовый идентификатор: @channel, t.me/..., 'me'
        return await client.get_entity(ref_str)
    except Exception as e:
        logging.error(f"Не удалось разрешить чат/канал '{ref_str}': {e}")
        raise


def is_service_message(msg: Message) -> bool:
    """Проверяет, является ли сообщение сервисным (не пересылается)."""
    if isinstance(msg, MessageService):
        return True
    if getattr(msg, "action", None) is not None:
        return True
    return False


async def handle_flood_wait(err: FloodWaitError, max_wait: int, context_desc: str) -> bool:
    """Обрабатывает FloodWaitError с защитой по максимальному времени ожидания.

    Returns:
        True если пауза выдержана и можно повторить; False если ожидание превышает лимит.
    """
    wait_sec = err.seconds
    logging.warning(
        f"⚠ Telegram FloodWait: требуется подождать {wait_sec} сек ({context_desc})"
    )
    if wait_sec > max_wait:
        logging.error(
            f"❌ Время ожидания ({wait_sec}с) превышает допустимый максимум (--max-wait {max_wait}с). "
            f"Процесс прерывается для безопасности аккаунта."
        )
        return False

    logging.info(f"⏳ Ожидание {wait_sec + 1} сек перед повтором...")
    await asyncio.sleep(wait_sec + 1)
    return True


async def forward_batch(
    client: TelegramClient,
    dst,
    src,
    messages_batch: List[Message],
    checkpoint_file: str,
    source_str: str,
    target_str: str,
) -> tuple[int, int]:
    """Пересылает пачку сообщений нативным методом.

    При частичной ошибке пачки выполняет fallback на поштучную отправку.
    Returns:
        (успешно_переслано, ошибок)
    """
    msg_ids = [m.id for m in messages_batch]
    try:
        await client.forward_messages(dst, messages_batch)
        last_id = msg_ids[-1]
        save_checkpoint(
            checkpoint_file,
            {
                "source": source_str,
                "target": target_str,
                "last_msg_id": last_id,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return len(messages_batch), 0
    except Exception as e:
        logging.warning(
            f"Ошибка при батч-пересылке {len(messages_batch)} сообщений: {e}. "
            f"Перехожу на поштучную пересылку для спасения валидных сообщений..."
        )
        batch_success = 0
        batch_errors = 0
        for single_msg in messages_batch:
            try:
                await client.forward_messages(dst, single_msg)
                batch_success += 1
                save_checkpoint(
                    checkpoint_file,
                    {
                        "source": source_str,
                        "target": target_str,
                        "last_msg_id": single_msg.id,
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    },
                )
            except Exception as single_err:
                batch_errors += 1
                logging.error(f"Не удалось переслать msg_id={single_msg.id}: {single_err}")
            await asyncio.sleep(0.5)
        return batch_success, batch_errors


async def forward_all(args):
    """Основная функция сбора и пересылки сообщений."""
    assert TelegramClient is not None

    if not args.source or not args.target:
        logging.error("Необходимо указать --source и --target (в CLI, конфиге или env-переменных)")
        return 2

    # Настройки по умолчанию
    delay = args.delay if args.delay is not None else 1.0
    batch_size = max(1, min(100, args.batch_size or 20))
    checkpoint_file = args.checkpoint or DEFAULT_CHECKPOINT_FILE

    # Возобновление (Resume) из чекпоинта
    min_id = args.min_id
    offset_id = args.offset_id
    if args.resume:
        cp = load_checkpoint(checkpoint_file)
        if cp and "last_msg_id" in cp:
            last_id = cp["last_msg_id"]
            logging.info(f"✓ Возобновление из чекпоинта: последний обработанный msg_id={last_id}")
            if args.reverse:
                # При хронологическом порядке следующий - больше последнего
                min_id = max(min_id or 0, last_id)
            else:
                # При обратном порядке следующий - меньше последнего
                offset_id = last_id
        else:
            logging.warning("Файл чекпоинта не найден или пуст. Начинаю сначала.")

    client = TelegramClient(args.session, args.api_id, args.api_hash)

    try:
        await client.start()
        logging.info("✓ Успешное подключение к Telegram API")

        # Разрешение source и target
        logging.info(f"Разрешаю источник: {args.source}")
        src = await resolve_peer(client, args.source)
        src_name = getattr(src, "title", getattr(src, "username", str(getattr(src, "id", args.source))))
        logging.info(f"✓ Источник: {src_name}")

        logging.info(f"Разрешаю получателя: {args.target}")
        dst = await resolve_peer(client, args.target)
        dst_name = getattr(dst, "title", getattr(dst, "username", str(getattr(dst, "id", args.target))))
        logging.info(f"✓ Получатель: {dst_name}")

        date_from = parse_date_start(args.from_date)
        date_to = parse_date_end(args.to_date)

        if date_from:
            logging.info(f"Фильтр по дате ОТ: {date_from.strftime('%Y-%m-%d %H:%M:%S')} UTC")
        if date_to:
            logging.info(f"Фильтр по дате ДО: {date_to.strftime('%Y-%m-%d %H:%M:%S')} UTC")

        # Оценка общего количества сообщений без лишней итерации
        total_in_chat = None
        try:
            head = await client.get_messages(src, limit=0)
            total_in_chat = head.total
            logging.info(f"Всего сообщений в чате-источнике: {total_in_chat}")
        except Exception as e:
            logging.debug(f"Не удалось получить точное число сообщений: {e}")

        # Параметры выполнения
        logging.info("=" * 60)
        logging.info("ПАРАМЕТРЫ ЗАПУСКА:")
        logging.info(f"  Порядок: {'хронологический (от старых к новым)' if args.reverse else 'обратный (от новых к старым)'}")
        logging.info(f"  Режим: {'копирование контента (--no-forward)' if args.no_forward else f'нативная пересылка (батчи по {batch_size})'}")
        logging.info(f"  Задержка между отправками: {delay:.2f} сек")
        if args.dry_run:
            logging.info("  ⚠ РЕЖИМ DRY-RUN: отправка отключена, только просмотр")
        logging.info("=" * 60)

        count = 0
        success_count = 0
        error_count = 0
        skipped_count = 0
        batch: List[Message] = []

        # Функция сброса текущего батча
        async def flush_batch():
            nonlocal success_count, error_count
            if not batch:
                return True
            s, err = await forward_batch(
                client, dst, src, batch, checkpoint_file, str(args.source), str(args.target)
            )
            success_count += s
            error_count += err
            batch.clear()
            await asyncio.sleep(delay)
            return True

        iter_kwargs = {
            "limit": args.limit,
            "reverse": args.reverse,
        }
        if min_id is not None:
            iter_kwargs["min_id"] = min_id
        if args.max_id is not None:
            iter_kwargs["max_id"] = args.max_id
        if offset_id is not None:
            iter_kwargs["offset_id"] = offset_id

        async for msg in client.iter_messages(src, **iter_kwargs):
            if not isinstance(msg, Message):
                skipped_count += 1
                continue

            # Пропуск сервисных сообщений
            if is_service_message(msg):
                skipped_count += 1
                logging.debug(f"Пропущено сервисное сообщение msg_id={msg.id}")
                continue

            # Фильтрация по датам с оптимизацией раннего выхода (Early Break)
            if msg.date:
                # msg.date всегда UTC
                msg_dt = msg.date if msg.date.tzinfo else msg.date.replace(tzinfo=timezone.utc)

                if date_from and msg_dt < date_from:
                    if not args.reverse:
                        # Идем от новых к старым. Достигнуты сообщения старше date_from.
                        # Все следующие сообщения будут еще старше, прерываем цикл!
                        logging.info("Достигнута нижняя граница даты (--from-date). Завершаю выборку.")
                        break
                    else:
                        skipped_count += 1
                        continue

                if date_to and msg_dt > date_to:
                    if args.reverse:
                        # Идем от старых к новым. Достигнуты сообщения новее date_to.
                        # Все следующие сообщения будут еще новее, прерываем цикл!
                        logging.info("Достигнута верхняя граница даты (--to-date). Завершаю выборку.")
                        break
                    else:
                        skipped_count += 1
                        continue

            count += 1
            preview = (msg.message or "").replace("\n", " ")[:60]
            if not preview and msg.media:
                preview = f"<{msg.media.__class__.__name__}>"

            if args.dry_run:
                logging.info(
                    f"[{count}] DRY-RUN: msg_id={msg.id} date={msg.date} {preview!r}"
                )
                success_count += 1
                continue

            # Режим 1: Копирование контента (--no-forward)
            if args.no_forward:
                sent = False
                while not sent:
                    try:
                        if msg.media:
                            await client.send_file(dst, msg.media, caption=msg.message or "")
                        elif msg.message:
                            await client.send_message(dst, msg.message)
                        else:
                            skipped_count += 1
                            break

                        sent = True
                        success_count += 1
                        save_checkpoint(
                            checkpoint_file,
                            {
                                "source": str(args.source),
                                "target": str(args.target),
                                "last_msg_id": msg.id,
                                "updated_at": datetime.now(timezone.utc).isoformat(),
                            },
                        )
                        await asyncio.sleep(delay)

                    except FloodWaitError as e:
                        can_continue = await handle_flood_wait(e, args.max_wait, f"msg_id={msg.id}")
                        if not can_continue:
                            return 1
                    except ChatWriteForbiddenError as e:
                        error_count += 1
                        logging.error(f"Нет прав на запись в целевой чат: {e}")
                        return 1
                    except Exception as e:
                        error_count += 1
                        logging.error(f"Ошибка при копировании msg_id={msg.id}: {e}")
                        break

            # Режим 2: Нативная пересылка с батчингом
            else:
                batch.append(msg)
                if len(batch) >= batch_size:
                    while True:
                        try:
                            await flush_batch()
                            break
                        except FloodWaitError as e:
                            can_continue = await handle_flood_wait(e, args.max_wait, "batch forward")
                            if not can_continue:
                                return 1

            if count % 20 == 0:
                logging.info(
                    f"📊 Прогресс: обработано {count} сообщений | "
                    f"✓ Успешно: {success_count}, ✗ Ошибок: {error_count}, ⊘ Пропущено: {skipped_count}"
                )

        # Сброс оставшихся сообщений в батче
        if batch and not args.dry_run:
            while True:
                try:
                    await flush_batch()
                    break
                except FloodWaitError as e:
                    can_continue = await handle_flood_wait(e, args.max_wait, "final batch")
                    if not can_continue:
                        return 1

        # Итоговая статистика
        logging.info("=" * 60)
        logging.info("ИТОГОВАЯ СТАТИСТИКА ПЕРЕСЫЛКИ:")
        logging.info(f"  Всего сообщений обработано: {count}")
        logging.info(f"  ✓ Успешно отправлено:       {success_count}")
        logging.info(f"  ✗ Ошибок отправки:          {error_count}")
        logging.info(f"  ⊘ Пропущено (фильтры/инфо): {skipped_count}")
        logging.info("=" * 60)

        return 0 if error_count == 0 else 1

    except Exception as e:
        logging.error(f"Критическая ошибка: {e}", exc_info=True)
        return 1
    finally:
        await client.disconnect()
        logging.info("Сессия Telegram корректно отключена")


async def main_async(args):
    if args.self_test:
        logging.info("Self-test telethon_forward: проверка импортов и завершение")
        if _telethon_import_error:
            logging.error(f"Ошибка импорта Telethon: {_telethon_import_error}")
            return 1
        logging.info("✓ Импорты Telethon успешны. Self-test пройден.")
        return 0

    if _telethon_import_error:
        logging.error(
            f"Telethon не установлен или произошла ошибка импорта: {_telethon_import_error}\n"
            f"Установите зависимости: pip install -r requirements.txt"
        )
        return 1

    # Загрузка конфигурации из TOML
    cfg = {}
    if args.config and os.path.exists(args.config):
        if tomllib is None:
            logging.warning(
                f"Модуль tomllib/tomli недоступен. Конфиг {args.config} пропущен. "
                f"Установите: pip install tomli"
            )
        else:
            try:
                with open(args.config, "rb") as f:
                    cfg = tomllib.load(f) or {}
                logging.info(f"✓ Загружен конфиг: {args.config}")
            except Exception as e:
                logging.warning(f"Не удалось прочитать {args.config}: {e}")

    # Чтение из параметров CLI > переменных окружения > TOML
    def resolve_val(cli_val, env_var, cfg_key, default=None):
        if cli_val is not None:
            return cli_val
        if env_var in os.environ and os.environ[env_var].strip():
            return os.environ[env_var].strip()
        if cfg_key in cfg:
            return cfg[cfg_key]
        return default

    args.api_id = resolve_val(args.api_id, "TG_API_ID", "api_id")
    if args.api_id is not None:
        try:
            args.api_id = int(args.api_id)
        except ValueError:
            pass

    args.api_hash = resolve_val(args.api_hash, "TG_API_HASH", "api_hash")
    args.session = resolve_val(args.session, "TG_SESSION", "session", "forward_session")
    args.source = resolve_val(args.source, "TG_SOURCE", "source")
    args.target = resolve_val(args.target, "TG_TARGET", "target")
    args.limit = resolve_val(args.limit, "TG_LIMIT", "limit")
    if args.limit is not None:
        try:
            args.limit = int(args.limit)
        except ValueError:
            pass

    args.from_date = resolve_val(args.from_date, "TG_FROM_DATE", "from_date")
    args.to_date = resolve_val(args.to_date, "TG_TO_DATE", "to_date")
    args.delay = resolve_val(args.delay, "TG_DELAY", "delay", 1.0)
    if args.delay is not None:
        args.delay = float(args.delay)

    args.batch_size = resolve_val(args.batch_size, "TG_BATCH_SIZE", "batch_size", 20)
    if args.batch_size is not None:
        args.batch_size = int(args.batch_size)

    args.reverse = bool(args.reverse or os.environ.get("TG_REVERSE") == "1" or cfg.get("reverse", False))
    args.no_forward = bool(args.no_forward or os.environ.get("TG_NO_FORWARD") == "1" or cfg.get("no_forward", False))
    args.dry_run = bool(args.dry_run or os.environ.get("TG_DRY_RUN") == "1" or cfg.get("dry_run", False))

    if not args.api_id or not args.api_hash:
        logging.error(
            "Не указаны api_id и api_hash! Задайте их через:\n"
            "  - аргументы: --api-id <ID> --api-hash <HASH>\n"
            "  - переменные окружения: TG_API_ID, TG_API_HASH\n"
            "  - файл telethon_config.toml"
        )
        return 2

    return await forward_all(args)


def main():
    args = parse_args()
    setup_logging(args.log)
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        logging.info("Остановка пользователем (Ctrl+C)")
        return 130


if __name__ == "__main__":
    sys.exit(main())
