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
    from chrome_capture import ChromeCaptureServer
except ImportError:
    ChromeCaptureServer = None

try:
    import mss
except ImportError:
    mss = None

try:
    import pytesseract
except ImportError:
    pytesseract = None

try:
    from PIL import Image, ImageFilter, ImageOps, ImageStat, ImageTk
except ImportError:
    Image = ImageFilter = ImageOps = ImageStat = ImageTk = None


APP_NAME = "OroCopy"
BG = "#10151b"
CARD = "#18212a"
TEXT = "#e8edf2"
MUTED = "#9aa6b2"
GREEN = "#57d99a"
RED = "#ff7777"
OCR_FRAME_INTERVAL = 0.45
OCR_STABLE_SAMPLES = 3
OCR_SINGLE_VARIANT_SAMPLES = 3
OCR_SINGLE_VARIANT_MIN_CONFIDENCE = 55


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
            loaded = {k: int(region[k]) for k in ("left", "top", "width", "height")}
            loaded["monitor"] = max(1, int(region.get("monitor", 1)))
            return loaded
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
    def __init__(self, owner: "ScreenObserver", monitor_number: int,
                 monitor: dict) -> None:
        self.owner = owner
        self.monitor_number = monitor_number
        self.window = tk.Toplevel(owner.root)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.attributes("-alpha", 0.22)
        self.origin_x, self.origin_y = monitor["left"], monitor["top"]
        x_offset = f"{self.origin_x:+d}"
        y_offset = f"{self.origin_y:+d}"
        self.window.geometry(f"{monitor['width']}x{monitor['height']}{x_offset}{y_offset}")
        self.canvas = tk.Canvas(self.window, bg="#06111a", highlightthickness=0,
                                cursor="crosshair")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_text(24, 24, anchor="nw",
                                text=(f"Pantalla {monitor_number}: arrastra el área con COMPRAS y VENTAS"
                                      " · Esc cancela"),
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
            "monitor": self.monitor_number,
        }
        self.window.destroy()
        if region["width"] < 40 or region["height"] < 25:
            self.owner.status_var.set("Selección muy pequeña; vuelve a marcar la zona")
            self.owner.root.deiconify()
            self.owner.root.lift()
            return
        self.owner.set_region(region)


class MonitorSelector:
    def __init__(self, owner: "ScreenObserver", monitors: list[dict]) -> None:
        self.owner = owner
        self.monitors = monitors
        self.window = tk.Toplevel(owner.root)
        self.window.title("OroCopy · elegir pantalla")
        self.window.configure(bg=BG)
        self.window.resizable(False, False)
        self.window.attributes("-topmost", True)
        self.window.protocol("WM_DELETE_WINDOW", self.cancel)
        tk.Label(self.window, text="ELIGE LA PANTALLA", bg=BG, fg=TEXT,
                 font=("Segoe UI", 12, "bold")).pack(anchor="w", padx=16, pady=(14, 4))
        tk.Label(self.window, text="Selecciona primero el monitor donde está el directo.",
                 bg=BG, fg=MUTED, font=("Segoe UI", 9)).pack(anchor="w", padx=16, pady=(0, 10))
        for index, monitor in enumerate(monitors, start=1):
            label = (f"Pantalla {index} · {monitor['width']}×{monitor['height']} px"
                     f" · posición {monitor['left']},{monitor['top']}")
            tk.Button(self.window, text=label, command=lambda i=index, m=monitor: self.choose(i, m),
                      relief="flat", bg="#176d46", fg="white", activebackground="#238858",
                      activeforeground="white", font=("Segoe UI", 9, "bold"),
                      padx=10, pady=8, cursor="hand2").pack(fill="x", padx=16, pady=4)
        tk.Button(self.window, text="Cancelar", command=self.cancel, relief="flat",
                  bg=CARD, fg=TEXT, font=("Segoe UI", 9), padx=10, pady=6).pack(
                      anchor="e", padx=16, pady=(6, 12))
        self.window.update_idletasks()
        width, height = self.window.winfo_width(), self.window.winfo_height()
        self.window.geometry(f"{width}x{height}+80+80")
        self.window.grab_set()
        self.window.focus_force()

    def choose(self, number: int, monitor: dict) -> None:
        self.window.grab_release()
        self.window.destroy()
        self.owner.root.after(120, lambda: RegionSelector(self.owner, number, monitor))

    def cancel(self) -> None:
        try:
            self.window.grab_release()
        except tk.TclError:
            pass
        self.window.destroy()
        self.owner.root.deiconify()
        self.owner.root.lift()


class ScreenObserver:
    def __init__(self) -> None:
        if os.name == "nt":
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
        self.root = tk.Tk()
        self.root.title("OroCopy · Lector de pantalla")
        self.root.geometry("430x465")
        self.root.minsize(370, 405)
        self.root.configure(bg=BG)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.region = load_region()
        self.running = False
        self.stop_event = threading.Event()
        self.results: queue.Queue = queue.Queue(maxsize=2)
        self.worker: threading.Thread | None = None
        self.capture_source = "monitor"
        self.chrome_frames: queue.Queue = queue.Queue(maxsize=2)
        self.chrome_last_frame_at = 0.0
        self.chrome_server = (ChromeCaptureServer(app_data_dir(), self._receive_chrome_frame,
                                                  lambda: self.running and
                                                  self.capture_source == "chrome")
                             if ChromeCaptureServer is not None else None)
        if self.chrome_server is not None:
            self.chrome_server.start()
        self.last_candidate: tuple[int, int] | None = None
        self.candidate_repeats = 0
        self.candidate_required = OCR_STABLE_SAMPLES
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
        self.ocr_failure_active = False
        self.copy_symbol, default_volume = load_copy_defaults()
        try:
            self.copy_volume = float(str(default_volume).replace(",", "."))
        except ValueError:
            self.copy_volume = 0.01

        tk.Label(self.root, text="OROCOPY · LECTOR", bg=BG, fg=TEXT,
                 font=("Segoe UI", 13, "bold")).pack(anchor="w", padx=12, pady=(7, 1))
        tk.Label(self.root,
                 text="Conteo visual del directo · OroCopy",
                 bg=BG, fg=MUTED, font=("Segoe UI", 8)).pack(anchor="w", padx=12)

        controls = tk.Frame(self.root, bg=BG)
        controls.pack(fill="x", padx=10, pady=5)
        for column in range(4):
            controls.grid_columnconfigure(column, weight=1, uniform="reader-controls")
        self.select_button = self._button(controls, "ZONA", self.select_region, "#275c4b")
        self.start_button = self._button(controls, "INICIAR", self.start_reading, "#176d46")
        self.stop_button = self._button(controls, "DETENER", self.stop_reading, "#842e35")
        self.copy_button = self._button(controls, "COPIAR", self.toggle_copy, "#176d46")
        for column, button in enumerate((self.select_button, self.start_button,
                                         self.stop_button, self.copy_button)):
            button.configure(font=("Segoe UI", 8, "bold"), padx=4, pady=3)
            button.grid(row=0, column=column, sticky="ew", padx=2)

        source_controls = tk.Frame(self.root, bg=BG)
        source_controls.pack(fill="x", padx=12, pady=(0, 3))
        self.source_var = tk.StringVar(value="monitor")
        for label, value in (("Monitor", "monitor"), ("Pestaña Chrome", "chrome")):
            tk.Radiobutton(source_controls, text=label, value=value, variable=self.source_var,
                           command=self._set_capture_source, bg=BG, fg=TEXT,
                           selectcolor=CARD, activebackground=BG, activeforeground=TEXT,
                           font=("Segoe UI", 8)).pack(side="left", padx=(0, 8))
        self.chrome_pair_button = self._button(source_controls, "Vincular Chrome",
                                               self.show_chrome_pairing, "#34424f")
        self.chrome_pair_button.configure(font=("Segoe UI", 8, "bold"), padx=6, pady=3)
        self.chrome_pair_button.pack(side="right")

        self.region_var = tk.StringVar(value=self._region_text())
        tk.Label(self.root, textvariable=self.region_var, bg=BG, fg=MUTED,
                 font=("Consolas", 7)).pack(anchor="w", padx=12, pady=(0, 2))
        self.preview = tk.Label(self.root, text="La vista previa aparecerá aquí",
                                bg="#080c10", fg=MUTED, font=("Segoe UI", 8),
                                width=45, height=7, relief="flat")
        self.preview.pack(fill="both", expand=True, padx=12, pady=2)

        counts = tk.Frame(self.root, bg=CARD)
        counts.pack(fill="x", padx=12, pady=(3, 2))
        self.buy_var = tk.StringVar(value="COMPRAS: —")
        self.sell_var = tk.StringVar(value="VENTAS: —")
        self.buy_label = tk.Label(counts, textvariable=self.buy_var, bg=CARD, fg=GREEN,
                                  font=("Segoe UI", 11, "bold"), padx=8, pady=3)
        self.buy_label.pack(side="left", expand=True)
        self.sell_label = tk.Label(counts, textvariable=self.sell_var, bg=CARD, fg=RED,
                                   font=("Segoe UI", 11, "bold"), padx=8, pady=3)
        self.sell_label.pack(side="left", expand=True)
        self.copy_summary_var = tk.StringVar(value="COPIA DEMO · DESARMADA")
        tk.Label(self.root, textvariable=self.copy_summary_var, bg=BG, fg=MUTED,
                 font=("Segoe UI", 7, "bold"), anchor="w",
                 wraplength=390).pack(fill="x", padx=12, pady=(0, 1))
        self.raw_text = tk.StringVar(value="—")
        tk.Label(self.root, textvariable=self.raw_text, bg=BG, fg=TEXT,
                 font=("Consolas", 7), anchor="w", justify="left",
                 wraplength=390).pack(fill="x", padx=12, pady=(1, 1))
        self.status_var = tk.StringVar(value=self._ocr_status())
        tk.Label(self.root, textvariable=self.status_var, bg=BG, fg="#e0a34a",
                 font=("Segoe UI", 7), anchor="w", justify="left",
                 wraplength=390).pack(fill="x", padx=12, pady=(0, 4))

        self.root.bind("<Escape>", lambda _event: self.root.iconify())
        self.root.after(100, self._poll_results)
        self.root.after(100, self._poll_copy_results)
        self.root.after(150, self._poll_dashboard_command)
        self.root.after(300, self._publish_state)

    def _button(self, parent, label, command, color):
        return tk.Button(parent, text=label, command=command, relief="flat",
                         bg=color, fg="white", activebackground=color,
                         activeforeground="white", font=("Segoe UI", 8, "bold"),
                         padx=6, pady=4, cursor="hand2")

    def _ocr_status(self) -> str:
        if pytesseract is None or Image is None:
            return "Faltan dependencias de OCR; ejecuta setup.ps1 para instalarlas."
        exe = find_tesseract()
        if not exe:
            return "Instala Tesseract OCR para Windows; la vista previa seguirá disponible."
        pytesseract.pytesseract.tesseract_cmd = exe
        if self.chrome_server is not None and self.chrome_server.error:
            return f"Puente Chrome no disponible: {self.chrome_server.error[:100]}"
        return "Listo. Elige Monitor o Pestaña Chrome antes de iniciar lectura."

    def _set_capture_source(self) -> None:
        if self.running:
            self.source_var.set(self.capture_source)
            self.status_var.set("Detén la lectura antes de cambiar la fuente")
            return
        self.capture_source = self.source_var.get()
        if self.capture_source == "chrome":
            if self.chrome_server is None or not self.chrome_server.available:
                self.status_var.set("Puente Chrome no disponible; revisa el puerto local 39872")
            else:
                self.status_var.set("Chrome: inicia lectura y captura la pestaña desde la extensión")
        else:
            self.status_var.set("Monitor: elige pantalla y zona antes de iniciar lectura")

    def show_chrome_pairing(self) -> None:
        if self.chrome_server is None or not self.chrome_server.available:
            messagebox.showerror("OroCopy · Chrome", "No está disponible el puente local de Chrome.")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(self.chrome_server.token)
        self.root.update_idletasks()
        messagebox.showinfo(
            "Vincular extensión OroCopy",
            "Token copiado al portapapeles. Pégalo en la extensión de Chrome.\n\n"
            f"Dirección local: {self.chrome_server.url}\n\n"
            "El token solo autoriza el envío de imágenes a este lector local.")

    def _receive_chrome_frame(self, image) -> bool:
        if not self.running or self.capture_source != "chrome":
            return False
        try:
            self.chrome_frames.put_nowait(image)
        except queue.Full:
            try:
                self.chrome_frames.get_nowait()
            except queue.Empty:
                pass
            try:
                self.chrome_frames.put_nowait(image)
            except queue.Full:
                return False
        self.chrome_last_frame_at = time.monotonic()
        return True

    def _region_text(self) -> str:
        if not self.region:
            return "Pantalla y zona: sin seleccionar"
        r = self.region
        return (f"Pantalla {r.get('monitor', 1)} · zona x={r['left']} y={r['top']} · "
                f"{r['width']}×{r['height']} px")

    def select_region(self) -> None:
        if self.running:
            self.status_var.set("Detén la lectura antes de cambiar la zona")
            return
        if mss is None:
            self.status_var.set("No se pueden listar pantallas: falta mss. Ejecuta setup.ps1.")
            return
        try:
            with mss.mss() as screen:
                monitors = [dict(monitor) for monitor in screen.monitors[1:]]
        except Exception as exc:
            self.status_var.set(f"No se pudieron listar las pantallas: {exc}")
            return
        if not monitors:
            self.status_var.set("No se detectaron monitores disponibles")
            return
        self.root.withdraw()
        self.root.after(150, lambda: MonitorSelector(self, monitors))

    def set_region(self, region: dict) -> None:
        self.region = region
        save_region(region)
        self.region_var.set(self._region_text())
        self.root.deiconify()
        self.root.lift()
        self.status_var.set("Zona guardada. Asegúrate de incluir los rótulos y sus cantidades.")

    def start_reading(self) -> None:
        self.capture_source = self.source_var.get()
        if self.capture_source == "monitor":
            if self.region is None:
                messagebox.showinfo("Selecciona una zona", "Marca primero el área que contiene COMPRAS y VENTAS.")
                return
            if mss is None:
                self.status_var.set("No se puede capturar: falta mss. Ejecuta setup.ps1.")
                return
            try:
                with mss.mss() as screen:
                    monitor_number = int(self.region.get("monitor", 1))
                    monitors = screen.monitors[1:]
                    if monitor_number < 1 or monitor_number > len(monitors):
                        raise ValueError("La pantalla guardada ya no está disponible")
                    monitor = monitors[monitor_number - 1]
                    left, top = int(self.region["left"]), int(self.region["top"])
                    right, bottom = left + int(self.region["width"]), top + int(self.region["height"])
                    if (left < monitor["left"] or top < monitor["top"] or
                            right > monitor["left"] + monitor["width"] or
                            bottom > monitor["top"] + monitor["height"]):
                        raise ValueError("La zona quedó fuera de la pantalla guardada")
            except (ValueError, OSError) as exc:
                self.status_var.set(f"Vuelve a elegir pantalla y zona: {exc}")
                return
        elif self.chrome_server is None or not self.chrome_server.available:
            self.status_var.set("Puente Chrome no disponible; cierra otra app que use el puerto 39872")
            return
        if Image is None:
            self.status_var.set("No se puede procesar la imagen: instala Pillow desde setup.ps1.")
            return
        if pytesseract is None or not find_tesseract():
            self.status_var.set("No se puede leer texto: instala Tesseract OCR y reinicia OroCopy.")
            return
        if self.running:
            return
        pytesseract.pytesseract.tesseract_cmd = find_tesseract()
        self.last_candidate = None
        self.candidate_repeats = 0
        self.candidate_required = OCR_STABLE_SAMPLES
        self.stable_counts = None
        self.buy_var.set("COMPRAS: —")
        self.sell_var.set("VENTAS: —")
        self.stop_event.clear()
        self.running = True
        self.start_button.configure(state="disabled")
        self.chrome_last_frame_at = 0.0
        self._publish_state()
        if self.capture_source == "chrome":
            self.status_var.set("Esperando fotogramas de la extensión Chrome…")
            target, worker_name = self._chrome_capture_loop, "OroCopyChromeReader"
        else:
            self.status_var.set("Lectura activa desde monitor · copia desde el dashboard")
            target, worker_name = self._capture_loop, "OroCopyScreenReader"
        self.worker = threading.Thread(target=target, daemon=True, name=worker_name)
        self.worker.start()

    def stop_reading(self) -> None:
        self._disarm_copy("Lectura detenida; copia desarmada. Posiciones copiadas siguen abiertas.")
        if not self.running:
            return
        self.running = False
        self.stop_event.set()
        self.start_button.configure(state="normal")
        self.status_var.set("Lectura detenida")
        self._publish_state()

    def _chrome_capture_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                image = self.chrome_frames.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                text, counts, agreement = self._recognize_counts(image)
                self._put_result((image, text, counts, agreement, ""))
            except Exception as exc:
                self._put_result((None, "", None, 0, f"Error de OCR Chrome: {exc}"))

    def _capture_loop(self) -> None:
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                with mss.mss() as screen:
                    capture_rect = {key: self.region[key]
                                    for key in ("left", "top", "width", "height")}
                    shot = screen.grab(capture_rect)
                image = Image.frombytes("RGB", shot.size, shot.rgb)
                text, counts, agreement = self._recognize_counts(image)
                self._put_result((image, text, counts, agreement, ""))
            except Exception as exc:
                self._put_result((None, "", None, 0, f"Error de captura/OCR: {exc}"))
            elapsed = time.monotonic() - started
            self.stop_event.wait(max(0.05, OCR_FRAME_INTERVAL - elapsed))

    def _recognize_counts(self, image):
        """Vote across different image/OCR settings for the fixed count panel."""
        gray = ImageOps.autocontrast(ImageOps.grayscale(image))
        mean = ImageStat.Stat(gray).mean[0]
        threshold = int(max(105, min(205, mean)))
        binary = gray.point(lambda value: 255 if value >= threshold else 0)
        sharpened = gray.filter(ImageFilter.UnsharpMask(radius=1.2, percent=140, threshold=2))
        variants = (
            ("gris", gray, 6),
            ("contraste", binary, 6),
            ("detalle", sharpened, 11),
        )
        votes: dict[tuple[int, int], int] = {}
        confidences: dict[tuple[int, int], list[float]] = {}
        diagnostics = []
        resampling = getattr(Image, "Resampling", Image).LANCZOS
        for name, variant, psm in variants:
            enlarged = variant.resize(
                (max(1, variant.width * 3), max(1, variant.height * 3)),
                resampling)
            data = pytesseract.image_to_data(
                enlarged, config=f"--oem 3 --psm {psm}", lang="eng",
                output_type=pytesseract.Output.DICT)
            words = [str(word).strip() for word in data["text"] if str(word).strip()]
            recognized = parse_counts(" ".join(words), data)
            digit_confidences = []
            for word, raw_confidence in zip(data["text"], data.get("conf", [])):
                word = str(word).strip()
                if not re.fullmatch(r"\d{1,3}", word) or int(word) > 200:
                    continue
                try:
                    confidence = float(raw_confidence)
                except (TypeError, ValueError):
                    continue
                if confidence >= 0:
                    digit_confidences.append(confidence)
            digit_confidence = min(digit_confidences) if digit_confidences else 0.0
            shown = recognized if recognized is not None else "?"
            diagnostics.append(f"{name}={shown} ({digit_confidence:.0f}%)")
            if recognized is not None:
                votes[recognized] = votes.get(recognized, 0) + 1
                confidences.setdefault(recognized, []).append(digit_confidence)

        consensus = max(votes, key=votes.get) if votes else None
        agreement = votes.get(consensus, 0) if consensus is not None else 0
        if agreement >= 2:
            return " · ".join(diagnostics), consensus, agreement

        # If only one image pipeline can read a small/pixelated counter, allow it
        # only with a credible digit score and a longer temporal confirmation.
        if len(votes) == 1 and agreement == 1:
            score = confidences[consensus][0]
            if score >= OCR_SINGLE_VARIANT_MIN_CONFIDENCE:
                return " · ".join(diagnostics), consensus, 1
        if agreement < 2:
            consensus = None
        return " · ".join(diagnostics), consensus, agreement

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
            image, text, counts, agreement, error = latest
            if image is not None:
                image.thumbnail((430, 230))
                self.last_photo = ImageTk.PhotoImage(image)
                self.preview.configure(image=self.last_photo, text="")
            if error:
                self.status_var.set(error[:160])
                self._handle_ocr_failure("No se pudo capturar/leer la zona")
            else:
                self.raw_text.set(text or "No se reconocieron los contadores")
                self._update_counts(counts, agreement)
        if self.running and self.capture_source == "chrome":
            if self.chrome_last_frame_at == 0:
                self.status_var.set("Esperando fotogramas · inicia captura desde la extensión Chrome")
            elif time.monotonic() - self.chrome_last_frame_at > 3:
                self.status_var.set("Sin fotogramas recientes · revisa la pestaña Chrome")
        if self.root.winfo_exists():
            self.root.after(100, self._poll_results)

    def _update_counts(self, counts: tuple[int, int] | None, agreement: int = 0) -> None:
        if counts is None:
            self._handle_ocr_failure("No se reconocen COMPRAS/VENTAS")
            if self.stable_counts is None:
                self.buy_var.set("COMPRAS: ?")
                self.sell_var.set("VENTAS: ?")
            self.status_var.set("Lectura dudosa · las variantes no coinciden; conservo el último conteo")
            self.last_candidate = None
            self.candidate_repeats = 0
            self.candidate_required = OCR_STABLE_SAMPLES
            return
        had_ocr_failure = self.ocr_failure_active
        self.ocr_failure_active = False
        if had_ocr_failure and self.copy_active:
            self.copy_status = "Lectura recuperada · la copia continúa activa"
            self._publish_state()
        if counts == self.last_candidate:
            self.candidate_repeats += 1
            required = (OCR_SINGLE_VARIANT_SAMPLES if agreement == 1
                        else OCR_STABLE_SAMPLES)
            self.candidate_required = min(self.candidate_required, required)
        else:
            self.last_candidate = counts
            self.candidate_repeats = 1
            self.candidate_required = (OCR_SINGLE_VARIANT_SAMPLES if agreement == 1
                                       else OCR_STABLE_SAMPLES)
        if self.candidate_repeats < self.candidate_required:
            self.status_var.set(
                f"Confirmando {self.candidate_repeats}/{self.candidate_required} · acuerdo OCR {agreement}/3…")
            return
        self.buy_var.set(f"COMPRAS: {counts[0]}")
        self.sell_var.set(f"VENTAS: {counts[1]}")
        if counts == self.stable_counts:
            self.candidate_required = OCR_STABLE_SAMPLES
            self.status_var.set(
                f"Lectura confirmada · sin cambios · acuerdo OCR {agreement}/3")
            self._publish_state()
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
            self.status_var.set(f"Lectura confirmada · esperando cambios · acuerdo OCR {agreement}/3")
        elif counts == (0, 0):
            self.status_var.set("Detectó cierre total · sincronizando copia" if self.copy_active
                                else "Detectó cierre total · solo registrado, sin operar")
        else:
            self.status_var.set("Cambio detectado · sincronizando copia" if self.copy_active
                                else "Cambio de posiciones detectado · solo registrado, sin operar")
        self._publish_state()

    def _sync_copy_button(self) -> None:
        if not hasattr(self, "copy_button"):
            return
        active = self.copy_active
        can_arm = (self.running and self.copy_engine is not None and
                   self.stable_counts is not None and
                   not (self.stable_counts[0] and self.stable_counts[1]))
        self.copy_button.configure(
            text="PAUSAR" if active else "COPIAR",
            bg="#842e35" if active else "#176d46",
            activebackground="#842e35" if active else "#176d46",
            state="normal" if active or can_arm else "disabled")

    def toggle_copy(self) -> None:
        if self.copy_active:
            self._disarm_copy("Copia pausada manualmente; posiciones abiertas siguen en MT5.")
            return
        if not self.running or self.stable_counts is None:
            self.copy_status = "Inicia lectura y espera el conteo estable"
            self._publish_state()
            return
        buys, sells = self.stable_counts
        if buys and sells:
            self.copy_status = "Conteo ambiguo · compras y ventas simultáneas"
            self._publish_state()
            return
        if self.copy_engine is None:
            self.copy_status = "MT5 no disponible · copia no armada"
            self._publish_state()
            return
        try:
            symbol, volume_text = load_copy_defaults()
            volume = float(str(volume_text).replace(",", "."))
            if volume <= 0:
                raise ValueError("El lote debe ser mayor que cero")
            account = self.copy_engine.inspect_demo(symbol)
            side_count = f"{buys} BUY" if buys else f"{sells} SELL" if sells else "0 posiciones"
            warning = (
                f"{account}\n{symbol} · lote {volume:g} por orden\n"
                f"Conteo estable: {side_count}.\n\n"
                "Las entradas se copiarán según el conteo visible. No llevan SL/TP inicial. "
                "¿Armar la copia DEMO?")
            if messagebox.askyesno("Confirmar copia DEMO", warning, parent=self.root,
                                   icon="warning"):
                self._arm_copy(volume, symbol)
        except Exception as exc:
            self.copy_status = f"Copia bloqueada · {exc}"
            self._publish_state()

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
        self.ocr_failure_active = False
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
        self.ocr_failure_active = True
        self.copy_status = (f"Lectura interrumpida ({detail}); copia sigue armada. "
                            "Revisa las posiciones abiertas en MT5.")
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
            self._sync_copy_button()
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
        if self.chrome_server is not None:
            self.chrome_server.close()
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
