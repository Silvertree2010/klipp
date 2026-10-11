"""Hintergrund-Recorder (gpu-screen-recorder im Replay-Modus) und Clip-Speichern."""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from . import library
from .config import Config, socket_path, state_path

GSR = "gpu-screen-recorder"
WAYLAND_VARS = ("WAYLAND_DISPLAY", "DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE",
                "XDG_CURRENT_DESKTOP", "XDG_SESSION_TYPE", "DBUS_SESSION_BUS_ADDRESS")
SOUND = "/usr/share/sounds/freedesktop/stereo/camera-shutter.oga"


# ---------------------------------------------------------------- IPC zu gsr

def ipc(name: str, data=None, timeout: float = 20.0) -> dict:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(str(socket_path()))
        s.sendall(json.dumps({"id": 1, "name": name, "data": data}).encode() + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(4096)
            if not chunk:
                break
            buf += chunk
    return json.loads(buf or b"{}")


def read_state() -> dict:
    try:
        st = json.loads(state_path().read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {"running": False}
    pid = st.get("daemon_pid")
    if not pid or not Path(f"/proc/{pid}").exists():
        return {"running": False}
    st["running"] = st.get("recording", False) and socket_path().exists()
    return st


def reload_daemon() -> bool:
    pid = read_state().get("daemon_pid")
    if not pid:
        return False
    try:
        os.kill(pid, signal.SIGHUP)
        return True
    except OSError:
        return False


# ---------------------------------------------------------------- Speichern

def save_clip(seconds: int | None = None, notify: bool = True) -> Path:
    cfg = Config.load()
    win = library.active_window()
    game = library.detect_game(win)
    if not socket_path().exists():
        raise RuntimeError("Instant Replay läuft nicht.")
    data = {"seconds": seconds} if seconds else None
    reply = ipc("save-replay", data)
    if reply.get("result") != "ok" or not reply.get("data"):
        raise RuntimeError(f"Speichern fehlgeschlagen: {reply.get('data') or reply}")
    clip = library.ingest(Path(reply["data"]), game, cfg)
    if notify:
        _announce(clip, game, cfg)
    return clip


def _announce(clip: Path, game: str, cfg: Config) -> None:
    if cfg.sound and Path(SOUND).exists() and shutil.which("pw-play"):
        subprocess.Popen(["pw-play", SOUND], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
    if cfg.notifications and shutil.which("notify-send"):
        # eigener Prozess wartet auf Klick, damit der Hotkey sofort zurückkehrt
        subprocess.Popen([sys.executable, "-m", "klipp", "_notify", str(clip), game],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, env=_pkg_env())


def _pkg_env() -> dict:
    env = dict(os.environ)
    pkg_root = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = pkg_root + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    return env


def notify_and_wait(clip: str, game: str) -> None:
    thumb = library.thumbnail_path(Path(clip))
    duration = library.read_index().get(clip, {}).get("duration")
    body = f"{game} · {round(duration)} s" if duration else game
    cmd = ["notify-send", "-a", "Klipp", "-i", str(thumb) if thumb.exists() else "camera-video",
           "-A", "default=Ansehen", "--wait", "Clip gespeichert", body]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=600).stdout.strip()
    except subprocess.SubprocessError:
        return
    if out in ("default", "open"):
        subprocess.Popen([sys.executable, "-m", "klipp", "open", clip], start_new_session=True,
                         env=_pkg_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ---------------------------------------------------------------- Daemon

def _adopt_session_env() -> bool:
    """Unter systemd fehlt die Wayland-Umgebung, bis Hyprland sie importiert hat."""
    if os.environ.get("WAYLAND_DISPLAY"):
        return True
    try:
        out = subprocess.run(["systemctl", "--user", "show-environment"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    for line in out.splitlines():
        key, _, val = line.partition("=")
        if key in WAYLAND_VARS and val:
            os.environ[key] = val
    return bool(os.environ.get("WAYLAND_DISPLAY"))


def pick_monitor(cfg: Config) -> str:
    if cfg.monitor != "auto":
        return cfg.monitor
    try:
        mons = json.loads(subprocess.run(["hyprctl", "monitors", "-j"], capture_output=True,
                                         text=True, timeout=3).stdout)
        landscape = [m for m in mons if m.get("transform", 0) % 2 == 0] or mons
        best = max(landscape, key=lambda m: (m["width"] * m["height"], m.get("refreshRate", 0)))
        return best["name"]
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError, ValueError, KeyError):
        return "screen"


def build_command(cfg: Config, incoming: Path) -> list[str]:
    cmd = [GSR, "-w", pick_monitor(cfg), "-c", "mp4", "-f", str(cfg.fps),
           "-r", str(max(5, cfg.clip_seconds)), "-k", cfg.codec, "-q", cfg.quality,
           "-fm", "cfr", "-cursor", "yes" if cfg.cursor else "no",
           "-replay-storage", "ram", "-restart-replay-on-save", "no",
           "-o", str(incoming), "-ipc", str(socket_path()), "-v", "no"]
    sources = []
    if cfg.desktop_audio:
        sources.append("default_output")
    if cfg.microphone:
        sources.append("default_input")
    if sources:
        cmd += ["-a", "|".join(sources), "-ac", "aac"]
    return cmd


_state: dict = {}
_state_lock = __import__("threading").Lock()


def _write_state(**st) -> None:
    with _state_lock:
        _state.update(st)
        tmp = state_path().with_suffix(".tmp")
        tmp.write_text(json.dumps({**_state, "daemon_pid": os.getpid()}))
        tmp.replace(state_path())


def run_daemon() -> int:
    stop = False
    reload = False
    child: subprocess.Popen | None = None

    def on_term(*_):
        nonlocal stop
        stop = True
        if child and child.poll() is None:
            child.send_signal(signal.SIGINT)  # im Replay-Modus: beenden ohne Speichern

    def on_hup(*_):
        nonlocal reload
        reload = True
        if child and child.poll() is None:
            child.send_signal(signal.SIGINT)

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    signal.signal(signal.SIGHUP, on_hup)

    if not shutil.which(GSR):
        _write_state(recording=False, error="gpu-screen-recorder ist nicht installiert")
        print("klipp: gpu-screen-recorder fehlt", file=sys.stderr)
        return 1

    from .hotkey import HotkeyManager
    hotkeys = HotkeyManager(lambda mode, err: _write_state(hotkey_mode=mode, hotkey_error=err))
    hotkeys.start()

    backoff = 2.0
    while not stop:
        if not _adopt_session_env():
            _write_state(recording=False, error="Warte auf Wayland-Sitzung …")
            time.sleep(2)
            continue
        cfg = Config.load()
        incoming = cfg.library_path / ".incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        socket_path().unlink(missing_ok=True)
        cmd = build_command(cfg, incoming)
        print("klipp: starte", " ".join(cmd), flush=True)
        started = time.time()
        child = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        _write_state(recording=True, since=started, seconds=cfg.clip_seconds,
                     monitor=cmd[2], fps=cfg.fps, error=None)
        _, err = child.communicate()
        code = child.returncode
        child = None
        if stop:
            break
        if reload:
            reload = False
            backoff = 2.0
            continue
        msg = (err or "").strip().splitlines()[-1:] or [f"Exit-Code {code}"]
        print(f"klipp: Recorder beendet ({code}): {msg[0]}", file=sys.stderr, flush=True)
        _write_state(recording=False, error=msg[0])
        backoff = 2.0 if time.time() - started > 60 else min(backoff * 2, 30)
        deadline = time.time() + backoff
        while time.time() < deadline and not stop and not reload:
            time.sleep(0.25)
        reload = False

    hotkeys.shutdown()
    _write_state(recording=False, error=None, hotkey_mode=None)
    socket_path().unlink(missing_ok=True)
    return 0
