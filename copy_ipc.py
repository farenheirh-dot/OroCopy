"""Small file-based IPC between the OroCopy toolbar and screen observer."""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import uuid


def data_dir() -> Path:
    root = Path(os.environ.get("APPDATA", str(Path.home()))) / "OroCopy"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _read(name: str) -> dict:
    try:
        value = json.loads((data_dir() / name).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _write(name: str, value: dict) -> None:
    path = data_dir() / name
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def read_observer_state() -> dict:
    return _read("observer_state.json")


def write_observer_state(value: dict) -> None:
    value = dict(value)
    value["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    _write("observer_state.json", value)


def send_observer_command(action: str, **details) -> str:
    command_id = uuid.uuid4().hex
    _write("observer_command.json", {
        "id": command_id,
        "action": action,
        "sent_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        **details,
    })
    return command_id


def read_observer_command() -> dict:
    return _read("observer_command.json")
