#!/usr/bin/env python3
"""Автоматический скриншотер для длинных чатов Telegram.

Позволяет делать серию скриншотов выбранной области окна Telegram
с автоматической прокруткой, защитой от дубликатов и определением конца чата.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import threading
import time
from datetime import datetime
from typing import Optional, Tuple

try:
    import pyautogui
    from PIL import Image, ImageChops
    import imagehash
except Exception as e:
    pyautogui = None
    Image = None
    ImageChops = None
    imagehash = None
    _import_error = e
else:
    _import_error = None

try:
    from pynput import keyboard
except Exception as e:
    keyboard = None
    _pynput_error = e
else:
    _pynput_error = None


class CaptureController:
    """Контроллер для захвата скриншотов и автопрокрутки."""

    def __init__(
        self,
        output_dir: str,
        scroll_amount: int = 1000,
        interval: float = 0.6,
        settle_time: float = 0.15,
        hash_threshold: int = 1,
        patience: int = 3,
        extra_retries: int = 2,
        retry_factor: float = 0.4,
        bottom_wait: float = 0.4,
    ):
        """Инициализирует контроллер захвата.

        Args:
            output_dir: Директория для сохранения скриншотов
            scroll_amount: Величина прокрутки за шаг
            interval: Пауза между шагами в секундах
            settle_time: Время ожидания затухания анимации прокрутки
            hash_threshold: Максимальная разница perceptual hash для признания кадров схожими (0-2)
            patience: Количество шагов без изменений до остановки
            extra_retries: Количество попыток микропрокрутки перед объявлением конца чата
            retry_factor: Коэффициент уменьшения прокрутки при retry
            bottom_wait: Пауза перед дополнительными микропрокрутками
        """
        self.output_dir = output_dir
        self.scroll_amount = scroll_amount
        self.interval = interval
        self.settle_time = settle_time
        self.hash_threshold = hash_threshold
        self.patience = patience
        self.extra_retries = extra_retries
        self.retry_factor = retry_factor
        self.bottom_wait = bottom_wait

        self._tl: Optional[Tuple[int, int]] = None  # Верхний левый угол (x, y)
        self._br: Optional[Tuple[int, int]] = None  # Нижний правый угол (x, y)
        self._roi: Optional[Tuple[int, int, int, int]] = None  # (left, top, width, height)

        self._stop_event = threading.Event()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._capture_count = 0
        self._start_time: Optional[float] = None
        self._saved_cursor_pos: Optional[Tuple[int, int]] = None

        os.makedirs(self.output_dir, exist_ok=True)
        logging.info(f"Директория для сохранения скриншотов: {os.path.abspath(self.output_dir)}")

    def _require_dependencies(self):
        """Проверяет наличие необходимых зависимостей."""
        if pyautogui is None or Image is None or imagehash is None:
            raise RuntimeError(
                f"Не установлены необходимые зависимости: {repr(_import_error)}\n"
                f"Установите их командой: pip install -r requirements.txt"
            )

    def set_top_left(self):
        """Устанавливает верхний левый угол области захвата по текущему положению курсора."""
        self._require_dependencies()
        x, y = pyautogui.position()
        self._tl = (int(x), int(y))
        logging.info(f"✓ Точка [1] (верхний левый угол): {self._tl}")
        self._update_roi_if_ready()

    def set_bottom_right(self):
        """Устанавливает нижний правый угол области захвата по текущему положению курсора."""
        self._require_dependencies()
        x, y = pyautogui.position()
        self._br = (int(x), int(y))
        logging.info(f"✓ Точка [2] (нижний правый угол): {self._br}")
        self._update_roi_if_ready()

    def _update_roi_if_ready(self):
        """Обновляет область захвата (ROI), если обе точки заданы."""
        if self._tl and self._br:
            x1, y1 = self._tl
            x2, y2 = self._br
            left, top = min(x1, x2), min(y1, y2)
            width, height = abs(x2 - x1), abs(y2 - y1)

            if width < 30 or height < 30:
                logging.warning(
                    f"⚠ Слишком маленькая область ({width}x{height}px). "
                    f"Минимум 30x30px. Выберите точки заново."
                )
                return

            self._roi = (left, top, width, height)
            logging.info(
                f"✓ Область захвата (ROI) зафиксирована:\n"
                f"  Координаты: ({left}, {top})\n"
                f"  Размер: {width}x{height}px\n"
                f"  Готово к старту! Нажмите [G] для запуска или [Q] для отмены."
            )

    def print_status(self):
        """Выводит текущий статус области захвата."""
        if self._roi:
            l, t, w, h = self._roi
            logging.info(f"Область захвата активна: x={l}, y={t}, w={w}, h={h}")
        else:
            logging.info(
                f"Точки: точка [1]={self._tl or 'не задана'}, точка [2]={self._br or 'не задана'}"
            )

    def _grab_roi_image(self) -> Image.Image:
        """Делает скриншот экрана с учетом масштабирования Retina/HiDPI и вырезает ROI."""
        assert self._roi is not None
        left, top, width, height = self._roi

        # Получаем логический размер экрана и физический скриншот
        screen_w, screen_h = pyautogui.size()
        full_img = pyautogui.screenshot()

        # Вычисляем фактор масштабирования (Retina = 2x, обычный = 1x)
        scale_x = full_img.width / screen_w if screen_w > 0 else 1.0
        scale_y = full_img.height / screen_h if screen_h > 0 else 1.0

        if abs(scale_x - 1.0) > 0.05 or abs(scale_y - 1.0) > 0.05:
            # Retina / HiDPI масштабирование координат
            crop_box = (
                int(round(left * scale_x)),
                int(round(top * scale_y)),
                int(round((left + width) * scale_x)),
                int(round((top + height) * scale_y)),
            )
        else:
            crop_box = (left, top, left + width, top + height)

        return full_img.crop(crop_box)

    def _save_frame(self, img: Image.Image, idx: int) -> str:
        """Сохраняет изображение кадра на диск."""
        filename = f"frame_{idx:05d}.png"
        filepath = os.path.join(self.output_dir, filename)
        img.save(filepath)
        self._capture_count += 1
        logging.debug(f"📸 Сохранен кадр: {filename}")
        return filepath

    def _scroll_at_roi(self, amount: Optional[int] = None):
        """Прокручивает окно внутри области захвата без дергания курсора по экрану."""
        assert self._roi is not None
        left, top, width, height = self._roi

        # Позиционируем курсор в правой нижней части области чата (где нет текста и кнопок реакций)
        scroll_x = left + int(width * 0.90)
        scroll_y = top + int(height * 0.70)

        amt = abs(self.scroll_amount) if amount is None else abs(int(amount))

        try:
            # Проверяем положение курсора: если он уже близко к точке скролла, не дергаем его
            cur_x, cur_y = pyautogui.position()
            if abs(cur_x - scroll_x) > 20 or abs(cur_y - scroll_y) > 20:
                pyautogui.moveTo(scroll_x, scroll_y, duration=0.0)

            pyautogui.scroll(-amt)
            if self.settle_time > 0:
                time.sleep(self.settle_time)
        except Exception as e:
            logging.error(f"Ошибка при прокрутке: {e}")

    def _images_similar(self, h1, h2) -> bool:
        """Сравнивает два кадра по perceptual hash."""
        return (h1 - h2) <= self.hash_threshold

    def start(self):
        """Запускает процесс автоскриншотов в фоновом потоке."""
        if self._running:
            logging.info("⚠ Процесс захвата уже выполняется")
            return
        if not self._roi:
            logging.error(
                "⚠ Сначала задайте область захвата:\n"
                "  1. Наведите мышь на верхний левый угол списка сообщений и нажмите [1]\n"
                "  2. Наведите мышь на нижний правый угол и нажмите [2]"
            )
            return

        self._stop_event.clear()
        self._running = True
        self._capture_count = 0
        self._start_time = time.time()

        try:
            cur_pos = pyautogui.position()
            self._saved_cursor_pos = (int(cur_pos.x), int(cur_pos.y))
        except Exception:
            self._saved_cursor_pos = None

        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

        logging.info("=" * 60)
        logging.info("🚀 ЗАПУСК АВТОСКРИНШОТОВ")
        logging.info(f"  Папка сохранения: {self.output_dir}")
        logging.info(f"  Шаг прокрутки:    {self.scroll_amount} px")
        logging.info(f"  Интервал шага:    {self.interval} сек")
        logging.info("=" * 60)

    def stop(self):
        """Останавливает процесс автоскриншотов."""
        if not self._running:
            logging.info("⚠ Процесс захвата не активен")
            return

        logging.info("⏸ Остановка захвата...")
        self._stop_event.set()

        if self._thread and self._thread != threading.current_thread():
            self._thread.join(timeout=3.0)

        # Возвращаем курсор на исходное место
        if self._saved_cursor_pos:
            try:
                pyautogui.moveTo(self._saved_cursor_pos[0], self._saved_cursor_pos[1], duration=0.1)
            except Exception:
                pass

        self._running = False
        duration = round(time.time() - self._start_time, 1) if self._start_time else 0.0

        # Сохраняем метаданные запуска
        self._save_metadata(duration)

        logging.info("=" * 60)
        logging.info("✓ ЗАХВАТ УСПЕШНО ЗАВЕРШЕН")
        logging.info(f"  Всего уникальных кадров: {self._capture_count}")
        logging.info(f"  Время работы:            {duration} сек")
        logging.info(f"  Папка с файлами:         {os.path.abspath(self.output_dir)}")
        logging.info("=" * 60)

    def _save_metadata(self, duration: float):
        """Сохраняет файл metadata.json в папку со скриншотами."""
        meta_file = os.path.join(self.output_dir, "metadata.json")
        try:
            data = {
                "created_at": datetime.now().isoformat(),
                "duration_seconds": duration,
                "total_frames": self._capture_count,
                "roi": {
                    "left": self._roi[0],
                    "top": self._roi[1],
                    "width": self._roi[2],
                    "height": self._roi[3],
                } if self._roi else None,
                "scroll_amount": self.scroll_amount,
                "interval": self.interval,
                "hash_threshold": self.hash_threshold,
                "patience": self.patience,
            }
            with open(meta_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logging.debug(f"Не удалось записать metadata.json: {e}")

    def _run_loop(self):
        """Основной цикл захвата, прокрутки и определения конца чата."""
        self._require_dependencies()
        frame_idx = 0
        no_change = 0

        try:
            # Захват и сохранение начального кадра
            logging.info("📸 Захват начального кадра...")
            img = self._grab_roi_image()
            self._save_frame(img, frame_idx)
            last_hash = imagehash.phash(img)
            frame_idx += 1

            while not self._stop_event.is_set():
                # Прокручиваем чат вниз
                self._scroll_at_roi()
                time.sleep(self.interval)

                if self._stop_event.is_set():
                    break

                # Делаем снимок в памяти (без сохранения на диск до проверки)
                new_img = self._grab_roi_image()
                new_hash = imagehash.phash(new_img)

                if self._images_similar(new_hash, last_hash):
                    no_change += 1
                    logging.info(
                        f"⏸ Кадр не изменился ({no_change}/{self.patience}) — "
                        f"возможно, достигнут конец чата"
                    )

                    # Дополнительные микропрокрутки для исключения залипания
                    retry_success = False
                    if self.extra_retries > 0:
                        time.sleep(self.bottom_wait)
                        for r in range(1, self.extra_retries + 1):
                            if self._stop_event.is_set():
                                break

                            reduced = max(1, int(self.scroll_amount * (self.retry_factor ** r)))
                            logging.debug(f"  Микропрокрутка #{r}: {reduced}px")
                            self._scroll_at_roi(reduced)
                            time.sleep(self.interval)

                            retry_img = self._grab_roi_image()
                            retry_hash = imagehash.phash(retry_img)

                            if not self._images_similar(retry_hash, last_hash):
                                logging.info("✓ Обнаружено новое содержимое после микропрокрутки")
                                self._save_frame(retry_img, frame_idx)
                                frame_idx += 1
                                last_hash = retry_hash
                                no_change = 0
                                retry_success = True
                                break

                    if not retry_success:
                        if no_change >= self.patience:
                            logging.info(
                                f"✓ Достигнут конец чата ({no_change} проверок без изменений). "
                                f"Автоматическая остановка."
                            )
                            break
                else:
                    # Кадр изменился — сохраняем его на диск
                    self._save_frame(new_img, frame_idx)
                    frame_idx += 1
                    last_hash = new_hash
                    no_change = 0

                    if frame_idx % 20 == 0:
                        logging.info(f"📊 Сохранено кадров: {self._capture_count}")

        except Exception as e:
            logging.error(f"Критическая ошибка в процессе захвата: {e}", exc_info=True)
        finally:
            self._running = False


def parse_args():
    parser = argparse.ArgumentParser(
        description="Авто-скриншотер для длинных чатов Telegram с прокруткой и распознаванием конца"
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Папка для сохранения скриншотов. По умолчанию: screenshots/<timestamp>",
    )
    parser.add_argument(
        "--label",
        default=None,
        help="Метка для имени подпапки: screenshots/<timestamp>_<label>",
    )
    parser.add_argument(
        "--scroll",
        type=int,
        default=1000,
        help="Величина прокрутки за шаг в пикселях (по умолчанию 1000)",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=0.6,
        help="Пауза между шагами в секундах (по умолчанию 0.6)",
    )
    parser.add_argument(
        "--settle-time",
        type=float,
        default=0.15,
        help="Пауза после прокрутки для затухания анимации (по умолчанию 0.15)",
    )
    parser.add_argument(
        "--hash-threshold",
        type=int,
        default=1,
        help="Порог различия perceptual hash (0-2, по умолчанию 1)",
    )
    parser.add_argument(
        "--patience",
        type=int,
        default=3,
        help="Сколько шагов без изменений до остановки (по умолчанию 3)",
    )
    parser.add_argument(
        "--extra-retries",
        type=int,
        default=2,
        help="Количество попыток микропрокрутки перед остановкой (по умолчанию 2)",
    )
    parser.add_argument(
        "--retry-factor",
        type=float,
        default=0.4,
        help="Коэффициент уменьшения шага микропрокрутки (по умолчанию 0.4)",
    )
    parser.add_argument(
        "--bottom-wait",
        type=float,
        default=0.4,
        help="Пауза перед микропрокрутками (по умолчанию 0.4)",
    )
    parser.add_argument(
        "--stdin-controls",
        action="store_true",
        help="Управление через ввод в терминале (1, 2, g, q, x) вместо глобальных хоткеев",
    )
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Самопроверка окружения и импорта модулей без выполнения действий",
    )
    parser.add_argument(
        "--log",
        default="INFO",
        help="Уровень логирования: DEBUG / INFO / WARNING",
    )
    return parser.parse_args()


def setup_logging(level: str):
    lvl = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=lvl,
        format="[%(asctime)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def print_stdin_help():
    print(
        "\nКоманды управления через терминал:\n"
        "  1 — зафиксировать верхний левый угол по текущему курсору\n"
        "  2 — зафиксировать нижний правый угол по текущему курсору\n"
        "  r — показать статус и координаты области\n"
        "  g — запустить захват и автопрокрутку\n"
        "  q — остановить захват\n"
        "  x — выйти из программы\n"
        "  h — показать эту подсказку\n"
    )


def main():
    args = parse_args()
    setup_logging(args.log)

    if args.self_test:
        logging.info("Режим self-test: проверка импорта зависимостей")
        has_errors = False
        if _import_error:
            logging.error(f"pyautogui / PIL / imagehash: {_import_error}")
            has_errors = True
        else:
            logging.info("✓ Библиотеки pyautogui, PIL, imagehash доступны")

        if _pynput_error:
            logging.warning(f"pynput: {_pynput_error} (глобальные хоткеи недоступны, доступен --stdin-controls)")
        else:
            logging.info("✓ Библиотека pynput доступна")

        return 1 if has_errors else 0

    if _import_error:
        logging.error(
            f"Не установлены необходимые зависимости: {_import_error}\n"
            f"Установите их: pip install -r requirements.txt"
        )
        return 1

    # Подготовка папки сохранения
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if args.out:
        out_dir = args.out
    else:
        label = None
        if args.label:
            cleaned = re.sub(r"\s+", "_", str(args.label))
            cleaned = re.sub(r"[^A-Za-z0-9_-]", "", cleaned)
            if cleaned:
                label = cleaned
        folder_name = f"{timestamp}_{label}" if label else timestamp
        out_dir = os.path.join("screenshots", folder_name)

    controller = CaptureController(
        output_dir=out_dir,
        scroll_amount=args.scroll,
        interval=args.interval,
        settle_time=args.settle_time,
        hash_threshold=args.hash_threshold,
        patience=args.patience,
        extra_retries=args.extra_retries,
        retry_factor=args.retry_factor,
        bottom_wait=args.bottom_wait,
    )

    logging.info("Инструкция:")
    logging.info("  1. Откройте Telegram и прокрутите нужный чат в самое начало (верх).")
    logging.info("  2. Задайте верхний левый [1] и нижний правый [2] угол чата.")

    # Режим управления через терминал (stdin)
    if args.stdin_controls or keyboard is None:
        if keyboard is None and not args.stdin_controls:
            logging.warning("pynput недоступен, автоматически активирован режим --stdin-controls")

        print_stdin_help()
        try:
            while True:
                try:
                    cmd = input("cmd [1/2/g/q/r/x/h]> ").strip().lower()
                except EOFError:
                    break

                if cmd == "1":
                    controller.set_top_left()
                elif cmd == "2":
                    controller.set_bottom_right()
                elif cmd == "g":
                    controller.start()
                elif cmd == "q":
                    controller.stop()
                elif cmd == "r":
                    controller.print_status()
                elif cmd == "h" or cmd == "?":
                    print_stdin_help()
                elif cmd == "x":
                    break
        except KeyboardInterrupt:
            logging.info("Выход по Ctrl+C")
        finally:
            controller.stop()

    # Режим управления глобальными горячими клавишами
    else:
        logging.info("Горячие клавиши:")
        logging.info("  macOS:   Cmd+Option+1 / Cmd+Option+2 — выбор углов, Cmd+Option+G — старт, Cmd+Option+Q — стоп")
        logging.info("  Windows: Ctrl+Alt+1   / Ctrl+Alt+2   — выбор углов, Ctrl+Alt+G   — старт, Ctrl+Alt+Q   — стоп")
        logging.info("  (Если хоткеи не срабатывают из-за прав macOS, запустите с флагом --stdin-controls)")

        hotkeys = {
            # Windows / Linux
            "<ctrl>+<alt>+1": controller.set_top_left,
            "<ctrl>+<alt>+2": controller.set_bottom_right,
            "<ctrl>+<alt>+g": controller.start,
            "<ctrl>+<alt>+q": controller.stop,
            # macOS: Cmd+Option
            "<cmd>+<alt>+1": controller.set_top_left,
            "<cmd>+<alt>+2": controller.set_bottom_right,
            "<cmd>+<alt>+g": controller.start,
            "<cmd>+<alt>+q": controller.stop,
            # macOS fallback: Cmd+Ctrl
            "<cmd>+<ctrl>+1": controller.set_top_left,
            "<cmd>+<ctrl>+2": controller.set_bottom_right,
            "<cmd>+<ctrl>+g": controller.start,
            "<cmd>+<ctrl>+q": controller.stop,
        }

        try:
            with keyboard.GlobalHotKeys(hotkeys) as h:
                h.join()
        except KeyboardInterrupt:
            logging.info("Остановка по Ctrl+C")
        finally:
            controller.stop()

    return 0


if __name__ == "__main__":
    sys.exit(main())
