"""Globaler Hotkey – von Klipp selbst verwaltet.

Primär: Laufzeit-Bind in Hyprland (`hyprctl keyword bind`), kein Config-Eintrag nötig.
Hyprland verwirft Laufzeit-Binds bei `hyprctl reload`, deshalb prüft der Daemon
regelmässig und setzt ihn neu.
Fallback (andere Compositoren / Bind abgelehnt): Tastatur direkt über /dev/input
lesen – braucht Lesezugriff (Gruppe `input`).
"""
import glob
import json
import os
import shutil
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

from .config import Config

MODS = ("SUPER", "CTRL", "ALT", "SHIFT")
MOD_MASK = {"SHIFT": 1, "CTRL": 4, "ALT": 8, "SUPER": 64}


# ---------------------------------------------------------------- Format

def parse(combo: str) -> tuple[list[str], str] | None:
    """'ALT+F10' -> (['ALT'], 'F10'); leer/ungültig -> None."""
    parts = [p.strip() for p in combo.split("+") if p.strip()]
    if not parts:
        return None
    mods = [p.upper() for p in parts[:-1]]
    if any(m not in MODS for m in mods):
        return None
    return mods, parts[-1]


def pretty(combo: str) -> str:
    p = parse(combo)
    if not p:
        return "Kein Hotkey"
    names = {"SUPER": "Super", "CTRL": "Strg", "ALT": "Alt", "SHIFT": "Shift"}
    key = p[1] if len(p[1]) > 1 else p[1].upper()
    return " + ".join([names[m] for m in p[0]] + [key])


def save_command() -> str:
    exe = shutil.which("klipp")
    if exe:
        return f"{exe} save"
    pkg_root = Path(__file__).resolve().parent.parent
    return f"env PYTHONPATH={pkg_root} {sys.executable} -m klipp save"


# ---------------------------------------------------------------- Hyprland

def _hyprctl(*args) -> str:
    try:
        return subprocess.run(["hyprctl", *args], capture_output=True, text=True,
                              timeout=3).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def hyprland_available() -> bool:
    return bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")) and bool(shutil.which("hyprctl"))


def _hypr_bound(mods: list[str], key: str, cmd: str) -> bool:
    try:
        binds = json.loads(_hyprctl("binds", "-j") or "[]")
    except json.JSONDecodeError:
        return False
    mask = sum(MOD_MASK[m] for m in mods)
    return any(b.get("modmask") == mask and b.get("key", "").lower() == key.lower()
               and b.get("dispatcher") == "exec" and b.get("arg") == cmd for b in binds)


def hypr_bind(combo: str) -> bool:
    p = parse(combo)
    if not p:
        return False
    mods, key = p
    cmd = save_command()
    if _hypr_bound(mods, key, cmd):
        return True
    _hyprctl("keyword", "bind", f"{' '.join(mods)}, {key}, exec, {cmd}")
    return _hypr_bound(mods, key, cmd)


def hypr_unbind(combo: str) -> None:
    p = parse(combo)
    if p:
        _hyprctl("keyword", "unbind", f"{' '.join(p[0])}, {p[1]}")


def hypr_conflict(combo: str) -> str | None:
    """Beschreibung eines fremden Binds auf derselben Kombination, sonst None."""
    p = parse(combo)
    if not p or not hyprland_available():
        return None
    try:
        binds = json.loads(_hyprctl("binds", "-j") or "[]")
    except json.JSONDecodeError:
        return None
    mask = sum(MOD_MASK[m] for m in p[0])
    for b in binds:
        if (b.get("modmask") == mask and b.get("key", "").lower() == p[1].lower()
                and "klipp" not in b.get("arg", "")):
            return b.get("description") or f"{b.get('dispatcher')} {b.get('arg', '')}".strip()
    return None


# ---------------------------------------------------------------- evdev-Fallback

EVENT = struct.Struct("llHHi")
EV_KEY = 1
# Linux-Keycodes der Modifier und einer Auswahl an Tasten (xkb-Name -> Code)
MOD_CODES = {29: "CTRL", 97: "CTRL", 56: "ALT", 100: "ALT", 42: "SHIFT", 54: "SHIFT",
             125: "SUPER", 126: "SUPER"}
KEY_CODES = {**{f"F{i}": c for i, c in zip(range(1, 11), range(59, 69))},
             "F11": 87, "F12": 88, "Print": 99, "Pause": 119, "Scroll_Lock": 70,
             "Insert": 110, "Home": 102, "End": 107, "Page_Up": 104, "Page_Down": 109,
             "Delete": 111, "space": 57, "Tab": 15, "grave": 41,
             **{str(d): c for d, c in zip("1234567890", range(2, 12))},
             **{ch: c for ch, c in zip("qwertyuiop", range(16, 26))},
             **{ch: c for ch, c in zip("asdfghjkl", range(30, 39))},
             **{ch: c for ch, c in zip("zxcvbnm", range(44, 51))}}


def _keyboards() -> list[str]:
    devs = []
    try:
        blocks = Path("/proc/bus/input/devices").read_text().split("\n\n")
    except OSError:
        return devs
    for block in blocks:
        if "EV=120013" not in block and "EV=12001f" not in block:
            continue
        for line in block.splitlines():
            if line.startswith("H: Handlers="):
                for h in line.split("=", 1)[1].split():
                    if h.startswith("event"):
                        devs.append(f"/dev/input/{h}")
    return devs


def evdev_possible() -> bool:
    devs = _keyboards()
    return bool(devs) and all(os.access(d, os.R_OK) for d in devs)


class EvdevListener:
    def __init__(self, combo: str, on_press):
        p = parse(combo)
        self.mods = set(p[0]) if p else set()
        self.code = (KEY_CODES.get(p[1]) or KEY_CODES.get(p[1].lower())) if p else None
        self.on_press = on_press
        self.held: set[str] = set()
        self.stop = threading.Event()

    def start(self) -> bool:
        if self.code is None:
            return False
        for dev in _keyboards():
            threading.Thread(target=self._read, args=(dev,), daemon=True).start()
        return True

    def _read(self, dev: str):
        try:
            f = open(dev, "rb", buffering=0)
        except OSError:
            return
        with f:
            while not self.stop.is_set():
                data = f.read(EVENT.size)
                if len(data) < EVENT.size:
                    return
                _, _, etype, code, value = EVENT.unpack(data)
                if etype != EV_KEY:
                    continue
                if code in MOD_CODES:
                    (self.held.add if value else self.held.discard)(MOD_CODES[code])
                elif code == self.code and value == 1 and self.held == self.mods:
                    self.on_press()


# ---------------------------------------------------------------- Daemon-Thread

class HotkeyManager(threading.Thread):
    """Hält den Hotkey aus der Config aktiv und meldet den Zustand."""

    def __init__(self, report):
        super().__init__(daemon=True)
        self.report = report  # report(mode, error)
        self.current = None
        self.mode = None
        self.listener: EvdevListener | None = None
        self.quit = threading.Event()

    def run(self):
        while not self.quit.is_set():
            try:
                self._tick()
            except Exception as e:  # noqa: BLE001
                self.report(None, str(e))
            self.quit.wait(3)
        self._release()

    def _tick(self):
        combo = Config.load().hotkey
        if combo != self.current:
            self._release()
            self.current = combo
        if not parse(combo):
            self.report("off", None)
            return
        if hyprland_available() and self.mode in (None, "hyprland"):
            if hypr_bind(combo):
                self.mode = "hyprland"
                self.report("hyprland", None)
                return
        if self.mode != "evdev":
            if evdev_possible():
                self.listener = EvdevListener(combo, self._fire)
                if self.listener.start():
                    self.mode = "evdev"
                    self.report("evdev", None)
                    return
            self.mode = None
            self.report(None, "Hotkey konnte nicht gesetzt werden")

    def _fire(self):
        from .recorder import _pkg_env
        subprocess.Popen([sys.executable, "-m", "klipp", "save"], env=_pkg_env(),
                         start_new_session=True, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)

    def _release(self):
        if self.current and self.mode == "hyprland":
            hypr_unbind(self.current)
        if self.listener:
            self.listener.stop.set()
            self.listener = None
        self.mode = None

    def shutdown(self):
        self.quit.set()
        self._release()
