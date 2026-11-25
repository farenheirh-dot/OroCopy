"""Loopback-only receiver for frames from the OroCopy Chrome extension."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path
import secrets
import threading


HOST = "127.0.0.1"
PORT = 39872
TOKEN_FILE = "chrome_capture_token.txt"
MAX_FRAME_BYTES = 5 * 1024 * 1024


class ChromeCaptureServer:
    def __init__(self, data_dir: Path, on_frame, is_accepting) -> None:
        self.data_dir = data_dir
        self.on_frame = on_frame
        self.is_accepting = is_accepting
        self.token = self._load_token()
        self.server = None
        self.thread = None
        self.error = None

    def _load_token(self) -> str:
        path = self.data_dir / TOKEN_FILE
        try:
            value = path.read_text(encoding="utf-8").strip()
            if len(value) >= 32:
                return value
        except OSError:
            pass
        value = secrets.token_urlsafe(32)
        path.write_text(value, encoding="utf-8")
        return value

    def start(self) -> None:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format, *_args):
                return

            def _origin(self) -> str | None:
                origin = self.headers.get("Origin", "")
                if not origin.startswith("chrome-extension://"):
                    return None
                return origin

            def _send_cors_headers(self, origin: str) -> None:
                self.send_header("Access-Control-Allow-Origin", origin)
                self.send_header("Vary", "Origin")

            def do_OPTIONS(self):
                origin = self._origin()
                if origin is None:
                    self.send_error(403, "Chrome extension origin required")
                    return
                self.send_response(204)
                self._send_cors_headers(origin)
                self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, X-OroCopy-Token")
                self.send_header("Access-Control-Max-Age", "600")
                self.end_headers()

            def do_POST(self):
                if self.path != "/frame":
                    self.send_error(404)
                    return
                origin = self._origin()
                if origin is None:
                    self.send_error(403, "Chrome extension origin required")
                    return
                supplied = self.headers.get("X-OroCopy-Token", "")
                if not secrets.compare_digest(supplied, owner.token):
                    self.send_error(401, "Invalid OroCopy pairing token")
                    return
                if not owner.is_accepting():
                    self.send_error(409, "Start Chrome reading in OroCopy first")
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    size = 0
                if size <= 0 or size > MAX_FRAME_BYTES:
                    self.send_error(413, "Frame size is invalid")
                    return
                try:
                    from PIL import Image
                    raw = self.rfile.read(size)
                    source = Image.open(BytesIO(raw))
                    if source.width > 3840 or source.height > 2160:
                        self.send_error(413, "Frame dimensions are too large")
                        return
                    source.verify()
                    image = Image.open(BytesIO(raw)).convert("RGB")
                    dimensions = {}
                    for side in ("Buy", "Sell"):
                        for dimension in ("Width", "Height"):
                            header = self.headers.get(f"X-OroCopy-{side}-{dimension}")
                            if header is not None:
                                dimensions[f"{side.lower()}_{dimension.lower()}"] = int(header)
                    if len(dimensions) == 4:
                        buy_width = dimensions["buy_width"]
                        buy_height = dimensions["buy_height"]
                        sell_width = dimensions["sell_width"]
                        sell_height = dimensions["sell_height"]
                        if (min(buy_width, buy_height, sell_width, sell_height) < 1 or
                                buy_width + sell_width > image.width or
                                max(buy_height, sell_height) > image.height):
                            self.send_error(400, "Crop dimensions are invalid")
                            return
                        frames = (
                            image.crop((0, 0, buy_width, buy_height)),
                            image.crop((buy_width, 0, buy_width + sell_width, sell_height)),
                        )
                        accepted = owner.on_frame(frames)
                    else:
                        # Older extension versions still send one combined crop.
                        accepted = owner.on_frame(image)
                except Exception:
                    self.send_error(400, "Invalid image frame")
                    return
                if not accepted:
                    self.send_error(429, "Frame queue is busy")
                    return
                self.send_response(204)
                self._send_cors_headers(origin)
                self.end_headers()

        try:
            self.server = ThreadingHTTPServer((HOST, PORT), Handler)
            self.server.daemon_threads = True
            self.thread = threading.Thread(target=self.server.serve_forever,
                                           name="OroCopyChromeFrameServer", daemon=True)
            self.thread.start()
        except OSError as exc:
            self.error = str(exc)

    @property
    def available(self) -> bool:
        return self.server is not None

    @property
    def url(self) -> str:
        return f"http://{HOST}:{PORT}/frame"

    def close(self) -> None:
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server = None
