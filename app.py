"""Native Windows XAUUSD control strip for MetaTrader 5.

The strip is shown/hidden with Ctrl+Alt+G and temporarily occupies the Windows
taskbar rectangle. It is deliberately not a browser application. Trading actions
are enabled only when the MetaTrader5 Python package is installed and a terminal
session is connected.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from datetime import datetime, timezone
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import messagebox
from pathlib import Path

try:
    import MetaTrader5 as mt5
except ImportError:
    mt5 = None

try:
    import pystray
    from PIL import Image, ImageDraw
except ImportError:
    pystray = None
    Image = ImageDraw = None

try:
    from copy_ipc import read_observer_state, send_observer_command
except ImportError:
    read_observer_state = send_observer_command = None

try:
    from copy_execution import CopyExecution
except ImportError:
    CopyExecution = None


APP_NAME = "OroCopy"
HOTKEY_TOGGLE = 1
HOTKEY_QUIT = 2
HOTKEY_OBSERVER = 3
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
VK_G = ord("G")
VK_Q = ord("Q")
VK_O = ord("O")
SW_HIDE = 0
SW_SHOW = 5

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", wintypes.LONG),
        ("top", wintypes.LONG),
        ("right", wintypes.LONG),
        ("bottom", wintypes.LONG),
    ]


user32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
user32.FindWindowW.restype = wintypes.HWND
user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(RECT))
user32.GetWindowRect.restype = wintypes.BOOL
user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
user32.ShowWindow.restype = wintypes.BOOL
user32.IsWindow.argtypes = (wintypes.HWND,)
user32.IsWindow.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = (wintypes.HWND,)
user32.IsWindowVisible.restype = wintypes.BOOL
user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
user32.GetWindowTextLengthW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
user32.GetWindowTextW.restype = ctypes.c_int
user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.RegisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int,
                                  wintypes.UINT, wintypes.UINT)
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = (wintypes.HWND, ctypes.c_int)
user32.UnregisterHotKey.restype = wintypes.BOOL
user32.GetMessageW.argtypes = (ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                               wintypes.UINT, wintypes.UINT)
user32.GetMessageW.restype = ctypes.c_int
user32.PostThreadMessageW.argtypes = (wintypes.DWORD, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM)
user32.PostThreadMessageW.restype = wintypes.BOOL
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR)
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.SetLastError.argtypes = (wintypes.DWORD,)
kernel32.SetLastError.restype = None
kernel32.GetLastError.restype = wintypes.DWORD
kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
kernel32.CloseHandle.restype = wintypes.BOOL


def config_path() -> Path:
    base = Path(os.environ.get("APPDATA", str(Path.home()))) / APP_NAME
    base.mkdir(parents=True, exist_ok=True)
    return base / "settings.json"


def load_settings() -> dict:
    defaults = {"symbol": "XAUUSDm", "volume": "0.01", "sl": "", "tp": "",
                "cruise_trigger": "0", "cruise_sl_distance": "",
                "cruise_tp_distance": ""}
    try:
        stored = json.loads(config_path().read_text(encoding="utf-8"))
        defaults.update({k: str(v) for k, v in stored.items() if k in defaults})
    except (OSError, ValueError, TypeError):
        pass
    if defaults["symbol"].strip().upper() == "XAUUSD":
        defaults["symbol"] = "XAUUSDm"
    return defaults


def save_settings(settings: dict) -> None:
    config_path().write_text(json.dumps(settings, indent=2), encoding="utf-8")


class GoldControl:
    def __init__(self) -> None:
        kernel32.SetLastError(0)
        self.instance_mutex = kernel32.CreateMutexW(None, False, "Local\\OroCopyDashboardMutex")
        if kernel32.GetLastError() == 183:
            kernel32.CloseHandle(self.instance_mutex)
            self.instance_mutex = None
            return
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.configure(bg="#10151b")
        self.root.bind("<Escape>", lambda _event: self.hide_panel())
        # OroCopy lives in the tray: closing its window must not terminate it.
        self.root.protocol("WM_DELETE_WINDOW", self.hide_panel)

        self.settings = load_settings()
        self.vars: dict[str, tk.StringVar] = {}
        self.action_controls: list[tk.Widget] = []
        self.hotkey_events: queue.Queue[str] = queue.Queue()
        self.hotkey_thread_id = 0
        self.hotkeys_registered = False
        self.taskbar_hwnd = None
        self.taskbar_rect: tuple[int, int, int, int] | None = None
        self.visible = False
        self.mt5_ready = False
        self.mt5_error = "MT5 desconectado"
        self.symbol = self.settings["symbol"].strip() or "XAUUSDm"
        self.bid = None
        self.ask = None
        self.position = None
        self.positions: tuple = ()
        self.cruise_active = False
        self.narrow_layout = True
        self.field_labels: dict[str, tk.Label] = {}
        self.field_entries: dict[str, tk.Entry] = {}
        self.last_cruise_status = ""
        self.last_cruise_status_at = 0.0
        self.daily_realized_cache = 0.0
        self.daily_realized_cache_at = 0.0
        self.daily_realized_cache_date = ""
        self.feedback_generation = 0
        self.tray_icon = None
        self.mt5_window_hwnd = None
        self.mt5_window_hidden = False
        self.observer_process = None
        self.copy_state: dict = {}
        self.copy_state_fresh = False
        self.copy_last_status = ""
        self.copy_engine = CopyExecution() if CopyExecution is not None else None
        self.closing = False

        self._build_ui()
        self._start_tray_icon()
        self._start_hotkeys()
        self._after(100, self._drain_hotkeys)
        self._after(1000, self._refresh)
        self._after(250, self._refresh_copy_state)
        self.root.mainloop()

    def _build_ui(self) -> None:
        self.shell = tk.Frame(self.root, bg="#10151b", highlightthickness=1,
                              highlightbackground="#303943")
        self.shell.pack(fill="both", expand=True)

        self.status_var = tk.StringVar(value="MT5 OFF")
        self.status_label = tk.Label(self.shell, textvariable=self.status_var,
                                     bg="#10151b", fg="#9aa6b2",
                                     font=("Segoe UI", 6, "bold"), wraplength=54)
        self.status_label.pack(fill="x", padx=2, pady=(0, 1))

        self.price_var = tk.StringVar(value="—")
        self.price_label = tk.Label(self.shell, textvariable=self.price_var, bg="#10151b",
                                    fg="#e8edf2", font=("Segoe UI", 11, "bold"),
                                    wraplength=54)
        self.price_label.pack(fill="x", padx=1, pady=(0, 0))
        self.entry_price_var = tk.StringVar(value="PRECIO BUY / SELL\n—")
        self.entry_price_label = tk.Label(
            self.shell, textvariable=self.entry_price_var, bg="#10151b",
            fg="#9aa6b2", font=("Segoe UI", 8, "bold"), justify="center", wraplength=54)
        self.entry_price_label.pack(fill="x", padx=1, pady=(1, 4))

        self._field("PAR", "symbol", self.symbol)
        self._field("LOTE", "volume", self.settings["volume"])
        # Keep empty internal values for legacy order helpers; no SL/TP controls are shown.
        for key in ("sl", "tp", "cruise_trigger", "cruise_sl_distance", "cruise_tp_distance"):
            self.vars[key] = tk.StringVar(value="")

        self.buy_button = self._button("BUY", "#148a51", self.open_buy, pady=(3, 1))
        self.sell_button = self._button("SELL", "#c64545", self.open_sell, pady=2)
        self.save_button = self._button("GUARDAR", "#53616d", self.save_position_levels,
                                        pady=(3, 1))
        self.save_button.pack_forget()
        self.be_button = self._button("BE", "#29333d", self.move_to_breakeven, pady=(3, 1))
        self.cruise_button = self._button("CRUZ", "#34424f",
                                         self.toggle_cruise, pady=(1, 1))
        self.cruise_button.pack_forget()
        self.copy_counts_var = tk.StringVar(value="COPY B0 S0 C0")
        self.copy_counts_label = self._label("COPY B0 S0 C0", 7, "#9aa6b2", bold=True,
                                             pady=(1, 1))
        self.copy_counts_label.configure(textvariable=self.copy_counts_var)
        self.copy_counts_label.pack_configure(before=self.be_button)
        self.close_half_button = self._button("50% + BE", "#8b6530",
                                              self.close_half_protected, pady=(1, 2))
        self.close_full_button = self._button("CERRAR 100%", "#842e35",
                                              self.close_position, pady=(1, 2))

        self.position_heading = self._label("OPERACIÓN", 9, "#d5dde5", bold=True,
                                            pady=(8, 0))
        self.position_var = tk.StringVar(value="SIN POSICIÓN")
        self.position_label = tk.Label(self.shell, textvariable=self.position_var,
                                       bg="#10151b", fg="#bec7d0",
                                       font=("Segoe UI", 8), wraplength=54,
                                       justify="center")
        self.position_label.pack(fill="x", padx=2, pady=(1, 0))
        self.balance_var = tk.StringVar(value="SALDO\n—")
        self.balance_label = tk.Label(
            self.shell, textvariable=self.balance_var, bg="#10151b", fg="#e8edf2",
            font=("Segoe UI", 8, "bold"), justify="center", wraplength=54)
        self.balance_label.pack(fill="x", padx=1, pady=(3, 0))
        self.daily_var = tk.StringVar(value="HOY\n—")
        self.daily_label = tk.Label(
            self.shell, textvariable=self.daily_var, bg="#10151b", fg="#8995a1",
            font=("Segoe UI", 8, "bold"), justify="center", wraplength=54)
        self.daily_label.pack(fill="x", padx=1, pady=(0, 2))
        self.protection_var = tk.StringVar(value="SIN POSICIÓN")
        self.protection_label = tk.Label(self.shell, textvariable=self.protection_var,
                                         bg="#10151b", fg="#8995a1",
                                         font=("Segoe UI", 7, "bold"), justify="center")
        self.protection_label.pack(fill="x", padx=1, pady=(1, 2))
        self.risk_var = tk.StringVar(value="LECTOR\nSIN CONEXIÓN")
        self.risk_label = tk.Label(self.shell, textvariable=self.risk_var,
                                   bg="#10151b", fg="#d0a95c",
                                   font=("Segoe UI", 7), justify="center")
        self.risk_label.pack_forget()
        self.feedback_var = tk.StringVar(value="LISTO")
        self.feedback_label = tk.Label(self.shell, textvariable=self.feedback_var,
                                       bg="#10151b", fg="#9aa6b2",
                                       font=("Segoe UI", 7, "bold"), justify="center")
        self.feedback_label.pack(fill="x", padx=1, pady=(1, 2))
        self.datetime_var = tk.StringVar(value="--/--/----\n--:--:--")
        self.datetime_label = tk.Label(self.shell, textvariable=self.datetime_var,
                                       bg="#10151b", fg="#9aa6b2",
                                     font=("Segoe UI", 6), justify="center")
        self.datetime_label.pack(fill="x", padx=1, pady=(2, 1))
        self.hide_button = tk.Button(self.shell, text="VOLVER",
                                     command=self.hide_panel, relief="flat",
                                     bg="#202a33", fg="#cdd5dc",
                                     activebackground="#33404b",
                                     activeforeground="white",
                                     font=("Segoe UI", 6), cursor="hand2")
        self.hide_button.pack(fill="x", padx=2, pady=(1, 3))

        self._set_trading_enabled(False)

    def _label(self, text: str, size: int, color: str, bold: bool = False,
               pady=(0, 0)) -> tk.Label:
        label = tk.Label(self.shell, text=text, bg="#10151b", fg=color,
                         font=("Segoe UI", size, "bold" if bold else "normal"),
                         wraplength=54)
        label.pack(fill="x", padx=2, pady=pady)
        return label

    def _field(self, title: str, key: str, value: str) -> None:
        label = tk.Label(self.shell, text=title, bg="#10151b", fg="#9aa6b2",
                         font=("Segoe UI", 6), anchor="center", wraplength=54)
        label.pack(fill="x", padx=2, pady=(2, 0))
        self.field_labels[key] = label
        var = tk.StringVar(value=value)
        self.vars[key] = var
        entry = tk.Entry(self.shell, textvariable=var, relief="flat",
                         bg="#202a33", fg="#edf2f6", insertbackground="white",
                         font=("Segoe UI", 8), justify="center")
        entry.pack(fill="x", padx=2, pady=(0, 1), ipady=1)
        self.field_entries[key] = entry

    def _button(self, label: str, color: str, command, pady=(2, 2)) -> tk.Button:
        button = tk.Button(self.shell, text=label, command=command,
                           relief="flat", bg=color, fg="white",
                           activebackground=color, activeforeground="white",
                           disabledforeground="#66717b",
                           font=("Segoe UI", 7, "bold"), cursor="hand2")
        button.pack(fill="x", padx=2, pady=pady, ipady=2)
        self.action_controls.append(button)
        return button

    def _set_trading_enabled(self, enabled: bool) -> None:
        for control in self.action_controls:
            control.configure(state="normal" if enabled else "disabled")
        self.hide_button.configure(state="normal")
        has_position = bool(enabled and self.positions)
        entry_state = "disabled" if has_position else ("normal" if enabled else "disabled")
        self.buy_button.configure(state=entry_state)
        self.sell_button.configure(state=entry_state)
        self.save_button.configure(
            state="normal" if has_position else "disabled")
        for control in (self.be_button, self.cruise_button,
                        self.close_half_button, self.close_full_button):
            control.configure(state="normal" if has_position else "disabled")

    def _start_hotkeys(self) -> None:
        thread = threading.Thread(target=self._hotkey_loop, daemon=True,
                                  name="OroCopyHotkeys")
        thread.start()

    def _start_tray_icon(self) -> None:
        if pystray is None or Image is None or ImageDraw is None:
            return
        icon_image = Image.new("RGBA", (64, 64), (16, 21, 27, 255))
        draw = ImageDraw.Draw(icon_image)
        draw.ellipse((5, 5, 59, 59), fill=(193, 148, 63, 255))
        draw.text((13, 21), "XAU", fill=(16, 21, 27, 255))
        menu = pystray.Menu(
            pystray.MenuItem("Mostrar / ocultar · Ctrl+Alt+G",
                             lambda _icon, _item: self.hotkey_events.put("show_panel")),
            pystray.MenuItem("Lector de TikTok · Ctrl+Alt+O",
                             lambda _icon, _item: self.hotkey_events.put("observer")),
            pystray.MenuItem("Ocultar ventana de MT5",
                             lambda _icon, _item: self.hotkey_events.put("hide_mt5")),
            pystray.MenuItem("Mostrar ventana de MT5",
                             lambda _icon, _item: self.hotkey_events.put("show_mt5")),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Salir de OroCopy",
                             lambda _icon, _item: self.hotkey_events.put("quit")),
        )
        self.tray_icon = pystray.Icon("OroCopy", icon_image,
                                     "OroCopy · MT5 sigue conectado", menu)
        self.tray_icon.run_detached()

    def _find_mt5_window(self):
        matches = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND,
                                           wintypes.LPARAM)

        def collect(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length <= 0:
                return True
            title_buffer = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, title_buffer, length + 1)
            title = title_buffer.value.casefold()
            if "metatrader 5" in title:
                matches.append(hwnd)
            return True

        callback = callback_type(collect)
        user32.EnumWindows(callback, 0)
        return matches[0] if matches else None

    def _hide_mt5_window(self) -> None:
        if self.mt5_window_hidden and self.mt5_window_hwnd and user32.IsWindow(self.mt5_window_hwnd):
            self._set_feedback("MT5 YA OCULTO")
            return
        hwnd = self._find_mt5_window()
        if not hwnd:
            self._set_feedback("NO ENCONTRÉ LA VENTANA MT5", "#e0a34a")
            return
        self.mt5_window_hwnd = hwnd
        user32.ShowWindow(hwnd, SW_HIDE)
        self.mt5_window_hidden = True
        self._set_feedback("MT5 OCULTO · SIGUE ACTIVO")

    def _show_mt5_window(self) -> None:
        hwnd = self.mt5_window_hwnd
        if not hwnd or not user32.IsWindow(hwnd):
            hwnd = self._find_mt5_window()
        if not hwnd:
            self.mt5_window_hidden = False
            return
        self.mt5_window_hwnd = hwnd
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.SetForegroundWindow(hwnd)
        self.mt5_window_hidden = False
        self._set_feedback("VENTANA MT5 VISIBLE")

    def _hotkey_loop(self) -> None:
        self.hotkey_thread_id = kernel32.GetCurrentThreadId()
        registered_g = bool(user32.RegisterHotKey(None, HOTKEY_TOGGLE,
                                                  MOD_CONTROL | MOD_ALT, VK_G))
        registered_q = bool(user32.RegisterHotKey(None, HOTKEY_QUIT,
                                                  MOD_CONTROL | MOD_ALT, VK_Q))
        registered_o = bool(user32.RegisterHotKey(None, HOTKEY_OBSERVER,
                                                  MOD_CONTROL | MOD_ALT, VK_O))
        self.hotkeys_registered = registered_g and registered_q and registered_o
        if not registered_g:
            self.hotkey_events.put("error_toggle")
        if not registered_q:
            self.hotkey_events.put("error_quit")
        if not registered_o:
            self.hotkey_events.put("error_observer")

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                if msg.wParam == HOTKEY_TOGGLE:
                    self.hotkey_events.put("toggle")
                elif msg.wParam == HOTKEY_QUIT:
                    self.hotkey_events.put("quit")
                elif msg.wParam == HOTKEY_OBSERVER:
                    self.hotkey_events.put("observer")
        user32.UnregisterHotKey(None, HOTKEY_TOGGLE)
        user32.UnregisterHotKey(None, HOTKEY_QUIT)
        user32.UnregisterHotKey(None, HOTKEY_OBSERVER)

    def _drain_hotkeys(self) -> None:
        if self.closing:
            return
        try:
            while True:
                event = self.hotkey_events.get_nowait()
                if event == "toggle":
                    self.show_panel() if not self.visible else self.hide_panel()
                elif event == "show_panel":
                    self.show_panel()
                elif event == "hide_mt5":
                    self._hide_mt5_window()
                elif event == "show_mt5":
                    self._show_mt5_window()
                elif event == "observer":
                    self._launch_observer()
                elif event == "quit":
                    self.shutdown()
                    return
                elif event.startswith("error_"):
                    message = ("Atajo Ctrl+Alt+O ocupado" if event == "error_observer"
                               else "Atajo ocupado; cierra otra app o cambia el atajo")
                    self.status_var.set(message)
        except queue.Empty:
            pass
        self._after(100, self._drain_hotkeys)

    def _launch_observer(self) -> None:
        if self.observer_process is not None and self.observer_process.poll() is None:
            return
        observer_script = Path(__file__).with_name("screen_observer.py")
        try:
            self.observer_process = subprocess.Popen(
                [sys.executable, str(observer_script)], cwd=str(observer_script.parent),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self._set_feedback("LECTOR TIKTOK ABIERTO")
        except OSError as exc:
            self._set_feedback(f"ERROR LECTOR: {exc}", "#ff7777")

    def _refresh_copy_state(self) -> None:
        if self.closing:
            return
        if read_observer_state is not None:
            state = read_observer_state()
            stamp = state.get("updated_at")
            try:
                age = (datetime.now().astimezone() - datetime.fromisoformat(stamp)).total_seconds()
                fresh = -1 <= age <= 2.0
            except (TypeError, ValueError):
                fresh = False
            self.copy_state = state
            self.copy_state_fresh = fresh
            try:
                counts_text = (f"B{int(state.get('copied_buys', 0))} "
                               f"S{int(state.get('copied_sells', 0))} "
                               f"C{int(state.get('closed_total', 0))}")
                self.copy_counts_var.set(counts_text + (" · OFF" if not fresh else ""))
            except (TypeError, ValueError):
                self.copy_counts_var.set("B— S— C—")
            active = bool(fresh and state.get("copy_active"))
            for key in ("symbol", "volume"):
                self.field_entries[key].configure(
                    state="disabled" if active else "normal")
            state_status = str(state.get("status", "")) if fresh else ""
            if (state_status and state_status != self.copy_last_status and
                    any(word in state_status.lower() for word in
                        ("error", "bloqueada", "pausada", "ambigu", "sin lectura"))):
                self._set_feedback(state_status[:48], "#e0a34a")
            self.copy_last_status = state_status
        self._after(500, self._refresh_copy_state)

    def _after(self, delay_ms: int, callback, *args) -> None:
        """Schedule Tk work only while its interpreter is alive."""
        if self.closing:
            return
        try:
            self.root.after(delay_ms, callback, *args)
        except tk.TclError:
            # A pending timer may race with an explicit shutdown.
            return

    def _get_taskbar_rect(self) -> tuple[int, int, int, int] | None:
        hwnd = user32.FindWindowW("Shell_TrayWnd", None)
        if not hwnd:
            return None
        rect = RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        self.taskbar_hwnd = hwnd
        return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top

    def show_panel(self) -> None:
        if self.visible:
            return
        bounds = self._get_taskbar_rect()
        if not bounds:
            self.status_var.set("No pude localizar la barra de Windows")
            return
        x, y, width, height = bounds
        if width < 40 or height < 40:
            self.status_var.set("La barra de Windows está oculta o no disponible")
            return
        self.taskbar_rect = bounds
        self._apply_responsive_layout(width, height)
        user32.ShowWindow(self.taskbar_hwnd, SW_HIDE)
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()
        self.visible = True
        self._refresh()

    def _apply_responsive_layout(self, width: int, height: int) -> None:
        self.narrow_layout = width < 105
        roomy_layout = height >= 650 and not self.narrow_layout
        tall_narrow_layout = self.narrow_layout and height >= 650
        usable_width = max(42, width - 6)
        for widget in (self.status_label, self.price_label, self.entry_price_label,
                       self.position_heading, self.position_label,
                       self.balance_label, self.daily_label, self.protection_label,
                       self.risk_label, self.feedback_label, self.copy_counts_label):
            widget.configure(wraplength=0 if self.narrow_layout else usable_width)
        for label in self.field_labels.values():
            label.configure(wraplength=0 if self.narrow_layout else usable_width)
        if self.narrow_layout:
            self.field_labels["symbol"].configure(text="PAR")
            self.field_labels["volume"].configure(text="LOTE")
            self.buy_button.configure(text="BUY")
            self.be_button.configure(text="BE")
        else:
            self.field_labels["symbol"].configure(text="Símbolo")
            self.field_labels["volume"].configure(text="Volumen")
            self.buy_button.configure(text="COMPRAR")
            self.be_button.configure(text="BREAKEVEN")
        self.price_label.configure(font=("Segoe UI", 13 if roomy_layout else (11 if tall_narrow_layout else (9 if self.narrow_layout else 14)), "bold"))
        self.entry_price_label.configure(font=("Segoe UI", 9 if roomy_layout else 8, "bold"))
        self.position_heading.configure(font=("Segoe UI", 9 if roomy_layout else (8 if tall_narrow_layout else (7 if self.narrow_layout else 9)), "bold"))
        self.position_label.configure(font=("Segoe UI", 9 if roomy_layout else (8 if tall_narrow_layout else (7 if self.narrow_layout else 8)), "bold"))
        self.balance_label.configure(font=("Segoe UI", 8 if roomy_layout else (7 if tall_narrow_layout else 6), "bold"))
        self.daily_label.configure(font=("Segoe UI", 8 if roomy_layout else (7 if tall_narrow_layout else 6), "bold"))
        self.protection_label.configure(font=("Segoe UI", 8 if roomy_layout else (7 if tall_narrow_layout else (6 if self.narrow_layout else 7)), "bold"))
        self.risk_label.configure(font=("Segoe UI", 8 if roomy_layout else (7 if tall_narrow_layout else (7 if self.narrow_layout else 7))))
        self.feedback_label.configure(font=("Segoe UI", 8 if roomy_layout else (7 if tall_narrow_layout else (6 if self.narrow_layout else 7)), "bold"))
        self.datetime_label.configure(font=("Segoe UI", 8 if roomy_layout else (7 if tall_narrow_layout else (6 if self.narrow_layout else 8)),
                                            "bold" if roomy_layout else "normal"),
                                      wraplength=0 if self.narrow_layout else usable_width)
        self.status_label.configure(font=("Segoe UI", 8 if roomy_layout else (7 if tall_narrow_layout else (6 if self.narrow_layout else 7)), "bold"))
        field_font = 7 if roomy_layout or tall_narrow_layout else (6 if self.narrow_layout else 7)
        entry_font = 10 if roomy_layout else (8 if self.narrow_layout else 9)
        button_font = 9 if roomy_layout else (8 if tall_narrow_layout else (7 if self.narrow_layout else 8))
        field_gap = 5 if roomy_layout else (2 if tall_narrow_layout else (0 if height < 550 else 2))
        entry_gap = 4 if roomy_layout else (1 if tall_narrow_layout else (0 if height < 550 else 1))
        button_gap = 5 if roomy_layout else (1 if tall_narrow_layout else (0 if height < 550 else 2))
        for label in self.field_labels.values():
            label.configure(font=("Segoe UI", field_font))
            label.pack_configure(pady=(field_gap, 0))
        for entry in self.field_entries.values():
            entry.configure(font=("Segoe UI", entry_font))
            entry.pack_configure(pady=(0, entry_gap), ipady=2 if roomy_layout else (1 if tall_narrow_layout else 0))
        for child in self.shell.winfo_children():
            if isinstance(child, tk.Button):
                child.configure(font=("Segoe UI", button_font, "bold"))
                child.pack_configure(ipady=4 if roomy_layout else (2 if tall_narrow_layout else (0 if height < 550 else 2)),
                                     pady=(button_gap, button_gap))
        close_font = 6 if self.narrow_layout else 7
        for button in (self.close_half_button, self.close_full_button):
            button.configure(font=("Segoe UI", close_font, "bold"), wraplength=usable_width)
        self.status_label.pack_configure(pady=(1, 3) if roomy_layout else ((0, 2) if tall_narrow_layout else (0, 1)))
        self.price_label.pack_configure(pady=(1, 0))
        self.entry_price_label.pack_configure(pady=(1, 7) if roomy_layout else ((0, 3) if tall_narrow_layout else (1, 4)))
        self.position_heading.pack_configure(pady=(10, 2) if roomy_layout else ((5, 1) if tall_narrow_layout else (4, 1)))
        self.position_label.pack_configure(pady=(1, 4) if roomy_layout else ((1, 2) if tall_narrow_layout else (1, 0)))
        self.balance_label.pack_configure(pady=(3, 1) if roomy_layout else ((2, 1) if tall_narrow_layout else (1, 0)))
        self.daily_label.pack_configure(pady=(1, 5) if roomy_layout else ((0, 3) if tall_narrow_layout else (0, 1)))
        self.protection_label.pack_configure(pady=(1, 5) if roomy_layout else ((0, 2) if tall_narrow_layout else (1, 1)))
        self.risk_label.pack_configure(pady=(3, 5) if roomy_layout else ((1, 2) if tall_narrow_layout else (1, 1)))
        self.feedback_label.pack_configure(pady=(3, 7) if roomy_layout else ((1, 3) if tall_narrow_layout else (1, 2)))
        self.datetime_label.pack_configure(pady=(4, 10) if roomy_layout else ((2, 4) if tall_narrow_layout else (2, 1)))
        self.hide_button.pack_configure(pady=(3, 8) if roomy_layout else ((2, 5) if tall_narrow_layout else (1, 3)))

    def hide_panel(self) -> None:
        if self.visible:
            self.root.withdraw()
            if self.taskbar_hwnd:
                user32.ShowWindow(self.taskbar_hwnd, SW_SHOW)
        self.visible = False

    def _ensure_mt5(self) -> bool:
        if mt5 is None:
            self.mt5_error = "Instala Python y el paquete MetaTrader5"
            return False
        try:
            terminal = mt5.terminal_info()
            account = mt5.account_info()
            if terminal is None or account is None:
                if mt5.initialize():
                    terminal = mt5.terminal_info()
                    account = mt5.account_info()
            if terminal is None or account is None:
                self.mt5_error = f"MT5 sin sesión: {mt5.last_error()}"
                return False
            if not terminal.connected:
                self.mt5_error = "MT5 sin conexión al servidor"
                return False
            self.mt5_error = "Conectado"
            self.account = account
            return True
        except Exception as exc:
            self.mt5_error = f"Error MT5: {exc}"
            return False

    def _set_protection_status(self, positions: tuple) -> None:
        has_sl = bool(positions) and all(float(p.sl or 0) > 0 for p in positions)
        has_tp = bool(positions) and all(float(p.tp or 0) > 0 for p in positions)
        if has_sl and has_tp:
            self.protection_var.set("SL + TP OK")
            self.protection_label.configure(fg="#57d99a")
        elif not has_sl and not has_tp:
            self.protection_var.set("FALTA SL / TP")
            self.protection_label.configure(fg="#ff7777")
        elif not has_sl:
            self.protection_var.set("FALTA SL")
            self.protection_label.configure(fg="#ff7777")
        else:
            self.protection_var.set("FALTA TP")
            self.protection_label.configure(fg="#e0a34a")

    def _daily_realized_profit(self) -> float | None:
        """Today's closed trading P/L, cached briefly to limit terminal queries."""
        now_local = datetime.now().astimezone()
        day_key = now_local.strftime("%Y-%m-%d")
        now_mono = time.monotonic()
        if (self.daily_realized_cache_date == day_key and
                now_mono - self.daily_realized_cache_at < 5):
            return self.daily_realized_cache
        start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
        try:
            deals = mt5.history_deals_get(start_local.astimezone(timezone.utc),
                                          now_local.astimezone(timezone.utc))
        except Exception:
            return None
        if deals is None:
            return None
        trade_types = {mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL}
        realized = sum(
            float(getattr(deal, "profit", 0.0) or 0.0) +
            float(getattr(deal, "commission", 0.0) or 0.0) +
            float(getattr(deal, "swap", 0.0) or 0.0) +
            float(getattr(deal, "fee", 0.0) or 0.0)
            for deal in deals if getattr(deal, "type", None) in trade_types)
        self.daily_realized_cache = realized
        self.daily_realized_cache_at = now_mono
        self.daily_realized_cache_date = day_key
        return realized

    def _refresh_account_summary(self) -> None:
        currency = getattr(self.account, "currency", "USD") or "USD"
        currency_text = "$" if currency == "USD" else f"{currency} "
        balance = float(getattr(self.account, "balance", 0.0) or 0.0)
        self.balance_var.set(f"SALDO\n{currency_text}{balance:,.2f}")

        realized = self._daily_realized_profit()
        if realized is None:
            self.daily_var.set("HOY\nSIN HISTORIAL")
            self.daily_label.configure(fg="#e0a34a")
            return
        try:
            open_positions = mt5.positions_get()
            floating = sum(float(p.profit or 0.0) +
                           float(getattr(p, "swap", 0.0) or 0.0)
                           for p in (open_positions or ()))
        except Exception:
            floating = 0.0
        daily = realized + floating
        sign = "+" if daily >= 0 else "−"
        self.daily_var.set(f"HOY {sign}{currency_text}{abs(daily):,.2f}")
        self.daily_label.configure(
            fg="#57d99a" if daily > 0 else "#ff7777" if daily < 0 else "#d0a95c")

    def _refresh(self) -> None:
        if self.closing:
            return
        try:
            self.datetime_var.set(datetime.now().strftime("%d/%m/%Y\n%H:%M:%S"))
            self._save_fields()
            selected_symbol = self.vars["symbol"].get().strip() or "XAUUSDm"
            self.symbol = selected_symbol
            if not self._ensure_mt5():
                self.mt5_ready = False
                self.bid = self.ask = None
                self.position = None
                self.positions = ()
                self.status_var.set("MT5\nOFFLINE" if self.narrow_layout else self.mt5_error[:60])
                self.status_label.configure(fg="#9aa6b2")
                self.price_var.set("—")
                self.price_label.configure(fg="#e8edf2")
                self.entry_price_var.set("PRECIO BUY / SELL\n—")
                self.position_var.set("SIN POSICIÓN")
                self.position_label.configure(fg="#8995a1")
                self.balance_var.set("SALDO\n—")
                self.daily_var.set("HOY\n—")
                self.daily_label.configure(fg="#8995a1")
                self.protection_var.set("SIN POSICIÓN")
                self.protection_label.configure(fg="#8995a1")
                self.risk_var.set("RIESGO AL SL\nSIN DATOS MT5")
                self._set_trading_enabled(False)
                self._after(1000, self._refresh)
                return

            info = mt5.symbol_info(self.symbol)
            if info is None:
                self.mt5_error = f"Símbolo no encontrado: {self.symbol}"
                self.status_var.set("SIMBOLO ERR" if self.narrow_layout else self.mt5_error[:60])
                self.status_label.configure(fg="#e0a34a")
                self.price_var.set("—")
                self.entry_price_var.set("PRECIO BUY / SELL\nREVISA PAR")
                self.position_var.set("SIN POSICIÓN")
                self.position_label.configure(fg="#8995a1")
                self.protection_var.set("SIN DATOS")
                self.protection_label.configure(fg="#e0a34a")
                self.risk_var.set("RIESGO AL SL\nPAR NO DISPONIBLE")
                self.mt5_ready = False
                self._set_trading_enabled(False)
                self._after(1000, self._refresh)
                return
            if not info.visible:
                mt5.symbol_select(self.symbol, True)
            tick = mt5.symbol_info_tick(self.symbol)
            if tick is None:
                self.status_var.set("SIN TICK" if self.narrow_layout else "Sin cotización de " + self.symbol)
                self.status_label.configure(fg="#e0a34a")
                self.price_var.set("—")
                self.price_label.configure(fg="#e8edf2")
                self.entry_price_var.set("PRECIO BUY / SELL\nSIN DATOS")
                self.position_var.set("SIN DATOS")
                self.position_label.configure(fg="#e0a34a")
                self.protection_var.set("SIN DATOS")
                self.protection_label.configure(fg="#e0a34a")
                self.risk_var.set("RIESGO AL SL\nSIN COTIZACIÓN")
                self.mt5_ready = False
                self._set_trading_enabled(False)
                self._after(1000, self._refresh)
                return

            self.bid, self.ask = float(tick.bid), float(tick.ask)
            digits = int(info.digits)
            self.price_var.set(f"{self.bid:.{digits}f}")
            self.price_label.configure(fg="#e8edf2")
            try:
                risk_volume = self._number("volume", required=False)
                risk_sl = self._number("sl", required=False)
            except ValueError:
                risk_volume = risk_sl = None
            self._refresh_risk_estimate(risk_volume, risk_sl, tick)
            mode = "DEMO" if self.account.trade_mode == mt5.ACCOUNT_TRADE_MODE_DEMO else "REAL"
            self.status_var.set(f"{mode}\nCONECTADO" if self.narrow_layout else f"● {mode} · EN VIVO")
            self.status_label.configure(fg="#57d99a")
            self._refresh_account_summary()
            positions = mt5.positions_get(symbol=self.symbol)
            if positions is None:
                positions = ()
            self.positions = tuple(positions)
            self.position = self.positions[0] if self.positions else None
            if len(positions) == 0:
                self.position_var.set("SIN POSICIÓN")
                self.position_label.configure(fg="#8995a1")
                self.protection_var.set("SIN POSICIÓN")
                self.protection_label.configure(fg="#8995a1")
                self.entry_price_var.set("PRECIO BUY / SELL\n—")
                self.price_label.configure(fg="#e8edf2")
                self.cruise_active = False
            else:
                direction_types = {p.type for p in self.positions}
                direction = ("BUY" if direction_types == {mt5.POSITION_TYPE_BUY}
                             else "SELL" if direction_types == {mt5.POSITION_TYPE_SELL}
                             else "MIXTA")
                total_profit = sum(float(p.profit or 0.0) for p in self.positions)
                outcome = "GANANDO" if total_profit > 0 else "PERDIENDO" if total_profit < 0 else "EN CERO"
                sign = "-" if total_profit < 0 else "+"
                currency = getattr(self.account, "currency", "USD") or "USD"
                amount = (f"{sign}${abs(total_profit):.2f}" if currency == "USD"
                          else f"{sign}{abs(total_profit):.2f} {currency}")
                count_text = f" ×{len(self.positions)}" if len(self.positions) > 1 else ""
                self.position_var.set(f"{direction}{count_text}\n{outcome}\n{amount}")
                self.position_label.configure(
                    fg="#57d99a" if total_profit > 0 else "#ff7777" if total_profit < 0 else "#d0a95c")
                self.price_label.configure(
                    fg="#57d99a" if total_profit > 0 else "#ff7777" if total_profit < 0 else "#e8edf2")
                if direction in ("BUY", "SELL"):
                    total_volume = sum(float(p.volume) for p in self.positions)
                    average_entry = sum(float(p.price_open) * float(p.volume)
                                        for p in self.positions) / total_volume
                    self.entry_price_var.set(f"PRECIO {direction}\n{average_entry:.{digits}f}")
                else:
                    self.entry_price_var.set("PRECIO BUY / SELL\nMIXTO")
                self._set_protection_status(self.positions)

            self.mt5_ready = True
            self._set_trading_enabled(True)
            total_profit = sum(float(p.profit or 0.0) for p in self.positions)
            self.cruise_button.configure(
                state="normal" if self.positions else "disabled",
                text=(("CRUZ ON" if self.cruise_active else "CRUZ")
                      if self.narrow_layout else
                      ("CRUCERO ACTIVO" if self.cruise_active else "MODO CRUCERO")))
            if self.cruise_active and self.positions:
                for position in self.positions:
                    self._update_cruise(position, info, tick)
        except Exception as exc:
            self.mt5_ready = False
            self.status_var.set(f"Error de lectura MT5: {exc}"[:60])
            self._set_trading_enabled(False)
        self._after(1000, self._refresh)

    def _save_fields(self) -> None:
        for key, var in self.vars.items():
            self.settings[key] = var.get().strip()
        try:
            save_settings(self.settings)
        except OSError:
            pass

    def _number(self, key: str, required: bool = True) -> float | None:
        value = self.vars[key].get().strip().replace(",", ".")
        if not value:
            if required:
                raise ValueError("Completa todos los campos requeridos.")
            return None
        try:
            result = float(value)
        except ValueError as exc:
            raise ValueError("Usa valores numéricos válidos.") from exc
        if result < 0:
            raise ValueError("El volumen y las distancias no pueden ser negativos.")
        return result

    def _positions(self):
        positions = mt5.positions_get(symbol=self.symbol)
        return tuple(positions or ())

    def _estimate_sl_loss(self, order_type: int, volume: float,
                          entry_price: float, sl_price: float) -> float | None:
        if mt5 is None:
            return None
        if order_type == mt5.ORDER_TYPE_BUY and sl_price >= entry_price:
            return None
        if order_type == mt5.ORDER_TYPE_SELL and sl_price <= entry_price:
            return None
        try:
            result = mt5.order_calc_profit(order_type, self.symbol, volume,
                                           entry_price, sl_price)
        except Exception:
            return None
        if result is None:
            return None
        return max(0.0, -float(result))

    def _format_money(self, value: float) -> str:
        currency = getattr(self.account, "currency", "USD") or "USD"
        if currency == "USD":
            return f"${value:,.2f}"
        return f"{value:,.2f} {currency}"

    def _refresh_risk_estimate(self, volume: float | None, sl: float | None,
                               tick) -> None:
        heading = "RIESGO TOTAL\n×10" if self.narrow_layout else "RIESGO TOTAL ×10"
        if volume is None or volume <= 0:
            self.risk_var.set(f"{heading}\nLOTE INVÁLIDO")
            return
        if sl is None:
            self.risk_var.set(f"{heading}\nSIN SL")
            return
        try:
            buy_loss = self._estimate_sl_loss(mt5.ORDER_TYPE_BUY, volume,
                                              float(tick.ask), sl)
            sell_loss = self._estimate_sl_loss(mt5.ORDER_TYPE_SELL, volume,
                                               float(tick.bid), sl)
            buy_loss = buy_loss * 10 if buy_loss is not None else None
            sell_loss = sell_loss * 10 if sell_loss is not None else None
        except Exception:
            buy_loss = sell_loss = None
        buy_text = self._format_money(buy_loss) if buy_loss is not None else "SL INVÁLIDO"
        sell_text = self._format_money(sell_loss) if sell_loss is not None else "SL INVÁLIDO"
        self.risk_var.set(f"{heading}\nBUY: {buy_text}\nSELL: {sell_text}")

    def _set_feedback(self, text: str, color: str = "#57d99a") -> None:
        if self.narrow_layout:
            text = {
                "ORDEN RECHAZADA": "ORDEN ERROR",
                "BUY ENVIADO": "BUY OK",
                "SELL ENVIADO": "SELL OK",
                "BE YA APLICADO": "BE OK",
                "BE APLICADO": "BE OK",
                "CRUCERO PAUSADO": "CRUZ OFF",
                "CRUCERO ACTIVO": "CRUZ ON",
                "ERROR EN CRUCERO": "CRUZ ERROR",
                "SL / TP APLICADOS": "SL/TP OK",
                "CIERRE RECHAZADO": "CIERRE ERROR",
                "CIERRE ENVIADO": "CERRANDO",
            }.get(text, text)
        self.feedback_generation += 1
        generation = self.feedback_generation
        self.feedback_var.set(text)
        self.feedback_label.configure(fg=color)

        def reset() -> None:
            if generation == self.feedback_generation:
                self.feedback_var.set("LISTO")
                self.feedback_label.configure(fg="#9aa6b2")

        self._after(5000, reset)

    def open_buy(self) -> None:
        self._open_market(mt5.ORDER_TYPE_BUY)

    def open_sell(self) -> None:
        self._open_market(mt5.ORDER_TYPE_SELL)

    def _open_market(self, order_type: int) -> None:
        if not self.mt5_ready or self.ask is None or self.bid is None:
            self._set_feedback("MT5 SIN CONEXIÓN", "#e0a34a")
            return
        if self._positions():
            self._set_feedback("YA HAY UNA POSICIÓN ABIERTA", "#e0a34a")
            return
        try:
            volume = self._number("volume")
            sl = self._number("sl", required=False)
            tp = self._number("tp", required=False)
            info = mt5.symbol_info(self.symbol)
            if volume is None or info is None:
                return
            if not (info.volume_min <= volume <= info.volume_max):
                raise ValueError(f"Volumen permitido: {info.volume_min:g} a {info.volume_max:g}.")
            if abs((volume / info.volume_step) - round(volume / info.volume_step)) > 1e-7:
                raise ValueError(f"El paso de volumen para {self.symbol} es {info.volume_step:g}.")
            side = "BUY" if order_type == mt5.ORDER_TYPE_BUY else "SELL"
            if sl is not None and sl <= 0:
                raise ValueError("El SL debe ser mayor que cero o dejarse vacío.")
            if tp is not None and tp <= 0:
                raise ValueError("El TP debe ser mayor que cero o dejarse vacío.")
            tick = mt5.symbol_info_tick(self.symbol)
            if tick is None:
                raise ValueError("No hay cotización actual para abrir la operación.")
            price = float(tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid)
            if order_type == mt5.ORDER_TYPE_BUY:
                if sl is not None and sl >= price:
                    raise ValueError("Para BUY, el SL debe quedar debajo del precio actual.")
                if tp is not None and tp <= price:
                    raise ValueError("Para BUY, el TP debe quedar encima del precio actual.")
                if sl is not None and tp is not None and sl >= tp:
                    raise ValueError("En BUY, el SL debe ser menor que el TP.")
            else:
                if sl is not None and sl <= price:
                    raise ValueError("Para SELL, el SL debe quedar encima del precio actual.")
                if tp is not None and tp >= price:
                    raise ValueError("Para SELL, el TP debe quedar debajo del precio actual.")
                if sl is not None and tp is not None and tp >= sl:
                    raise ValueError("En SELL, el TP debe ser menor que el SL.")
            completed = 0
            actual_volume = 0.0
            last_error = ""
            failed = 0
            accepted_codes = (mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_DONE_PARTIAL)
            for index in range(10):
                tick = mt5.symbol_info_tick(self.symbol)
                if tick is None:
                    last_error = "Sin cotización durante el envío"
                    failed += 1
                    continue
                price = float(tick.ask if order_type == mt5.ORDER_TYPE_BUY else tick.bid)
                request = {
                    "action": mt5.TRADE_ACTION_DEAL, "symbol": self.symbol,
                    "volume": volume, "type": order_type, "price": price,
                    "sl": sl or 0.0, "tp": tp or 0.0,
                    "deviation": 20, "magic": 71212026,
                    "comment": f"OroCopy {index + 1}/10",
                    "type_time": mt5.ORDER_TIME_GTC,
                    "type_filling": mt5.ORDER_FILLING_IOC,
                }
                result = mt5.order_send(request)
                if result is None or result.retcode not in accepted_codes:
                    last_error = (str(mt5.last_error()) if result is None else
                                  f"Retcode {result.retcode}: {result.comment}")
                    failed += 1
                    continue
                completed += 1
                actual_volume += float(result.volume or volume)

            if completed == 10:
                self._set_feedback(f"{side} 10/10 · {actual_volume:g} LOTES")
            elif completed:
                self._set_feedback(
                    f"{side} PARCIAL {completed}/10 · {actual_volume:g} LOTES · {failed} RECHAZADAS · {last_error}",
                    "#e0a34a")
            else:
                self._set_feedback(f"ORDEN RECHAZADA · {last_error}", "#ff7777")
        except ValueError as exc:
            self._set_feedback(f"REVISA LOS VALORES · {exc}", "#e0a34a")
        except Exception as exc:
            self._set_feedback(f"ERROR AL ENVIAR · {exc}", "#ff7777")

    def save_position_levels(self) -> None:
        if not self.mt5_ready:
            self._set_feedback("MT5 SIN CONEXIÓN", "#e0a34a")
            return
        positions = self._positions()
        if not positions:
            self._set_feedback("NO HAY POSICIONES ABIERTAS", "#e0a34a")
            return
        try:
            requested_sl = self._number("sl", required=False)
            requested_tp = self._number("tp", required=False)
            if requested_sl is None and requested_tp is None:
                raise ValueError("Escribe un SL o TP; los campos vacíos conservan el nivel actual.")
            if requested_sl is not None and requested_sl <= 0:
                raise ValueError("El SL debe ser mayor que cero.")
            if requested_tp is not None and requested_tp <= 0:
                raise ValueError("El TP debe ser mayor que cero.")

            info = mt5.symbol_info(self.symbol)
            tick = mt5.symbol_info_tick(self.symbol)
            if info is None or tick is None:
                raise ValueError("No hay datos actuales del símbolo para guardar niveles.")
            digits = int(info.digits)
            point = float(info.point or 0.0)
            min_gap = max(float(info.trade_stops_level) * point, point)
            planned_levels = []
            for position in positions:
                sl = (requested_sl if requested_sl is not None
                      else float(position.sl or 0.0))
                tp = (requested_tp if requested_tp is not None
                      else float(position.tp or 0.0))
                if position.type == mt5.POSITION_TYPE_BUY:
                    reference = float(tick.bid)
                    if sl and sl >= reference - min_gap:
                        raise ValueError("Para BUY, el SL debe quedar debajo del precio con distancia suficiente.")
                    if tp and tp <= reference + min_gap:
                        raise ValueError("Para BUY, el TP debe quedar encima del precio con distancia suficiente.")
                    if sl and tp and sl >= tp:
                        raise ValueError("En BUY, el SL debe ser menor que el TP.")
                else:
                    reference = float(tick.ask)
                    if sl and sl <= reference + min_gap:
                        raise ValueError("Para SELL, el SL debe quedar encima del precio con distancia suficiente.")
                    if tp and tp >= reference - min_gap:
                        raise ValueError("Para SELL, el TP debe quedar debajo del precio con distancia suficiente.")
                    if sl and tp and tp >= sl:
                        raise ValueError("En SELL, el TP debe ser menor que el SL.")
                planned_levels.append((position, round(sl, digits) if sl else 0.0,
                                       round(tp, digits) if tp else 0.0))

            succeeded = 0
            first_error = ""
            for position, sl, tp in planned_levels:
                if self._modify_position(position, sl, tp, quiet=True):
                    succeeded += 1
                elif not first_error:
                    first_error = "algún cambio fue rechazado por el bróker"
            if succeeded:
                if self.cruise_active:
                    self.cruise_active = False
                    self.cruise_button.configure(
                        text="CRUZ" if self.narrow_layout else "MODO CRUCERO")
                self._set_feedback(f"SL / TP GUARDADOS {succeeded}/{len(positions)}")
            else:
                self._set_feedback(f"NO GUARDADO · {first_error or 'sin cambios'}", "#ff7777")
        except ValueError as exc:
            self._set_feedback(f"NO GUARDADO · {exc}", "#e0a34a")

    def move_to_breakeven(self) -> None:
        if not self.mt5_ready or not self.positions:
            self._set_feedback("NO HAY POSICIONES ABIERTAS", "#e0a34a")
            return
        succeeded = already = 0
        for position in self.positions:
            target = float(position.price_open)
            current_sl = float(position.sl or 0.0)
            if ((position.type == mt5.POSITION_TYPE_BUY and current_sl >= target) or
                    (position.type == mt5.POSITION_TYPE_SELL and current_sl != 0 and current_sl <= target)):
                already += 1
                continue
            if self._modify_position(position, target, float(position.tp or 0.0), quiet=True):
                succeeded += 1
        if succeeded:
            self.cruise_active = False
            self.cruise_button.configure(text="CRUZ" if self.narrow_layout else "MODO CRUCERO")
            self._set_feedback(f"BREAKEVEN {succeeded}/{len(self.positions)}")
        elif already:
            self._set_feedback(f"BE YA APLICADO {already}/{len(self.positions)}", "#d0a95c")
        else:
            self._set_feedback("BREAKEVEN RECHAZADO", "#ff7777")

    def toggle_cruise(self) -> None:
        if not self.mt5_ready or not self.positions:
            self._set_feedback("NO HAY POSICIONES ABIERTAS", "#e0a34a")
            return
        if self.cruise_active:
            self.cruise_active = False
            self.cruise_button.configure(text="CRUZ" if self.narrow_layout else "MODO CRUCERO")
            self._set_feedback("CRUCERO PAUSADO", "#d0a95c")
            return
        try:
            trigger = self._number("cruise_trigger")
            sl_distance = self._number("cruise_sl_distance")
            tp_distance = self._number("cruise_tp_distance")
            total_profit = sum(float(p.profit or 0.0) for p in self.positions)
            if sl_distance is None or tp_distance is None or sl_distance <= 0 or tp_distance <= 0:
                raise ValueError("Configura una distancia positiva para SL móvil y extensión del TP.")
            if trigger is None or total_profit < trigger:
                raise ValueError("La ganancia total aún no alcanza el umbral de crucero.")
            if total_profit <= 0:
                raise ValueError("La operación debe estar en ganancia para activar Crucero.")
            self.cruise_active = True
            self.cruise_button.configure(text="CRUZ ON" if self.narrow_layout else "CRUCERO ACTIVO")
            self._set_feedback("CRUCERO ACTIVO")
        except ValueError as exc:
            self._set_feedback(f"CRUCERO · {exc}", "#e0a34a")

    def _update_cruise(self, p, info, tick) -> None:
        try:
            trigger = self._number("cruise_trigger")
            sl_distance = self._number("cruise_sl_distance")
            tp_distance = self._number("cruise_tp_distance")
            if trigger is None or sl_distance is None or tp_distance is None:
                return
            if p.profit < trigger:
                return
            point = float(info.point)
            min_gap = max(float(info.trade_stops_level) * point, point)
            digits = int(info.digits)
            if p.type == mt5.POSITION_TYPE_BUY:
                old_sl = float(p.sl or 0.0)
                candidate_sl = max(float(p.price_open), float(tick.bid) - sl_distance)
                if float(tick.bid) - min_gap >= float(p.price_open):
                    candidate_sl = min(candidate_sl, float(tick.bid) - min_gap)
                    next_sl = max(old_sl, candidate_sl)
                else:
                    next_sl = old_sl
                candidate_tp = float(tick.bid) + tp_distance
                old_tp = float(p.tp or 0.0)
                next_tp = max(old_tp, candidate_tp)
                update_step = max(point * 10, point / 2)
                improved = next_sl > old_sl + update_step or next_tp > old_tp + update_step
            else:
                old_sl = float(p.sl or 0.0)
                candidate_sl = min(float(p.price_open), float(tick.ask) + sl_distance)
                if float(tick.ask) + min_gap <= float(p.price_open):
                    candidate_sl = max(candidate_sl, float(tick.ask) + min_gap)
                    next_sl = candidate_sl if old_sl == 0 else min(old_sl, candidate_sl)
                else:
                    next_sl = old_sl
                candidate_tp = float(tick.ask) - tp_distance
                old_tp = float(p.tp or 0.0)
                next_tp = candidate_tp if old_tp == 0 else min(old_tp, candidate_tp)
                update_step = max(point * 10, point / 2)
                improved = ((old_sl == 0 and next_sl > 0) or next_sl < old_sl - update_step or
                            old_tp == 0 or next_tp < old_tp - update_step)
            if improved:
                self._modify_position(p, round(next_sl, digits), round(next_tp, digits), quiet=True)
        except (ValueError, AttributeError):
            return

    def _modify_position(self, position, sl: float, tp: float, quiet: bool = False) -> bool:
        request = {"action": mt5.TRADE_ACTION_SLTP, "symbol": self.symbol,
                   "position": position.ticket, "sl": sl, "tp": tp}
        result = mt5.order_send(request)
        if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
            detail = mt5.last_error() if result is None else f"Retcode {result.retcode}: {result.comment}"
            if quiet:
                if self.cruise_active:
                    self.cruise_active = False
                    self.cruise_button.configure(
                        text="CRUZ" if self.narrow_layout else "MODO CRUCERO")
                self._set_feedback(f"CAMBIO RECHAZADO · {detail}", "#ff7777")
            else:
                self._set_feedback(f"CAMBIO RECHAZADO · {detail}", "#ff7777")
            return False
        if not quiet:
            self._set_feedback("SL / TP APLICADOS")
        return True

    def close_half(self) -> None:
        self._close_fraction(0.5)

    def close_half_protected(self) -> None:
        if not self._close_fraction(0.5):
            return
        remaining_positions = self._positions()
        if not remaining_positions:
            self._set_feedback("50% CERRADO · SIN POSICIONES RESTANTES")
            return
        succeeded = already = 0
        for position in remaining_positions:
            target = float(position.price_open)
            current_sl = float(position.sl or 0.0)
            if ((position.type == mt5.POSITION_TYPE_BUY and current_sl >= target) or
                    (position.type == mt5.POSITION_TYPE_SELL and current_sl != 0 and current_sl <= target)):
                already += 1
                continue
            if self._modify_position(position, target, float(position.tp or 0.0), quiet=True):
                succeeded += 1
        if succeeded or already == len(remaining_positions):
            self.cruise_active = False
            self.cruise_button.configure(text="CRUZ" if self.narrow_layout else "MODO CRUCERO")
            self._set_feedback(f"50% CERRADO · BE {succeeded + already}/{len(remaining_positions)}")
        else:
            self._set_feedback(f"50% CERRADO · BE PARCIAL {succeeded}/{len(remaining_positions)}",
                               "#e0a34a")

    def close_position(self) -> None:
        self._close_fraction(1.0)

    def _close_fraction(self, fraction: float) -> bool:
        if not self.mt5_ready:
            self._set_feedback("MT5 SIN CONEXIÓN", "#e0a34a")
            return False
        positions = self._positions()
        if not positions:
            self._set_feedback("NO HAY POSICIONES ABIERTAS", "#e0a34a")
            return False
        info = mt5.symbol_info(self.symbol)
        if info is None or float(info.volume_step or 0) <= 0:
            self._set_feedback("NO PUDE LEER EL LOTE MÍNIMO", "#ff7777")
            return False

        total_volume = sum(float(p.volume) for p in positions)
        target_volume = total_volume * fraction
        step = float(info.volume_step)
        min_volume = float(info.volume_min)
        target_steps = round(target_volume / step)
        aligned_target = target_steps * step
        if abs(aligned_target - target_volume) > max(step * 1e-6, 1e-8):
            self._set_feedback("50% NO ALINEA CON EL PASO DE LOTE", "#e0a34a")
            return False
        if aligned_target < min_volume:
            self._set_feedback("VOLUMEN INSUFICIENTE PARA CERRAR 50%", "#e0a34a")
            return False

        remaining = aligned_target
        closed_volume = 0.0
        completed_positions = 0
        first_error = ""
        ordered_positions = sorted(positions, key=lambda p: (getattr(p, "time", 0), p.ticket))
        for position in ordered_positions:
            if remaining < min_volume - 1e-9:
                break
            request_volume = min(float(position.volume), remaining)
            request_steps = int((request_volume + step * 1e-7) / step)
            request_volume = round(request_steps * step, 8)
            if request_volume < min_volume - 1e-9:
                continue
            tick = mt5.symbol_info_tick(self.symbol)
            if tick is None:
                first_error = "SIN COTIZACIÓN PARA CERRAR"
                break
            order_type = (mt5.ORDER_TYPE_SELL if position.type == mt5.POSITION_TYPE_BUY
                          else mt5.ORDER_TYPE_BUY)
            price = float(tick.bid if order_type == mt5.ORDER_TYPE_SELL else tick.ask)
            request = {
                "action": mt5.TRADE_ACTION_DEAL, "symbol": self.symbol,
                "position": position.ticket, "volume": request_volume,
                "type": order_type, "price": price, "deviation": 20,
                "magic": 71212026,
                "comment": "OroCopy close 50%" if fraction == 0.5 else "OroCopy close 100%",
                "type_time": mt5.ORDER_TIME_GTC,
                "type_filling": mt5.ORDER_FILLING_IOC,
            }
            result = mt5.order_send(request)
            if result is None or result.retcode not in (
                    mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_DONE_PARTIAL):
                first_error = (str(mt5.last_error()) if result is None else
                               f"Retcode {result.retcode}: {result.comment}")
                continue
            actual = float(result.volume or request_volume)
            closed_volume += actual
            remaining = max(0.0, remaining - actual)
            completed_positions += 1

        if fraction == 1.0 and closed_volume > 0:
            self.cruise_active = False
            self.cruise_button.configure(text="CRUZ" if self.narrow_layout else "MODO CRUCERO")
        if closed_volume + 1e-8 >= aligned_target:
            label = "CERRÓ 50%" if fraction == 0.5 else "CERRÓ 100%"
            self._set_feedback(f"{label} · {completed_positions} TICKETS · {closed_volume:g} LOTES")
        elif closed_volume > 0:
            label = "CIERRE PARCIAL 50%" if fraction == 0.5 else "CIERRE PARCIAL"
            self._set_feedback(f"{label} · {closed_volume:g}/{aligned_target:g} LOTES · {first_error}",
                               "#e0a34a")
        else:
            self._set_feedback(f"CIERRE RECHAZADO · {first_error or 'sin ejecución'}", "#ff7777")
        self.positions = self._positions()
        self._set_trading_enabled(self.mt5_ready)
        return closed_volume + 1e-8 >= aligned_target

    def shutdown(self) -> None:
        if self.closing:
            return
        self.closing = True
        if self.copy_state_fresh and self.copy_state.get("copy_active") and send_observer_command:
            send_observer_command("pause")
        self.hide_panel()
        if self.mt5_window_hidden:
            self._show_mt5_window()
        if self.tray_icon is not None:
            try:
                self.tray_icon.stop()
            except Exception:
                pass
        try:
            if mt5 is not None:
                mt5.shutdown()
        except Exception:
            pass
        if self.hotkey_thread_id:
            user32.PostThreadMessageW(self.hotkey_thread_id, WM_QUIT, 0, 0)
        try:
            self.root.destroy()
        except tk.TclError:
            pass
        if self.instance_mutex:
            kernel32.CloseHandle(self.instance_mutex)
            self.instance_mutex = None


if __name__ == "__main__":
    GoldControl()
