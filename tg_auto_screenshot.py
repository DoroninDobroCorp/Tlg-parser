#!/usr/bin/env python3
from __future__ import annotations
import argparse
import os
import sys
import time
import threading
import logging
import re
from datetime import datetime
from typing import Optional, Tuple

try:
    import pyautogui
    from PIL import Image
    from PIL import ImageChops
    import imagehash
except Exception as e:
    # Позволяет запустить в режиме self-test без установленных зависимостей
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
    def __init__(self,
                 output_dir: str,
                 scroll_amount: int = 800,
                 interval: float = 0.6,
                 hash_threshold: int = 1,
                 patience: int = 3,
                 extra_retries: int = 2,
                 retry_factor: float = 0.4,
                 bottom_wait: float = 0.4):
        self.output_dir = output_dir
        self.scroll_amount = scroll_amount
        self.interval = interval
        self.hash_threshold = hash_threshold
        self.patience = patience
        self.extra_retries = extra_retries
        self.retry_factor = retry_factor
        self.bottom_wait = bottom_wait

        self._tl: Optional[Tuple[int, int]] = None
        self._br: Optional[Tuple[int, int]] = None
        self._roi: Optional[Tuple[int, int, int, int]] = None  # left, top, width, height

        self._stop_event = threading.Event()
        self._running = False
        self._thread: Optional[threading.Thread] = None

        os.makedirs(self.output_dir, exist_ok=True)

    def _require_pyauto(self):
        if pyautogui is None or Image is None or imagehash is None:
            raise RuntimeError(f"Не установлены зависимости: {repr(_import_error)}")

    def set_top_left(self):
        self._require_pyauto()
        x, y = pyautogui.position()
        self._tl = (int(x), int(y))
        logging.info(f"Точка верхний-левый: {self._tl}")
        self._update_roi_if_ready()

    def set_bottom_right(self):
        self._require_pyauto()
        x, y = pyautogui.position()
        self._br = (int(x), int(y))
        logging.info(f"Точка нижний-правый: {self._br}")
        self._update_roi_if_ready()

    def _update_roi_if_ready(self):
        if self._tl and self._br:
            x1, y1 = self._tl
            x2, y2 = self._br
            left, top = min(x1, x2), min(y1, y2)
            width, height = abs(x2 - x1), abs(y2 - y1)
            if width < 20 or height < 20:
                logging.warning("Слишком маленькая область. Выберите заново.")
                return
            self._roi = (left, top, width, height)
            logging.info(f"Выбрана область ROI: left={left}, top={top}, width={width}, height={height}")

    def start(self):
        if self._running:
            logging.info("Уже запущено")
            return
        if not self._roi:
            logging.error("Сначала задайте область (Ctrl+Alt+1 затем Ctrl+Alt+2 над окном Telegram)")
            return
        self._stop_event.clear()
        self._running = True
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logging.info("Старт захвата скриншотов...")

    def stop(self):
        if not self._running:
            logging.info("Не запущено")
            return
        self._stop_event.set()
        if self._thread:
            try:
                self._thread.join(timeout=5)
            except KeyboardInterrupt:
                # Игнорируем, завершаем как есть
                pass
        self._running = False
        logging.info("Остановлено")

    def _capture_once(self, idx: int) -> Image.Image:
        assert self._roi is not None
        left, top, width, height = self._roi
        img = pyautogui.screenshot(region=(left, top, width, height))
        filepath = os.path.join(self.output_dir, f"frame_{idx:05d}.png")
        img.save(filepath)
        logging.debug(f"Сохранено: {filepath}")
        return img

    def _scroll_down(self, amount: Optional[int] = None):
        assert self._roi is not None
        left, top, width, height = self._roi
        cx = left + width // 2
        cy = top + int(height * 0.8)
        current_pos = pyautogui.position()
        try:
            pyautogui.moveTo(cx, cy, duration=0.05)
            amt = abs(self.scroll_amount) if amount is None else abs(int(amount))
            pyautogui.scroll(-amt)
        finally:
            pyautogui.moveTo(current_pos.x, current_pos.y, duration=0.0)

    def _images_similar(self, h1, h2) -> bool:
        # Считаем одинаковыми, если расстояние Хэмминга <= threshold
        return h1 - h2 <= self.hash_threshold

    def _run_loop(self):
        self._require_pyauto()
        idx = 0
        last_hash = None
        no_change = 0

        # Первичный кадр
        img = self._capture_once(idx)
        last_hash = imagehash.phash(img)
        idx += 1

        while not self._stop_event.is_set():
            # Основной скролл и снимок
            self._scroll_down()
            time.sleep(self.interval)
            img = self._capture_once(idx)
            h = imagehash.phash(img)

            if self._images_similar(h, last_hash):
                no_change += 1
                logging.info(f"Нет изменений ({no_change}/{self.patience}) — возможно достигнут конец чата")

                # Попробуем дополнительные мелкие прокрутки, вдруг контент подгружается или упёрлись в низ
                retry_success = False
                if self.extra_retries > 0:
                    time.sleep(self.bottom_wait)
                    for r in range(1, self.extra_retries + 1):
                        reduced = max(1, int(self.scroll_amount * (self.retry_factor ** r)))
                        logging.debug(f"Доп. попытка прокрутки #{r}: {reduced} пикс")
                        self._scroll_down(reduced)
                        time.sleep(self.interval)
                        img_retry = self._capture_once(idx)
                        h_retry = imagehash.phash(img_retry)
                        idx += 1
                        if not self._images_similar(h_retry, last_hash):
                            logging.info("Найдены изменения после доп. прокрутки — продолжаю")
                            no_change = 0
                            last_hash = h_retry
                            retry_success = True
                            break
                if not retry_success:
                    # Если все доп. попытки безрезультатны — считаем низ достигнут
                    if no_change >= self.patience:
                        logging.info("Достигнут конец: доп. прокрутки изменений не дали. Останавливаюсь.")
                        break
            else:
                no_change = 0
                last_hash = h
                idx += 1

        self._running = False


def parse_args():
    parser = argparse.ArgumentParser(description="Авто-скриншотер для длинных чатов Telegram")
    parser.add_argument("--out", default=None, help="Папка для сохранения скринов. По умолчанию screenshots/<timestamp>")
    parser.add_argument("--label", default=None, help="Необязательная метка для подпапки: screenshots/<timestamp>_<label>")
    parser.add_argument("--scroll", type=int, default=5600, help="Величина прокрутки за шаг (пиксели колеса)")
    parser.add_argument("--interval", type=float, default=0.6, help="Задержка между шагами (сек)")
    parser.add_argument("--hash-threshold", type=int, default=1, help="Порог близости кадров по pHash (0-2)")
    parser.add_argument("--patience", type=int, default=3, help="Сколько раз подряд без изменений до остановки")
    parser.add_argument("--extra-retries", type=int, default=2, help="Сколько попыток доп. микропрокрутки при отсутствии изменений")
    parser.add_argument("--retry-factor", type=float, default=0.4, help="Коэффициент уменьшения шага на доп. попытках (0..1)")
    parser.add_argument("--bottom-wait", type=float, default=0.4, help="Пауза перед доп. прокрутками при подозрении на конец")
    parser.add_argument("--stdin-controls", action="store_true", help="Управление из терминала: 1,2,g,q,x вместо глобальных хоткеев")
    parser.add_argument("--self-test", action="store_true", help="Запуск без глобальных хоткеев и без действий — проверка окружения")
    parser.add_argument("--duration", type=int, default=10, help="Длительность self-test в секундах")
    parser.add_argument("--log", default="INFO", help="Уровень логирования: DEBUG, INFO, WARNING")
    return parser.parse_args()


def setup_logging(level: str):
    lvl = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(
        level=lvl,
        format="[%(asctime)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def main():
    args = parse_args()
    setup_logging(args.log)

    if args.self_test:
        logging.info("Режим self-test: проверка импорта зависимостей и ожидание без действий")
        if _import_error:
            logging.warning(f"pyautogui/PIL/imagehash: {_import_error}")
        if _pynput_error:
            logging.warning(f"pynput: {_pynput_error}")
        time.sleep(max(1, int(args.duration)))
        logging.info("Self-test завершён")
        return 0

    if _import_error:
        logging.error(f"Ошибка импорта зависимостей: {_import_error}")
        return 1
    if keyboard is None:
        logging.error(f"Ошибка импорта pynput: {_pynput_error}")
        return 1

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    if args.out:
        out_dir = args.out
    else:
        label = None
        if args.label:
            # Оставляем только безопасные символы: буквы/цифры/_/-; пробелы -> _
            cleaned = re.sub(r"\s+", "_", str(args.label))
            cleaned = re.sub(r"[^A-Za-z0-9_-]", "", cleaned)
            if cleaned:
                label = cleaned
        folder_name = f"{timestamp}_{label}" if label else timestamp
        out_dir = os.path.join("screenshots", folder_name)
    controller = CaptureController(
        out_dir,
        scroll_amount=args.scroll,
        interval=args.interval,
        hash_threshold=args.hash_threshold,
        patience=args.patience,
        extra_retries=args.extra_retries,
        retry_factor=args.retry_factor,
        bottom_wait=args.bottom_wait,
    )

    logging.info("Подсказка: сначала откройте окно Telegram и прокрутите чат в начало (самый верх)")

    if args.stdin_controls:
        logging.info("Режим управления из терминала включен (--stdin-controls):")
        logging.info("  [1] — верхний левый угол, [2] — нижний правый")
        logging.info("  [g] — старт, [q] — стоп, [x] — выход")
        try:
            while True:
                cmd = input("[1/2/g/q/x]> ").strip().lower()
                if cmd == '1':
                    controller.set_top_left()
                elif cmd == '2':
                    controller.set_bottom_right()
                elif cmd == 'g':
                    controller.start()
                elif cmd == 'q':
                    controller.stop()
                elif cmd == 'x':
                    break
        except KeyboardInterrupt:
            logging.info("Завершение по Ctrl+C")
        finally:
            controller.stop()
    else:
        logging.info("Горячие клавиши:")
        logging.info("  Ctrl+Alt+1/2/G/Q — Windows/Linux")
        logging.info("  Cmd+Option+1/2/G/Q — macOS (вариант 1)")
        logging.info("  Cmd+Ctrl+1/2/G/Q — macOS (вариант 2, если вариант 1 конфликтует)")

        # Поддерживаем и Ctrl+Alt (Win/Linux), и Cmd+Option (macOS)
        hotkeys = {
            '<ctrl>+<alt>+1': controller.set_top_left,
            '<ctrl>+<alt>+2': controller.set_bottom_right,
            '<ctrl>+<alt>+g': controller.start,
            '<ctrl>+<alt>+q': controller.stop,
            '<cmd>+<alt>+1': controller.set_top_left,     # macOS: Cmd+Option+1
            '<cmd>+<alt>+2': controller.set_bottom_right,  # macOS: Cmd+Option+2
            '<cmd>+<alt>+g': controller.start,             # macOS: Cmd+Option+G
            '<cmd>+<alt>+q': controller.stop,              # macOS: Cmd+Option+Q
            '<cmd>+<ctrl>+1': controller.set_top_left,     # macOS: Cmd+Ctrl+1 (fallback)
            '<cmd>+<ctrl>+2': controller.set_bottom_right, # macOS: Cmd+Ctrl+2 (fallback)
            '<cmd>+<ctrl>+g': controller.start,            # macOS: Cmd+Ctrl+G (fallback)
            '<cmd>+<ctrl>+q': controller.stop,             # macOS: Cmd+Ctrl+Q (fallback)
        }

        try:
            with keyboard.GlobalHotKeys(hotkeys) as h:
                h.join()
        except KeyboardInterrupt:
            logging.info("Завершение по Ctrl+C")
        finally:
            controller.stop()

    return 0


if __name__ == "__main__":
    sys.exit(main())
