"""Einstellungen und Pfade."""
import os
import tomllib
from dataclasses import asdict, dataclass, fields
from pathlib import Path

HOME = Path.home()
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config")) / "klipp"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", HOME / ".local/share")) / "klipp"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", HOME / ".cache")) / "klipp"
CONFIG_FILE = CONFIG_DIR / "config.toml"
INDEX_FILE = DATA_DIR / "index.json"
THUMB_DIR = CACHE_DIR / "thumbs"

APP_ID = "dev.klipp.Klipp"


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    d = Path(base) / "klipp"
    d.mkdir(parents=True, exist_ok=True)
    return d


def socket_path() -> Path:
    return runtime_dir() / "gsr.sock"


def state_path() -> Path:
    return runtime_dir() / "state.json"


# Felder, deren Änderung einen Neustart des Recorders braucht
RECORDER_KEYS = ("clip_seconds", "fps", "quality", "codec", "monitor",
                 "desktop_audio", "microphone", "cursor")


@dataclass
class Config:
    clip_seconds: int = 30
    fps: int = 60
    quality: str = "very_high"      # medium | high | very_high | ultra
    codec: str = "h264"             # h264 | hevc | av1
    monitor: str = "auto"           # auto | DP-1 | ...
    desktop_audio: bool = True
    microphone: bool = False
    cursor: bool = True
    hotkey: str = "ALT+F10"         # leer = kein Hotkey
    library: str = "~/Videos/Klipp"
    discord_limit_mb: int = 10
    notifications: bool = True
    sound: bool = True

    @property
    def library_path(self) -> Path:
        return Path(os.path.expanduser(self.library))

    @classmethod
    def load(cls) -> "Config":
        cfg = cls()
        try:
            data = tomllib.loads(CONFIG_FILE.read_text())
        except (FileNotFoundError, tomllib.TOMLDecodeError):
            return cfg
        for f in fields(cls):
            if f.name in data and isinstance(data[f.name], type(getattr(cfg, f.name))):
                setattr(cfg, f.name, data[f.name])
        return cfg

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        lines = ["# Klipp – Einstellungen"]
        for key, value in asdict(self).items():
            if isinstance(value, bool):
                lines.append(f"{key} = {'true' if value else 'false'}")
            elif isinstance(value, int):
                lines.append(f"{key} = {value}")
            else:
                escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
                lines.append(f'{key} = "{escaped}"')
        tmp = CONFIG_FILE.with_suffix(".tmp")
        tmp.write_text("\n".join(lines) + "\n")
        tmp.replace(CONFIG_FILE)

    def recorder_signature(self) -> tuple:
        return tuple(getattr(self, k) for k in RECORDER_KEYS)
