"""TikTok screen observer with explicitly armed, demo-only MT5 copying."""

from __future__ import annotations

import ctypes
from datetime import datetime
import json
import os
from pathlib import Path
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox

try:
    from copy_execution import CopyExecution
except ImportError:
    CopyExecution = None

try:
    from copy_ipc import read_observer_command, write_observer_state
except ImportError:
    read_observer_command = write_observer_state = None

try:
    import mss
except ImportError:
    mss = None

try:
    import pytesseract
except ImportError:
    pytesseract = None

try:
    from PIL import Image, ImageOps, ImageTk
except ImportError:
    Image = ImageOps = ImageTk = None


APP_NAME = "OroCopy"
BG = "#10151b"
CARD = "#18212a"
TEXT = "#e8edf2"
MUTED = "#9aa6b2"
GREEN = "#57d99a"
RED = "#ff7777"


def app_data_dir() -> Path:
    path = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_file() -> Path:
    return app_data_dir() / "observer.json"


def load_region() -> dict | None:
    try:
        data = json.loads(config_file().read_text(encoding="utf-8"))
        region = data.get("region")
        if region and all(k in region for k in ("left", "top", "width", "height")):
            return {k: int(region[k]) for k in ("left", "top", "width", "height")}
    except (OSError, ValueError, TypeError):
        pass
    return None


def save_region(region: dict) -> None:
    config_file().write_text(json.dumps({"region": region}, indent=2), encoding="utf-8")


def load_copy_defaults() -> tuple[str, str]:
    symbol, volume = "XAUUSDm", "0.01"
    try:
        data = json.loads((app_data_dir() / "settings.json").read_text(encoding="utf-8"))
        symbol = str(data.get("symbol", symbol)).strip() or symbol
        volume = str(data.get("volume", volume)).strip() or volume
    except (OSError, ValueError, TypeError):
        pass
    if symbol.upper() == "XAUUSD":
        symbol = "XAUUSDm"
    return symbol, volume


def find_tesseract() -> str | None:
    found = shutil.which("tesseract")
    if found:
        return found
    candidates = (
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) /
        "Tesseract-OCR" / "tesseract.exe",
        Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) /
        "Programs" / "Tesseract-OCR" / "tesseract.exe",
    )
    return next((str(path) for path in candidates if path.is_file()), None)


def parse_counts(text: str, ocr_data: dict | None = None) -> tuple[int, int] | None:
    """Read counts under their side headings, using OCR coordinates when available."""
    if ocr_data and ocr_data.get("text"):
        tokens = []
        for i, raw in enumerate(ocr_data["text"]):
            word = str(raw).strip()
            if not word:
                continue
            tokens.append({
                "word": word,
                "norm": re.sub(r"[^A-Z]", "", word.upper()),
                "left": int(ocr_data["left"][i]),
                "top": int(ocr_data["top"][i]),
                "width": int(ocr_data["width"][i]),
                "height": int(ocr_data["height"][i]),
                "line": (ocr_data["block_num"][i], ocr_data["par_num"][i],
                         ocr_data["line_num"][i]),
            })
        side_specs = {
            "buy": {"COMPRAS", "COMPRA", "BUY", "BUYS", "LONG"},
            "sell": {"VENTAS", "VENTA", "SELL", "SELLS", "SHORT"},
        }
        anchors = {}
        for side, names in side_specs.items():
            matches = [t for t in tokens if t["norm"] in names]
            if matches:
                anchors[side] = min(matches, key=lambda t: t["top"])
        if len(anchors) == 2:
            boundary = ((anchors["buy"]["left"] + anchors["buy"]["width"] / 2) +
                        (anchors["sell"]["left"] + anchors["sell"]["width"] / 2)) / 2
        else:
            boundary = None

        counts = {}
        for side, anchor in anchors.items():
            center_x = anchor["left"] + anchor["width"] / 2
            center_y = anchor["top"] + anchor["height"] / 2

            def in_column(token) -> bool:
                token_x = token["left"] + token["width"] / 2
                return (boundary is None or
                        (token_x <= boundary if center_x <= boundary else token_x > boundary))

            positions = [t for t in tokens
                         if t["norm"].startswith("POSICION") and in_column(t) and
                         0 <= t["top"] - center_y <= max(240, anchor["height"] * 6)]
            value = None
            for pos in sorted(positions, key=lambda t: t["top"]):
                same_line = [t for t in tokens if t["line"] == pos["line"] and
                             re.fullmatch(r"\d{1,3}", t["word"].strip()) and
                             t["left"] >= pos["left"] + pos["width"] and in_column(t)]
                if same_line:
                    value = int(min(same_line, key=lambda t: t["left"])["word"])
                    break
                below = [t for t in tokens if re.fullmatch(r"\d{1,3}", t["word"].strip()) and
                         in_column(t) and 0 <= t["top"] - pos["top"] <= max(120, pos["height"] * 4)]
                if below:
                    value = int(min(below, key=lambda t: t["top"])["word"])
                    break
            if value is None:
                below = [t for t in tokens if re.fullmatch(r"\d{1,3}", t["word"].strip()) and
                         in_column(t) and 0 < t["top"] - center_y <= max(180, anchor["height"] * 5)]
                if below:
                    value = int(min(below, key=lambda t: t["top"])["word"])
            if value is not None and 0 <= value <= 200:
                counts[side] = value
        if "buy" in counts and "sell" in counts:
            return counts["buy"], counts["sell"]

    # Conservative fallback: each number must belong to text after its heading
    # and before the other heading, so it cannot borrow the other side's count.
    normalized = re.sub(r"\s+", " ", text.upper())
    heading = re.compile(r"COMPRAS|COMPRA|BUYS?|LONG|VENTAS|VENTA|SELLS?|SHORT")
    matches = list(heading.finditer(normalized))
    segments = {}
    for index, match in enumerate(matches):
        next_start = matches[index + 1].start() if index + 1 < len(matches) else len(normalized)
        label = match.group(0)
        side = "buy" if label in {"COMPRAS", "COMPRA", "BUY", "BUYS", "LONG"} else "sell"
        segment = normalized[match.end():next_start]
        pos = re.search(r"POSICION(?:ES)?\D{0,12}(\d{1,3})\b", segment)
        if pos:
            value = int(pos.group(1))
            if value <= 200:
                segments[side] = value
    if "buy" in segments and "sell" in segments:
        return segments["buy"], segments["sell"]
    return None


class RegionSelector:
    def __init__(self, owner: "ScreenObserver") -> None:
        self.owner = owner
        self.window = tk.Toplevel(owner.root)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.attributes("-alpha", 0.22)
        with mss.mss() as screen:
            virtual = screen.monitors[0]
        self.origin_x, self.origin_y = virtual["left"], virtual["top"]
        x_offset = str(self.origin_x) if self.origin_x < 0 else f"+{self.origin_x}"
        y_offset = str(self.origin_y) if self.origin_y < 0 else f"+{self.origin_y}"
        self.window.geometry(f"{virtual['width']}x{virtual['height']}{x_offset}{y_offset}")
        self.canvas = tk.Canvas(self.window, bg="#06111a", highlightthickness=0,
                                cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_text(24, 24, anchor="nw",
                                text="Arrastra un rectángulo que incluya COMPRAS y VENTAS · Esc cancela",
                                fill="white", font=("Segoe UI", 16, "bold"))
        self.start: tuple[int, int] | None = None
        self.rectangle = None
        self.canvas.bind("<ButtonPress-1>", self.begin)
        self.canvas.bind("<B1-Motion>", self.move)
        self.canvas.bind("<ButtonRelease-1>", self.finish)
        self.window.bind("<Escape>", lambda _event: self.cancel())
        self.window.protocol("WM_DELETE_WINDOW", self.cancel)
        self.window.focus_force()

    def cancel(self) -> None:
        self.window.destroy()
        self.owner.root.deiconify()
        self.owner.root.lift()

    def begin(self, event) -> None:
        self.start = (event.x_root, event.y_root)
        x = event.x_root - self.origin_x
        y = event.y_root - self.origin_y
        self.rectangle = self.canvas.create_rectangle(
            x, y, x, y, outline="#57d99a", width=3, fill="#57d99a", stipple="gray25")

    def move(self, event) -> None:
        if self.start is None or self.rectangle is None:
            return
        x1 = self.start[0] - self.origin_x
        y1 = self.start[1] - self.origin_y
        x2 = event.x_root - self.origin_x
        y2 = event.y_root - self.origin_y
        self.canvas.coords(self.rectangle, x1, y1, x2, y2)

    def finish(self, event) -> None:
        if self.start is None:
            return
        x1, y1 = self.start
        x2, y2 = event.x_root, event.y_root
        region = {
            "left": min(x1, x2), "top": min(y1, y2),
            "width": abs(x2 - x1), "height": abs(y2 - y1),
        }
        self.window.destroy()
        if region["width"] < 40 or region["height"] < 25:
            self.owner.status_var.set("Selección muy pequeña; vuelve a marcar la zona")
            self.owner.root.deiconify()
            self.owner.root.lift()
            return
        self.owner.set_region(region)


class ScreenObserver:
    def __init__(self) -> None:
        if os.name == "nt":
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
        self.root = tk.Tk()
        self.root.title("OroCopy · Lector de pantalla")
        self.root.geometry("470x500")
        self.root.minsize(400, 430)
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.region = load_region()
        self.running = False
        self.stop_event = threading.Event()
        self.results: queue.Queue = queue.Queue(maxsize=2)
        self.worker: threading.Thread | None = None
        self.last_candidate: tuple[int, int] | None = None
        self.candidate_repeats = 0
        self.stable_counts: tuple[int, int] | None = None
        self.last_photo = None
        self.copy_engine = CopyExecution() if CopyExecution is not None else None
        self.copy_active = False
        self.copy_busy = False
        self.copy_worker_target: tuple[int, int] | None = None
        self.copy_pending_target: tuple[int, int] | None = None
        self.copy_last_synced: tuple[int, int] | None = None
        self.copy_results: queue.Queue = queue.Queue()
        self.copy_cancel_event = threading.Event()
        self.copy_command_id = ""
        self.copy_status = "Desarmada · no envía órdenes"
        self.copy_closed_total = 0
        self.copied_counts = (0, 0)
        self.ocr_failure_since: float | None = None
        self.copy_symbol, _default_volume = load_copy_defaults()
        self.copy_volume = 0.01

        tk.Label(self.root, text="OROCOPY · LECTOR", bg=BG, fg=TEXT,
                 font=("Segoe UI", 15, "bold")).pack(anchor="w", padx=14, pady=(10, 1))
        tk.Label(self.root,
                 text="Conteo visual del directo · control de copia en la barra Ctrl+Alt+G",
                 bg=BG, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w", padx=14)

        controls = tk.Frame(self.root, bg=BG)
        controls.pack(fill="x", padx=14, pady=8)
        self.select_button = self._button(controls, "Seleccionar zona", self.select_region,
                                          "#275c4b")
        self.select_button.pack(side="left", padx=(0, 8))
        self.start_button = self._button(controls, "Iniciar lectura", self.start_reading,
                                         "#176d46")
        self.start_button.pack(side="left", padx=(0, 8))
        self.stop_button = self._button(controls, "Detener lectura", self.stop_reading, "#842e35")
        self.stop_button.pack(side="left")

        self.region_var = tk.StringVar(value=self._region_text())
        tk.Label(self.root, textvariable=self.region_var, bg=BG, fg=MUTED,
                 font=("Consolas", 8)).pack(anchor="w", padx=16, pady=(0, 3))
        self.preview = tk.Label(self.root, text="La vista previa aparecerá aquí",
                                bg="#080c10", fg=MUTED, font=("Segoe UI", 9),
                                width=55, height=10, relief="flat")
        self.preview.pack(fill="both", expand=True, padx=14, pady=3)

        counts = tk.Frame(self.root, bg=CARD)
        counts.pack(fill="x", padx=14, pady=(5, 3))
        self.buy_var = tk.StringVar(value="COMPRAS: —")
        self.sell_var = tk.StringVar(value="VENTAS: —")
        self.buy_label = tk.Label(counts, textvariable=self.buy_var, bg=CARD, fg=GREEN,
                                  font=("Segoe UI", 13, "bold"), padx=10, pady=6)
        self.buy_label.pack(side="left", expand=True)
        self.sell_label = tk.Label(counts, textvariable=self.sell_var, bg=CARD, fg=RED,
                                   font=("Segoe UI", 13, "bold"), padx=10, pady=6)
        self.sell_label.pack(side="left", expand=True)
        self.copy_summary_var = tk.StringVar(value="COPIA DEMO · DESARMADA")
        tk.Label(self.root, textvariable=self.copy_summary_var, bg=BG, fg=MUTED,
                 font=("Segoe UI", 8, "bold"), anchor="w").pack(fill="x", padx=16, pady=(0, 2))
        self.raw_text = tk.StringVar(value="—")
        tk.Label(self.root, textvariable=self.raw_text, bg=BG, fg=TEXT,
                 font=("Consolas", 8), anchor="w", justify="left",
                 wraplength=430).pack(fill="x", padx=16, pady=(2, 2))
        self.status_var = tk.StringVar(value=self._ocr_status())
        tk.Label(self.root, textvariable=self.status_var, bg=BG, fg="#e0a34a",
                 font=("Segoe UI", 8), anchor="w", justify="left",
                 wraplength=430).pack(fill="x", padx=16, pady=(0, 7))

        self.root.bind("<Escape>", lambda _event: self.root.iconify())
        self.root.after(100, self._poll_results)
        self.root.after(100, self._poll_copy_results)
        self.root.after(150, self._poll_dashboard_command)
        self.root.after(300, self._publish_state)

    def _button(self, parent, label, command, color):
        return tk.Button(parent, text=label, command=command, relief="flat",
                         bg=color, fg="white", activebackground=color,
                         activeforeground="white", font=("Segoe UI", 10, "bold"),
                         padx=14, pady=8, cursor="hand2")

    def _ocr_status(self) -> str:
        if mss is None:
            return "Falta la dependencia mss; ejecuta setup.ps1 para instalarla."
        if pytesseract is None or Image is None:
            return "Faltan dependencias de OCR; ejecuta setup.ps1 para instalarlas."
        exe = find_tesseract()
        if not exe:
            return "Instala Tesseract OCR para Windows; la vista previa seguirá disponible."
        pytesseract.pytesseract.tesseract_cmd = exe
        return "Listo para capturar. El reconocimiento puede requerir ajustar la zona."

    def _region_text(self) -> str:
        if not self.region:
            return "Zona: sin seleccionar"
        r = self.region
        return f"Zona: x={r['left']} y={r['top']} · {r['width']}×{r['height']} px"

    def select_region(self) -> None:
        if self.running:
            self.status_var.set("Detén la lectura antes de cambiar la zona")
            return
        self.root.withdraw()
        self.root.after(250, lambda: RegionSelector(self))

    def set_region(self, region: dict) -> None:
        self.region = region
        save_region(region)
        self.region_var.set(self._region_text())
        self.root.deiconify()
        self.root.lift()
        self.status_var.set("Zona guardada. Asegúrate de incluir los rótulos y sus cantidades.")

    def start_reading(self) -> None:
        if self.region is None:
            messagebox.showinfo("Selecciona una zona", "Marca primero el área que contiene COMPRAS y VENTAS.")
            return
        if mss is None or Image is None:
            self.status_var.set("No se puede capturar: faltan Pillow o mss. Ejecuta setup.ps1.")
            return
        if pytesseract is None or not find_tesseract():
            self.status_var.set("No se puede leer texto: instala Tesseract OCR y reinicia OroCopy.")
            return
        if self.running:
            return
        pytesseract.pytesseract.tesseract_cmd = find_tesseract()
        self.last_candidate = None
        self.candidate_repeats = 0
        self.stable_counts = None
        self.buy_var.set("COMPRAS: —")
        self.sell_var.set("VENTAS: —")
        self.stop_event.clear()
        self.running = True
        self.start_button.configure(state="disabled")
        self.status_var.set("Lectura activa · la copia se controla desde el dashboard")
        self.worker = threading.Thread(target=self._capture_loop, daemon=True,
                                       name="OroCopyScreenReader")
        self.worker.start()

    def stop_reading(self) -> None:
        self._disarm_copy("Lectura detenida; copia desarmada. Posiciones copiadas siguen abiertas.")
        if not self.running:
            return
        self.running = False
        self.stop_event.set()
        self.start_button.configure(state="normal")
        self.status_var.set("Lectura detenida")

    def _capture_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                with mss.mss() as screen:
                    shot = screen.grab(self.region)
                image = Image.frombytes("RGB", shot.size, shot.rgb)
                text = ""
                ocr_data = None
                if pytesseract is not None:
                    processed = ImageOps.grayscale(image)
                    processed = ImageOps.autocontrast(processed).resize(
                        (max(1, processed.width * 3), max(1, processed.height * 3)))
                    ocr_data = pytesseract.image_to_data(
                        processed, config="--oem 3 --psm 6", lang="eng",
                        output_type=pytesseract.Output.DICT)
                    text = " ".join(str(word) for word in ocr_data["text"] if str(word).strip())
                self._put_result((image, text, ocr_data, ""))
            except Exception as exc:
                self._put_result((None, "", None, f"Error de captura/OCR: {exc}"))
                self.stop_event.wait(1.0)
            self.stop_event.wait(0.8)

    def _put_result(self, item) -> None:
        try:
            self.results.put_nowait(item)
        except queue.Full:
            try:
                self.results.get_nowait()
            except queue.Empty:
                pass
            try:
                self.results.put_nowait(item)
            except queue.Full:
                pass

    def _poll_results(self) -> None:
        latest = None
        try:
            while True:
                latest = self.results.get_nowait()
        except queue.Empty:
            pass
        if latest is not None:
            image, text, ocr_data, error = latest
            if image is not None:
                image.thumbnail((430, 230))
                self.last_photo = ImageTk.PhotoImage(image)
                self.preview.configure(image=self.last_photo, text="")
            if error:
                self.status_var.set(error[:160])
                self._handle_ocr_failure("No se pudo capturar/leer la zona")
            else:
                cleaned = re.sub(r"\s+", " ", text).strip()
                self.raw_text.set(cleaned or "No se reconoció texto en la zona")
                self._update_counts(text, ocr_data)
        if self.root.winfo_exists():
            self.root.after(100, self._poll_results)

    def _update_counts(self, text: str, ocr_data: dict | None = None) -> None:
        counts = parse_counts(text, ocr_data)
        if counts is None:
            self._handle_ocr_failure("No se reconocen COMPRAS/VENTAS")
            self.buy_var.set("COMPRAS: ?")
            self.sell_var.set("VENTAS: ?")
            self.last_candidate = None
            self.candidate_repeats = 0
            return
        had_ocr_failure = self.ocr_failure_since is not None
        self.ocr_failure_since = None
        if had_ocr_failure and self.copy_active:
            self.copy_status = "Lectura recuperada · la copia continúa activa"
            self._publish_state()
        if counts == self.last_candidate:
            self.candidate_repeats += 1
        else:
            self.last_candidate = counts
            self.candidate_repeats = 1
        if self.candidate_repeats < 3:
            self.status_var.set("Confirmando lectura del contador…")
            return
        self.buy_var.set(f"COMPRAS: {counts[0]}")
        self.sell_var.set(f"VENTAS: {counts[1]}")
        if counts == self.stable_counts:
            return

        previous = self.stable_counts
        self.stable_counts = counts
        self._log_counts(previous, counts)
        if self.copy_active:
            if counts[0] and counts[1]:
                self._disarm_copy("Lectura ambigua: compras y ventas a la vez. Copia pausada; revisa MT5.")
            else:
                self._queue_copy_target(counts)
        if previous is None:
            self.status_var.set("Lectura estable · esperando cambios de posiciones")
        elif counts == (0, 0):
            self.status_var.set("Detectó cierre total · sincronizando copia" if self.copy_active
                                else "Detectó cierre total · solo registrado, sin operar")
        else:
            self.status_var.set("Cambio detectado · sincronizando copia" if self.copy_active
                                else "Cambio de posiciones detectado · solo registrado, sin operar")

    def _arm_copy(self, volume_value, symbol_value=None) -> None:
        if self.copy_engine is None:
            self.copy_status = "MT5 no disponible · copia no armada"
            self._publish_state()
            return
        if not self.running:
            self.copy_status = "Inicia lectura y espera el conteo estable"
            self._publish_state()
            return
        if self.stable_counts is None:
            self.copy_status = "Esperando conteo estable de COMPRAS/VENTAS"
            self._publish_state()
            return
        buys, sells = self.stable_counts
        if buys and sells:
            self.copy_status = "Conteo ambiguo · compras y ventas simultáneas"
            self._publish_state()
            return
        try:
            volume = float(str(volume_value).replace(",", "."))
            if volume <= 0:
                raise ValueError
            self.copy_volume = volume
            if symbol_value:
                self.copy_symbol = str(symbol_value).strip() or self.copy_symbol
            account = self.copy_engine.inspect_demo(self.copy_symbol)
        except Exception as exc:
            self.copy_status = f"Copia bloqueada · {exc}"
            self._publish_state()
            return
        self.copy_active = True
        self.copy_cancel_event.clear()
        self.ocr_failure_since = None
        self.copy_last_synced = None
        self.copy_status = f"Armada · {account} · {self.copy_symbol}"
        self._publish_state()
        self._queue_copy_target(self.stable_counts)

    def _disarm_copy(self, reason: str) -> None:
        was_active = self.copy_active
        self.copy_active = False
        self.copy_cancel_event.set()
        self.copy_pending_target = None
        self.copy_status = reason
        self._publish_state()
        if was_active:
            self._log_copy("COPY_DISARMED", None, reason)

    def _handle_ocr_failure(self, detail: str) -> None:
        if not self.copy_active:
            return
        now = time.monotonic()
        if self.ocr_failure_since is None:
            self.ocr_failure_since = now
        elapsed = now - self.ocr_failure_since
        remaining = max(0, int(30 - elapsed + 0.999))
        if elapsed >= 30:
            self._disarm_copy(
                "Sin lectura continua durante 30 s; copia pausada. Revisa las posiciones abiertas en MT5.")
        else:
            self.copy_status = (f"Lectura interrumpida ({detail}); esperando {remaining} s "
                                "antes de pausar. Posiciones abiertas siguen activas.")
            self._publish_state()

    def _queue_copy_target(self, target: tuple[int, int]) -> None:
        if not self.copy_active:
            return
        if self.copy_busy:
            self.copy_pending_target = target
            return
        if target == self.copy_last_synced:
            return
        self.copy_busy = True
        self.copy_worker_target = target
        threading.Thread(target=self._copy_worker, args=(target,), daemon=True,
                         name="OroCopyMT5Sync").start()

    def _copy_worker(self, target: tuple[int, int]) -> None:
        before = None
        try:
            before = self.copy_engine.positions_count(self.copy_symbol)
            summary = self.copy_engine.reconcile(
                self.copy_symbol, self.copy_volume, target, self.copy_cancel_event)
            after = self.copy_engine.positions_count(self.copy_symbol)
            self.copy_results.put((target, summary, None, before, after))
        except Exception as exc:
            try:
                after = self.copy_engine.positions_count(self.copy_symbol)
            except Exception:
                after = None
            self.copy_results.put((target, None, str(exc), before, after))

    def _poll_copy_results(self) -> None:
        try:
            while True:
                target, summary, error, before, after = self.copy_results.get_nowait()
                self.copy_busy = False
                self.copy_worker_target = None
                if error:
                    if before is not None and after is not None:
                        self.copy_closed_total += max(0, before[0] - after[0]) + max(0, before[1] - after[1])
                        self.copied_counts = after
                    self._log_copy("COPY_ERROR", target, error)
                    self._disarm_copy(f"Error de copia; pausada. Revisa MT5: {error[:120]}")
                else:
                    self.copy_last_synced = target
                    if before is not None and after is not None:
                        self.copy_closed_total += max(0, before[0] - after[0]) + max(0, before[1] - after[1])
                    self.copied_counts = after or target
                    self.copy_status = summary
                    self._publish_state()
                    self._log_copy("COPY_SYNC", target, summary)
                    if self.copy_active and self.copy_pending_target is not None:
                        pending = self.copy_pending_target
                        self.copy_pending_target = None
                        self._queue_copy_target(pending)
        except queue.Empty:
            pass
        if self.root.winfo_exists():
            self.root.after(100, self._poll_copy_results)

    def _log_copy(self, event: str, target: tuple[int, int] | None, detail: str) -> None:
        record = {
            "timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "event": event,
            "symbol": self.copy_symbol,
            "buys": None if target is None else target[0],
            "sells": None if target is None else target[1],
            "detail": detail,
            "magic": 71212611,
        }
        try:
            with (app_data_dir() / "copy_execution.jsonl").open("a", encoding="utf-8") as log:
                log.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            self.copy_status = f"No se pudo registrar la copia: {exc}"

    def _poll_dashboard_command(self) -> None:
        if read_observer_command is not None:
            command = read_observer_command()
            command_id = str(command.get("id", ""))
            if command_id and command_id != self.copy_command_id:
                self.copy_command_id = command_id
                try:
                    sent_at = datetime.fromisoformat(command.get("sent_at", ""))
                    age = (datetime.now().astimezone() - sent_at).total_seconds()
                except (TypeError, ValueError):
                    age = 999
                if -1 <= age <= 5:
                    action = command.get("action")
                    if action == "arm":
                        self._arm_copy(command.get("volume", "0.01"), command.get("symbol"))
                    elif action == "pause":
                        self._disarm_copy("Copia pausada desde el dashboard; posiciones abiertas siguen en MT5.")
        self._publish_state()
        if self.root.winfo_exists():
            self.root.after(250, self._poll_dashboard_command)

    def _publish_state(self) -> None:
        if write_observer_state is None:
            return
        try:
            write_observer_state({
                "reading": bool(self.running),
                "copy_active": bool(self.copy_active),
                "buys": None if self.stable_counts is None else self.stable_counts[0],
                "sells": None if self.stable_counts is None else self.stable_counts[1],
                "copied_buys": self.copied_counts[0],
                "copied_sells": self.copied_counts[1],
                "closed_total": self.copy_closed_total,
                "status": self.copy_status,
                "symbol": self.copy_symbol,
                "volume": self.copy_volume,
            })
            self.copy_summary_var.set(
                f"COPIA DEMO · {'ACTIVA' if self.copy_active else 'PAUSADA'} · "
                f"BUY {self.copied_counts[0]} / SELL {self.copied_counts[1]} · "
                f"cerradas {self.copy_closed_total}")
        except OSError:
            pass

    def _log_counts(self, previous: tuple[int, int] | None,
                    current: tuple[int, int]) -> None:
        if previous == current:
            return
        if previous is None:
            event = "BASE_INICIAL"
        elif current == (0, 0):
            event = "CIERRE_TOTAL"
        elif current[0] > previous[0] or current[1] > previous[1]:
            event = "POSICIONES_AUMENTARON"
        elif current[0] < previous[0] or current[1] < previous[1]:
            event = "POSICIONES_DISMINUYERON"
        else:
            event = "CAMBIO_DE_LADO"
        record = {
            "timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "event": event,
            "previous_buys": None if previous is None else previous[0],
            "previous_sells": None if previous is None else previous[1],
            "buys": current[0],
            "sells": current[1],
            "mode": "copy_armed" if self.copy_active else "observe_only",
        }
        try:
            with (app_data_dir() / "screen_observer.jsonl").open("a", encoding="utf-8") as log:
                log.write(json.dumps(record, ensure_ascii=False) + "\n")
        except OSError as exc:
            self.status_var.set(f"No se pudo guardar el registro: {exc}")

    def close(self) -> None:
        self._disarm_copy("Ventana cerrada; copia desarmada. Posiciones copiadas siguen abiertas.")
        self.stop_event.set()
        self.running = False
        self._publish_state()
        self.root.destroy()


def acquire_observer_mutex():
    """Prevent two OCR processes from sending duplicate copies."""
    if os.name != "nt":
        return True
    kernel = ctypes.windll.kernel32
    kernel.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p)
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.SetLastError.argtypes = (ctypes.c_ulong,)
    kernel.GetLastError.restype = ctypes.c_ulong
    kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
    kernel.CloseHandle.restype = ctypes.c_bool
    kernel.SetLastError(0)
    handle = kernel.CreateMutexW(None, False, "Local\\OroCopyObserverMutex")
    if kernel.GetLastError() == 183:
        kernel.CloseHandle(handle)
        return None
    return handle if handle else False


def ensure_dashboard_running() -> None:
    """Start the taskbar control if the observer was launched by itself."""
    if os.name != "nt":
        return
    handle = None
    try:
        kernel = ctypes.windll.kernel32
        kernel.OpenMutexW.argtypes = (ctypes.c_ulong, ctypes.c_bool, ctypes.c_wchar_p)
        kernel.OpenMutexW.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = (ctypes.c_void_p,)
        kernel.CloseHandle.restype = ctypes.c_bool
        handle = kernel.OpenMutexW(0x00100000, False, "Local\\OroCopyDashboardMutex")
        if handle:
            return
        app_file = Path(__file__).with_name("app.py")
        subprocess.Popen([sys.executable, str(app_file)], cwd=str(app_file.parent),
                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except OSError:
        pass
    finally:
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)


if __name__ == "__main__":
    observer_mutex = acquire_observer_mutex()
    if observer_mutex is None:
        raise SystemExit(0)
    ensure_dashboard_running()
    try:
        ScreenObserver().root.mainloop()
    finally:
        if observer_mutex and os.name == "nt":
            ctypes.windll.kernel32.CloseHandle(observer_mutex)
