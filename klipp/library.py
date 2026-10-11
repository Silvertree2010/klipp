"""Clip-Bibliothek: Dateien, Metadaten, Thumbnails, Spielerkennung, Export."""
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .config import DATA_DIR, INDEX_FILE, THUMB_DIR, Config

VIDEO_EXT = {".mp4", ".mkv", ".mov", ".webm"}

# Fenster, die kein Spiel sind – Clips davon landen unter "Desktop"
NON_GAMES = {
    "kitty", "alacritty", "foot", "wezterm", "org.wezfurlong.wezterm", "firefox",
    "vivaldi-stable", "chromium", "google-chrome", "brave-browser", "discord",
    "vesktop", "webcord", "spotify", "code", "code-oss", "obsidian",
    "org.gnome.nautilus", "thunar", "dolphin", "steam", "dev.klipp.klipp",
    "", None,
}


@dataclass
class Clip:
    path: Path
    game: str
    created: float
    favorite: bool = False
    duration: float | None = None
    folders: list[str] = field(default_factory=list)

    @property
    def title(self) -> str:
        return self.path.stem


# ---------------------------------------------------------------- Index

@contextmanager
def _locked_index():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    lock = open(DATA_DIR / ".index.lock", "w")
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        try:
            index = json.loads(INDEX_FILE.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            index = {}
        before = json.dumps(index, sort_keys=True)
        yield index
        if json.dumps(index, sort_keys=True) != before:
            tmp = INDEX_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(index, indent=1, ensure_ascii=False))
            tmp.replace(INDEX_FILE)
    finally:
        fcntl.flock(lock, fcntl.LOCK_UN)
        lock.close()


def read_index() -> dict:
    try:
        return json.loads(INDEX_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def update_entry(path: Path, **fields) -> None:
    with _locked_index() as index:
        index.setdefault(str(path), {}).update(fields)


def move_entry(old: Path, new: Path) -> None:
    with _locked_index() as index:
        index[str(new)] = index.pop(str(old), {})


def drop_entry(path: Path) -> None:
    with _locked_index() as index:
        index.pop(str(path), None)


# ---------------------------------------------------------------- Eigene Ordner
# Virtuelle Ordner wie Alben: die Datei bleibt im Spielordner, ein Clip kann in
# mehreren Ordnern liegen. Die Liste steht unter FOLDERS_KEY im Index.

FOLDERS_KEY = "__folders__"


def list_folders() -> list[str]:
    return list(read_index().get(FOLDERS_KEY) or [])


def create_folder(name: str) -> str:
    name = name.strip()[:60]
    if not name:
        raise ValueError("Name fehlt")
    with _locked_index() as index:
        folders = index.setdefault(FOLDERS_KEY, [])
        if any(f.lower() == name.lower() for f in folders):
            raise ValueError(f"„{name}“ gibt es schon")
        folders.append(name)
    return name


def rename_folder(old: str, new: str) -> str:
    new = new.strip()[:60]
    if not new:
        raise ValueError("Name fehlt")
    with _locked_index() as index:
        folders = index.setdefault(FOLDERS_KEY, [])
        if any(f.lower() == new.lower() and f != old for f in folders):
            raise ValueError(f"„{new}“ gibt es schon")
        index[FOLDERS_KEY] = [new if f == old else f for f in folders]
        for key, meta in index.items():
            if key != FOLDERS_KEY and old in (meta.get("folders") or []):
                meta["folders"] = [new if f == old else f for f in meta["folders"]]
    return new


def delete_folder(name: str) -> None:
    """Ordner entfernen – die Clips bleiben erhalten."""
    with _locked_index() as index:
        index[FOLDERS_KEY] = [f for f in index.get(FOLDERS_KEY, []) if f != name]
        for key, meta in index.items():
            if key != FOLDERS_KEY and name in (meta.get("folders") or []):
                meta["folders"] = [f for f in meta["folders"] if f != name]


def set_in_folder(clip: Clip, folder: str, inside: bool) -> None:
    folders = [f for f in clip.folders if f != folder] + ([folder] if inside else [])
    update_entry(clip.path, folders=folders, game=clip.game, created=clip.created)
    clip.folders = folders


def scan(cfg: Config) -> list[Clip]:
    root = cfg.library_path
    index = read_index()
    clips = []
    if root.is_dir():
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                p = Path(dirpath) / name
                if p.suffix.lower() not in VIDEO_EXT or name.startswith("."):
                    continue
                meta = index.get(str(p), {})
                game = meta.get("game") or (p.parent.name if p.parent != root else "Desktop")
                try:
                    created = meta.get("created") or p.stat().st_mtime
                except OSError:
                    continue
                clips.append(Clip(p, game, created, bool(meta.get("favorite")),
                                  meta.get("duration"), list(meta.get("folders") or [])))
    clips.sort(key=lambda c: c.created, reverse=True)
    return clips


# ---------------------------------------------------------------- Medien-Helfer

def probe_duration(path: Path) -> float | None:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, text=True, timeout=15).stdout.strip()
        return round(float(out), 2)
    except (ValueError, subprocess.SubprocessError, OSError):
        return None


def thumbnail_path(path: Path) -> Path:
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        mtime = 0
    key = hashlib.sha1(f"{path}:{mtime}".encode()).hexdigest()
    return THUMB_DIR / f"{key}.jpg"


def ensure_thumbnail(path: Path, duration: float | None = None) -> Path | None:
    thumb = thumbnail_path(path)
    if thumb.exists():
        return thumb
    THUMB_DIR.mkdir(parents=True, exist_ok=True)
    at = min(2.0, (duration or 4) / 2)
    tmp = thumb.with_suffix(".tmp.jpg")
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-ss", f"{at:.2f}", "-i", str(path),
         "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "4", str(tmp)],
        capture_output=True, timeout=30)
    if r.returncode != 0 or not tmp.exists():
        return None
    tmp.replace(thumb)
    return thumb


def has_encoder(name: str) -> bool:
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                             capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return f" {name} " in out


def _encoder_args(bitrate_k: int | None = None) -> list[str]:
    """H.264 per NVENC wenn möglich, sonst x264."""
    if has_encoder("h264_nvenc"):
        if bitrate_k:
            return ["-c:v", "h264_nvenc", "-preset", "p6", "-rc", "cbr",
                    "-b:v", f"{bitrate_k}k", "-maxrate", f"{bitrate_k}k",
                    "-bufsize", f"{bitrate_k}k"]
        return ["-c:v", "h264_nvenc", "-preset", "p6", "-rc", "vbr", "-cq", "19", "-b:v", "0"]
    if bitrate_k:
        return ["-c:v", "libx264", "-preset", "veryfast", "-b:v", f"{bitrate_k}k",
                "-maxrate", f"{bitrate_k}k", "-bufsize", f"{bitrate_k * 2}k"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"]


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(2, 1000):
        cand = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not cand.exists():
            return cand
    return path.with_name(f"{path.stem} {int(time.time())}{path.suffix}")


def safe_name(text: str) -> str:
    text = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "", text).strip(" .")
    return text[:80] or "Clip"


# ---------------------------------------------------------------- Spielerkennung

def _steam_roots() -> list[Path]:
    home = Path.home()
    cands = [home / ".local/share/Steam", home / ".steam/steam",
             home / ".var/app/com.valvesoftware.Steam/.local/share/Steam"]
    roots = []
    for c in cands:
        vdf = c / "steamapps/libraryfolders.vdf"
        if vdf.exists():
            roots.append(c)
            try:
                for m in re.finditer(r'"path"\s+"([^"]+)"', vdf.read_text(errors="ignore")):
                    roots.append(Path(m.group(1)))
            except OSError:
                pass
    seen, out = set(), []
    for r in roots:
        if r.resolve() not in seen:
            seen.add(r.resolve())
            out.append(r)
    return out


def steam_game_name(appid: str) -> str | None:
    for root in _steam_roots():
        acf = root / "steamapps" / f"appmanifest_{appid}.acf"
        try:
            m = re.search(r'"name"\s+"([^"]+)"', acf.read_text(errors="ignore"))
        except OSError:
            continue
        if m:
            return m.group(1)
    return None


def _proc_steam_appid(pid: int) -> str | None:
    """Steam setzt SteamAppId in der Umgebung jedes gestarteten Spiels."""
    try:
        env = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return None
    for kv in env:
        if kv.startswith((b"SteamAppId=", b"SteamGameId=")):
            val = kv.split(b"=", 1)[1].decode(errors="ignore")
            if val.isdigit() and val != "0":
                return val
    return None


def active_window() -> dict:
    try:
        out = subprocess.run(["hyprctl", "activewindow", "-j"],
                             capture_output=True, text=True, timeout=3).stdout
        return json.loads(out) or {}
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return {}


def detect_game(win: dict | None = None) -> str:
    win = active_window() if win is None else win
    cls = (win.get("class") or "").strip()
    title = (win.get("title") or "").strip()
    m = re.fullmatch(r"steam_app_(\d+)", cls)
    appid = m.group(1) if m else (_proc_steam_appid(win["pid"]) if win.get("pid") else None)
    if appid:
        name = steam_game_name(appid)
        if name:
            return safe_name(name)
    if cls.lower() in NON_GAMES:
        return "Desktop"
    if win.get("fullscreen") and title and len(title) <= 48:
        return safe_name(title)
    pretty = cls.split(".")[-1].replace("_", " ").replace("-", " ").strip()
    return safe_name(pretty.title() if pretty.islower() else pretty) if pretty else "Desktop"


# ---------------------------------------------------------------- Operationen

def ingest(raw: Path, game: str, cfg: Config) -> Path:
    """Frisch gespeicherten Replay in den Spielordner verschieben und registrieren."""
    folder = cfg.library_path / safe_name(game)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H-%M-%S")
    dest = _unique(folder / f"{safe_name(game)} {stamp}{raw.suffix}")
    shutil.move(str(raw), dest)
    duration = probe_duration(dest)
    update_entry(dest, game=game, created=time.time(), duration=duration)
    ensure_thumbnail(dest, duration)
    return dest


def set_favorite(clip: Clip, value: bool) -> None:
    update_entry(clip.path, favorite=value, game=clip.game, created=clip.created)
    clip.favorite = value


def rename(clip: Clip, new_title: str) -> Path:
    dest = _unique(clip.path.with_name(safe_name(new_title) + clip.path.suffix))
    clip.path.rename(dest)
    move_entry(clip.path, dest)
    update_entry(dest, game=clip.game, created=clip.created)
    clip.path = dest
    return dest


def trash(clip: Clip) -> None:
    subprocess.run(["gio", "trash", str(clip.path)], check=True, timeout=10)
    drop_entry(clip.path)


def trim(clip: Clip, start: float, end: float) -> Path:
    """Bildgenau zuschneiden (neu kodiert), Original bleibt erhalten."""
    dest = _unique(clip.path.with_name(f"{clip.path.stem} (Schnitt){clip.path.suffix}"))
    cmd = ["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
           "-i", str(clip.path), *_encoder_args(), "-c:a", "aac", "-b:a", "192k",
           "-movflags", "+faststart", str(dest)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        dest.unlink(missing_ok=True)
        raise RuntimeError(r.stderr.strip()[-400:] or "ffmpeg fehlgeschlagen")
    duration = probe_duration(dest)
    update_entry(dest, game=clip.game, created=time.time(), duration=duration,
                 folders=list(clip.folders))
    ensure_thumbnail(dest, duration)
    return dest


def export_discord(clip: Clip, cfg: Config, start: float | None = None,
                   end: float | None = None) -> Path:
    """Auf das Discord-Upload-Limit komprimieren."""
    duration = clip.duration or probe_duration(clip.path) or 30
    start = start or 0.0
    end = end if end is not None else duration
    length = max(0.5, end - start)
    whole = start <= 0.05 and end >= duration - 0.05
    if whole and clip.path.stat().st_size <= cfg.discord_limit_mb * 1024 * 1024 * 0.97:
        return clip.path  # passt schon – Original verschicken
    out_dir = cfg.library_path / ".discord"
    out_dir.mkdir(parents=True, exist_ok=True)
    dest = out_dir / f"{safe_name(clip.title)}.mp4"
    limit_bits = cfg.discord_limit_mb * 1024 * 1024 * 8
    audio_k = 128
    factor = 0.90
    for _ in range(3):
        video_k = min(int(limit_bits * factor / length / 1000) - audio_k, 20000)
        if video_k < 150:
            raise RuntimeError("Clip ist zu lang für das Discord-Limit – zuerst zuschneiden.")
        height = 720 if video_k < 3500 else 1080 if video_k < 9000 else None
        vf = [f"scale=-2:'min({height},ih)'"] if height else []
        fps = ["-r", "60"] if video_k >= 2000 else ["-r", "30"]
        cmd = ["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
               "-i", str(clip.path), *(["-vf", ",".join(vf)] if vf else []), *fps,
               *_encoder_args(video_k), "-c:a", "aac", "-b:a", f"{audio_k}k",
               "-movflags", "+faststart", str(dest)]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(r.stderr.strip()[-400:] or "ffmpeg fehlgeschlagen")
        if dest.stat().st_size <= cfg.discord_limit_mb * 1024 * 1024:
            return dest
        factor *= 0.85
    return dest


def copy_files_to_clipboard(paths: list[Path]) -> bool:
    """Datei(en) als Datei in die Zwischenablage – Strg+V in Discord hängt sie an."""
    uris = "\r\n".join(p.resolve().as_uri() for p in paths) + "\r\n"
    try:
        subprocess.Popen(["wl-copy", "--type", "text/uri-list"], stdin=subprocess.PIPE,
                         start_new_session=True).communicate(uris.encode(), timeout=5)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def open_folder(path: Path) -> None:
    target = path if path.is_dir() else path.parent
    subprocess.Popen(["xdg-open", str(target)], start_new_session=True,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
