"""Klipp – Clip-Bibliothek (GTK4 + libadwaita)."""
import math
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from . import __version__, hotkey, library, recorder  # noqa: E402
from .config import APP_ID, INDEX_FILE, Config  # noqa: E402

CSS = """
.clip-card { padding: 6px; border-radius: 16px; }
.clip-thumb { border-radius: 12px; background: alpha(currentColor, 0.08); }
.badge { background: rgba(0,0,0,0.70); color: white; border-radius: 7px;
         padding: 2px 7px; margin: 8px; font-size: 0.82em; font-weight: 700; }
.fav-badge { color: #f6d32d; background: rgba(0,0,0,0.55); border-radius: 99px;
             padding: 4px; margin: 8px; }
.clip-title { font-weight: 600; }
.player-frame { background: black; border-radius: 14px; }
.rec-dot { color: #e01b24; }
.idle-dot { color: alpha(currentColor, 0.35); }
.status-box { padding: 12px; }
.save-button { padding: 8px 16px; }
"""


def fmt_time(sec: float) -> str:
    sec = max(0.0, sec)
    m, s = divmod(sec, 60)
    return f"{int(m)}:{s:04.1f}" if sec < 600 else f"{int(m)}:{int(s):02d}"


def fmt_duration(sec: float | None) -> str:
    if sec is None:
        return "…"
    sec = round(sec)
    return f"{sec // 60}:{sec % 60:02d}"


def fmt_when(ts: float) -> str:
    d = datetime.fromtimestamp(ts)
    days = (datetime.now().date() - d.date()).days
    if days == 0:
        return f"Heute, {d:%H:%M}"
    if days == 1:
        return f"Gestern, {d:%H:%M}"
    if days < 7:
        return ["Mo", "Di", "Mi", "Do", "Fr", "Sa", "So"][d.weekday()] + f", {d:%H:%M}"
    return f"{d:%d.%m.%Y}"


def run_bg(fn, done=None, error=None):
    """fn im Thread ausführen, Ergebnis im GTK-Thread zurückgeben."""
    def worker():
        try:
            res = fn()
        except Exception as e:  # noqa: BLE001
            if error:
                GLib.idle_add(error, e)
            return
        if done:
            GLib.idle_add(done, res)
    threading.Thread(target=worker, daemon=True).start()


# ====================================================================== Trim-Leiste

class TrimBar(Gtk.DrawingArea):
    PAD = 12
    HANDLE_W = 10

    def __init__(self, on_seek, on_range):
        super().__init__()
        self.set_content_height(44)
        self.set_hexpand(True)
        self.duration = 0.0
        self.start = 0.0
        self.end = 0.0
        self.pos = 0.0
        self.on_seek = on_seek
        self.on_range = on_range
        self._drag = None
        self.set_draw_func(self._draw)
        drag = Gtk.GestureDrag()
        drag.connect("drag-begin", self._begin)
        drag.connect("drag-update", self._update)
        drag.connect("drag-end", lambda *_: setattr(self, "_drag", None))
        self.add_controller(drag)
        self.set_cursor(Gdk.Cursor.new_from_name("pointer"))

    def set_duration(self, d: float):
        self.duration = d
        self.start, self.end = 0.0, d
        self.queue_draw()

    def set_pos(self, p: float):
        self.pos = p
        self.queue_draw()

    @property
    def is_trimmed(self) -> bool:
        return self.duration > 0 and (self.start > 0.05 or self.end < self.duration - 0.05)

    def _x(self, t: float) -> float:
        w = self.get_width() - 2 * self.PAD
        return self.PAD + (t / self.duration * w if self.duration else 0)

    def _t(self, x: float) -> float:
        w = max(1, self.get_width() - 2 * self.PAD)
        return min(self.duration, max(0.0, (x - self.PAD) / w * self.duration))

    def _begin(self, gesture, x, y):
        if not self.duration:
            return
        self._origin = x
        ds, de = abs(x - self._x(self.start)), abs(x - self._x(self.end))
        if min(ds, de) <= 14:
            self._drag = "start" if ds <= de else "end"
        else:
            self._drag = "seek"
            self._apply(x)

    def _update(self, gesture, dx, dy):
        if self._drag:
            self._apply(self._origin + dx)

    def _apply(self, x):
        t = self._t(x)
        if self._drag == "start":
            self.start = min(t, self.end - 0.5)
            self.on_range(self.start, self.end)
            self.on_seek(self.start)
        elif self._drag == "end":
            self.end = max(t, self.start + 0.5)
            self.on_range(self.start, self.end)
            self.on_seek(max(self.start, self.end - 1.5))
        else:
            self.on_seek(t)
        self.queue_draw()

    def _draw(self, area, cr, w, h):
        fg = self.get_color()
        ok, acc = self.get_style_context().lookup_color("accent_bg_color")
        if not ok:
            acc = Adw.StyleManager.get_default().get_accent_color_rgba()
        pad, top, bh = self.PAD, 8, h - 16
        # Spur
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.12)
        self._rounded(cr, pad, top, w - 2 * pad, bh, 8)
        cr.fill()
        if not self.duration:
            return
        xs, xe = self._x(self.start), self._x(self.end)
        # Auswahl
        cr.set_source_rgba(acc.red, acc.green, acc.blue, 0.30)
        cr.rectangle(xs, top, xe - xs, bh)
        cr.fill()
        cr.set_source_rgba(acc.red, acc.green, acc.blue, 1)
        cr.set_line_width(2)
        cr.rectangle(xs, top + 1, xe - xs, bh - 2)
        cr.stroke()
        # Griffe
        for x in (xs, xe):
            self._rounded(cr, x - self.HANDLE_W / 2, top - 4, self.HANDLE_W, bh + 8, 4)
            cr.fill()
            cr.set_source_rgba(1, 1, 1, 0.9)
            cr.rectangle(x - 1, top + bh / 2 - 6, 2, 12)
            cr.fill()
            cr.set_source_rgba(acc.red, acc.green, acc.blue, 1)
        # Abspielposition
        xp = self._x(self.pos)
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.95)
        cr.rectangle(xp - 1, 2, 2, h - 4)
        cr.fill()

    @staticmethod
    def _rounded(cr, x, y, w, h, r):
        cr.new_sub_path()
        cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
        cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
        cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
        cr.arc(x + r, y + r, r, math.pi, 3 * math.pi / 2)
        cr.close_path()


# ====================================================================== Clip-Karte

class ClipCard(Gtk.Button):
    def __init__(self, clip: library.Clip, on_open):
        super().__init__()
        self.clip = clip
        self.add_css_class("flat")
        self.add_css_class("clip-card")
        self.connect("clicked", lambda *_: on_open(clip))

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        overlay = Gtk.Overlay()
        self.picture = Gtk.Picture(content_fit=Gtk.ContentFit.COVER, can_shrink=True)
        self.picture.set_size_request(288, 162)
        self.picture.add_css_class("clip-thumb")
        self.picture.set_overflow(Gtk.Overflow.HIDDEN)
        overlay.set_child(self.picture)

        self.dur = Gtk.Label(label=fmt_duration(clip.duration), halign=Gtk.Align.END,
                             valign=Gtk.Align.END)
        self.dur.add_css_class("badge")
        overlay.add_overlay(self.dur)
        if clip.favorite:
            star = Gtk.Image(icon_name="starred-symbolic", halign=Gtk.Align.END,
                             valign=Gtk.Align.START)
            star.add_css_class("fav-badge")
            overlay.add_overlay(star)
        box.append(overlay)

        title = Gtk.Label(label=clip.title, xalign=0, ellipsize=Pango.EllipsizeMode.END,
                          max_width_chars=30)
        title.add_css_class("clip-title")
        sub = Gtk.Label(label=f"{clip.game} · {fmt_when(clip.created)}", xalign=0,
                        ellipsize=Pango.EllipsizeMode.END, max_width_chars=34)
        sub.add_css_class("dim-label")
        sub.add_css_class("caption")
        box.append(title)
        box.append(sub)
        self.set_child(box)

        drag = Gtk.DragSource(actions=Gdk.DragAction.COPY)
        drag.connect("prepare", lambda *_: Gdk.ContentProvider.new_for_value(
            Gdk.FileList.new_from_list([Gio.File.new_for_path(str(self.clip.path))])))
        self.add_controller(drag)

    def set_thumbnail(self, path: Path | None, duration: float | None):
        if path:
            try:
                self.picture.set_paintable(Gdk.Texture.new_from_filename(str(path)))
            except GLib.Error:
                pass
        self.dur.set_label(fmt_duration(duration))


# ====================================================================== Player

class PlayerPage(Adw.NavigationPage):
    def __init__(self, win: "KlippWindow", clip: library.Clip):
        super().__init__(title=clip.title)
        self.win = win
        self.clip = clip
        self._busy = False

        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        view.add_top_bar(header)

        self.fav = Gtk.ToggleButton(icon_name="starred-symbolic" if clip.favorite else
                                    "non-starred-symbolic", active=clip.favorite,
                                    tooltip_text="Favorit")
        self.fav.connect("toggled", self._toggle_fav)

        menu = Gio.Menu()
        menu.append("Umbenennen …", "clip.rename")
        menu.append("Im Ordner zeigen", "clip.folder")
        menu.append("Datei kopieren", "clip.copy")
        section = Gio.Menu()
        section.append("In den Papierkorb", "clip.delete")
        menu.append_section(None, section)
        more = Gtk.MenuButton(icon_name="view-more-symbolic", menu_model=menu,
                              tooltip_text="Mehr")

        self.discord_btn = Gtk.Button(tooltip_text=f"Auf {Config.load().discord_limit_mb} MB "
                                                   "komprimieren und kopieren")
        content = Adw.ButtonContent(icon_name="send-to-symbolic", label="Für Discord")
        self.discord_btn.set_child(content)
        self.discord_btn.add_css_class("suggested-action")
        self.discord_btn.connect("clicked", self._discord)

        header.pack_end(more)
        header.pack_end(self.fav)
        header.pack_end(self.discord_btn)

        actions = Gio.SimpleActionGroup()
        for name, cb in (("rename", self._rename), ("folder", self._folder),
                         ("copy", self._copy), ("delete", self._delete)):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", cb)
            actions.add_action(a)
        self.insert_action_group("clip", actions)

        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=6,
                       margin_bottom=18, margin_start=18, margin_end=18)
        self.media = Gtk.MediaFile.new_for_filename(str(clip.path))
        self.video = Gtk.Video(media_stream=self.media, autoplay=True, vexpand=True,
                               graphics_offload=Gtk.GraphicsOffloadEnabled.ENABLED)
        self.video.add_css_class("player-frame")
        self.video.set_overflow(Gtk.Overflow.HIDDEN)
        body.append(self.video)

        self.trim = TrimBar(self._seek, self._range_changed)
        body.append(self.trim)

        row = Gtk.Box(spacing=8)
        icon = Gtk.Image(icon_name="edit-cut-symbolic")
        icon.add_css_class("dim-label")
        row.append(icon)
        self.range_label = Gtk.Label(xalign=0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        self.range_label.add_css_class("numeric")
        row.append(self.range_label)
        for label, cb, tip in (("[", self._mark_start, "Anfang an Abspielposition"),
                               ("]", self._mark_end, "Ende an Abspielposition")):
            b = Gtk.Button(label=label, tooltip_text=tip)
            b.add_css_class("flat")
            b.connect("clicked", cb)
            row.append(b)
        self.reset_btn = Gtk.Button(label="Zurücksetzen", sensitive=False)
        self.reset_btn.add_css_class("flat")
        self.reset_btn.connect("clicked", self._reset)
        row.append(self.reset_btn)
        self.save_trim = Gtk.Button(label="Schnitt speichern", sensitive=False)
        self.save_trim.add_css_class("pill")
        self.save_trim.connect("clicked", self._do_trim)
        row.append(self.save_trim)
        body.append(row)

        view.set_content(body)
        self.set_child(view)

        self.media.connect("notify::duration", self._on_duration)
        self.media.connect("notify::timestamp", self._on_timestamp)
        self.media.connect("notify::ended", self._on_ended)
        self.media.connect("notify::error", self._on_error)
        self.connect("hidden", self._on_hidden)
        self._update_label()

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._key)
        self.add_controller(keys)

    # -- Wiedergabe
    def _on_duration(self, *_):
        d = self.media.get_duration() / 1e6
        if d > 0:
            self.trim.set_duration(d)
            self._update_label()

    def _on_timestamp(self, *_):
        t = self.media.get_timestamp() / 1e6
        self.trim.set_pos(t)
        if self.trim.is_trimmed and t >= self.trim.end and self.media.get_playing():
            self._seek(self.trim.start)

    def _on_ended(self, *_):
        if self.media.get_ended():
            self._seek(self.trim.start if self.trim.is_trimmed else 0)
            self.media.play()

    def _on_error(self, *_):
        err = self.media.get_error()
        if err:
            self.win.toast(f"Wiedergabe fehlgeschlagen: {err.message}")

    def _on_hidden(self, *_):
        self.media.pause()
        self.video.set_media_stream(None)

    def _seek(self, t: float):
        self.media.seek(int(t * 1e6))
        self.trim.set_pos(t)

    def _key(self, ctrl, keyval, code, state):
        if keyval == Gdk.KEY_space:
            self.media.set_playing(not self.media.get_playing())
            return True
        if keyval in (Gdk.KEY_Left, Gdk.KEY_Right):
            step = -2 if keyval == Gdk.KEY_Left else 2
            self._seek(min(self.trim.duration, max(0, self.media.get_timestamp() / 1e6 + step)))
            return True
        return False

    # -- Schnitt
    def _range_changed(self, s, e):
        self._update_label()

    def _update_label(self):
        t = self.trim
        if not t.duration:
            self.range_label.set_label("Lädt …")
            return
        length = t.end - t.start
        if t.is_trimmed:
            self.range_label.set_label(f"{fmt_time(t.start)} – {fmt_time(t.end)}  ·  {length:.1f} s")
        else:
            self.range_label.set_label(f"Ganzer Clip  ·  {length:.1f} s  —  Griffe ziehen zum Zuschneiden")
        self.reset_btn.set_sensitive(t.is_trimmed)
        self.save_trim.set_sensitive(t.is_trimmed and not self._busy)
        if t.is_trimmed:
            self.save_trim.add_css_class("suggested-action")
        else:
            self.save_trim.remove_css_class("suggested-action")

    def _mark_start(self, *_):
        t = self.media.get_timestamp() / 1e6
        self.trim.start = min(t, self.trim.end - 0.5)
        self.trim.queue_draw()
        self._update_label()

    def _mark_end(self, *_):
        t = self.media.get_timestamp() / 1e6
        self.trim.end = max(t, self.trim.start + 0.5)
        self.trim.queue_draw()
        self._update_label()

    def _reset(self, *_):
        self.trim.start, self.trim.end = 0.0, self.trim.duration
        self.trim.queue_draw()
        self._update_label()

    def _set_busy(self, busy: bool):
        self._busy = busy
        self.discord_btn.set_sensitive(not busy)
        self._update_label()

    def _do_trim(self, *_):
        s, e = self.trim.start, self.trim.end
        self._set_busy(True)
        self.win.toast("Schnitt wird gespeichert …", timeout=2)

        def done(path):
            self._set_busy(False)
            self.win.reload()
            self.win.toast(f"Gespeichert: {path.name}", action=("Öffnen", lambda: self.win.open_path(path)))

        def fail(e):
            self._set_busy(False)
            self.win.toast(f"Schnitt fehlgeschlagen: {e}")
        run_bg(lambda: library.trim(self.clip, s, e), done, fail)

    # -- Aktionen
    def _discord(self, *_):
        cfg = Config.load()
        s = self.trim.start if self.trim.is_trimmed else None
        e = self.trim.end if self.trim.is_trimmed else None
        self._set_busy(True)
        spinner = Adw.Spinner()
        self.discord_btn.set_child(spinner)
        self.win.toast(f"Komprimiere auf {cfg.discord_limit_mb} MB …", timeout=2)

        def restore():
            self.discord_btn.set_child(Adw.ButtonContent(icon_name="send-to-symbolic",
                                                         label="Für Discord"))
            self._set_busy(False)

        def done(path):
            restore()
            library.copy_files_to_clipboard([path])
            mb = path.stat().st_size / 1024 / 1024
            self.win.toast(f"Kopiert ({mb:.1f} MB) – in Discord mit Strg+V einfügen",
                           action=("Ordner", lambda: library.open_folder(path)))

        def fail(err):
            restore()
            self.win.toast(str(err))
        run_bg(lambda: library.export_discord(self.clip, cfg, s, e), done, fail)

    def _toggle_fav(self, btn):
        library.set_favorite(self.clip, btn.get_active())
        btn.set_icon_name("starred-symbolic" if btn.get_active() else "non-starred-symbolic")
        self.win.reload()

    def _copy(self, *_):
        if library.copy_files_to_clipboard([self.clip.path]):
            self.win.toast("Datei kopiert – in Discord oder im Dateimanager einfügen")

    def _folder(self, *_):
        library.open_folder(self.clip.path)

    def _rename(self, *_):
        entry = Gtk.Entry(text=self.clip.title, activates_default=True)
        dlg = Adw.AlertDialog(heading="Clip umbenennen", extra_child=entry)
        dlg.add_response("cancel", "Abbrechen")
        dlg.add_response("ok", "Umbenennen")
        dlg.set_response_appearance("ok", Adw.ResponseAppearance.SUGGESTED)
        dlg.set_default_response("ok")

        def resp(d, r):
            name = entry.get_text().strip()
            if r == "ok" and name and name != self.clip.title:
                pos = self.media.get_timestamp()
                playing = self.media.get_playing()
                self.media.pause()
                try:
                    library.rename(self.clip, name)
                except OSError as e:
                    self.win.toast(f"Umbenennen fehlgeschlagen: {e}")
                    return
                self.media = Gtk.MediaFile.new_for_filename(str(self.clip.path))
                for sig, cb in (("notify::duration", self._on_duration),
                                ("notify::timestamp", self._on_timestamp),
                                ("notify::ended", self._on_ended),
                                ("notify::error", self._on_error)):
                    self.media.connect(sig, cb)
                self.video.set_media_stream(self.media)
                self.media.seek(pos)
                if playing:
                    self.media.play()
                self.set_title(self.clip.title)
                self.win.reload()
        dlg.connect("response", resp)
        dlg.present(self.win)

    def _delete(self, *_):
        dlg = Adw.AlertDialog(heading="Clip löschen?",
                              body=f"„{self.clip.title}“ wird in den Papierkorb verschoben.")
        dlg.add_response("cancel", "Abbrechen")
        dlg.add_response("delete", "Löschen")
        dlg.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

        def resp(d, r):
            if r != "delete":
                return
            self.media.pause()
            try:
                library.trash(self.clip)
            except Exception as e:  # noqa: BLE001
                self.win.toast(f"Löschen fehlgeschlagen: {e}")
                return
            self.win.nav.pop()
            self.win.reload()
            self.win.toast("In den Papierkorb verschoben")
        dlg.connect("response", resp)
        dlg.present(self.win)


# ====================================================================== Einstellungen

class Preferences(Adw.PreferencesDialog):
    FPS = [30, 60, 120, 144, 165, 200, 240]
    QUALITY = [("medium", "Mittel"), ("high", "Hoch"), ("very_high", "Sehr hoch"),
               ("ultra", "Ultra")]
    CODEC = [("h264", "H.264 – läuft überall, auch in Discord"),
             ("hevc", "HEVC – kleinere Dateien"), ("av1", "AV1 – am kleinsten")]
    LIMITS = [(10, "10 MB (ohne Nitro)"), (50, "50 MB (Nitro Basic)"), (500, "500 MB (Nitro)")]

    def __init__(self, win: "KlippWindow"):
        super().__init__(title="Einstellungen", search_enabled=False)
        self.win = win
        self.cfg = Config.load()
        self.before = self.cfg.recorder_signature()

        page = Adw.PreferencesPage(title="Allgemein", icon_name="emblem-system-symbolic")
        self.add(page)

        keys = Adw.PreferencesGroup(title="Hotkey")
        page.add(keys)
        self.hotkey_row = Adw.ActionRow(title="Clip speichern", activatable=True)
        self.hotkey_label = Adw.ShortcutLabel(disabled_text="Kein Hotkey", valign=Gtk.Align.CENTER)
        self.hotkey_row.add_suffix(self.hotkey_label)
        self.hotkey_row.add_suffix(Gtk.Image(icon_name="document-edit-symbolic"))
        self.hotkey_row.connect("activated", self._capture_hotkey)
        keys.add(self.hotkey_row)
        self._show_hotkey()

        rec = Adw.PreferencesGroup(title="Instant Replay",
                                   description="Klipp nimmt im Hintergrund auf und speichert "
                                               "auf Tastendruck die letzten Sekunden.")
        page.add(rec)
        self.seconds = Adw.SpinRow.new_with_range(5, 600, 5)
        self.seconds.set_title("Clip-Länge")
        self.seconds.set_subtitle("Sekunden, die beim Speichern zurückgeholt werden")
        self.seconds.set_value(self.cfg.clip_seconds)
        rec.add(self.seconds)

        self.fps = self._combo("Bildrate", [f"{f} FPS" for f in self.FPS],
                               self.FPS.index(self.cfg.fps) if self.cfg.fps in self.FPS else 1)
        rec.add(self.fps)
        q_keys = [k for k, _ in self.QUALITY]
        self.quality = self._combo("Qualität", [v for _, v in self.QUALITY],
                                   q_keys.index(self.cfg.quality) if self.cfg.quality in q_keys else 2)
        rec.add(self.quality)
        c_keys = [k for k, _ in self.CODEC]
        self.codec = self._combo("Codec", [v.split(" –")[0] for _, v in self.CODEC],
                                 c_keys.index(self.cfg.codec) if self.cfg.codec in c_keys else 0)
        self.codec.set_subtitle("H.264 empfohlen – läuft überall, auch in Discord")
        rec.add(self.codec)

        self.monitors = ["auto"] + self._list_monitors()
        labels = ["Automatisch (Hauptmonitor)"] + self.monitors[1:]
        self.monitor = self._combo("Bildschirm", labels,
                                   self.monitors.index(self.cfg.monitor)
                                   if self.cfg.monitor in self.monitors else 0)
        rec.add(self.monitor)

        audio = Adw.PreferencesGroup(title="Ton")
        page.add(audio)
        self.desktop_audio = Adw.SwitchRow(title="Spiel- und Desktop-Ton",
                                           active=self.cfg.desktop_audio)
        self.microphone = Adw.SwitchRow(title="Mikrofon", subtitle="Deine Stimme mit aufnehmen",
                                        active=self.cfg.microphone)
        self.cursor = Adw.SwitchRow(title="Mauszeiger aufnehmen", active=self.cfg.cursor)
        audio.add(self.desktop_audio)
        audio.add(self.microphone)
        audio.add(self.cursor)

        lib = Adw.PreferencesGroup(title="Bibliothek")
        page.add(lib)
        self.folder_row = Adw.ActionRow(title="Speicherort", subtitle=self.cfg.library)
        btn = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER,
                         tooltip_text="Ordner wählen")
        btn.add_css_class("flat")
        btn.connect("clicked", self._pick_folder)
        self.folder_row.add_suffix(btn)
        self.folder_row.set_activatable_widget(btn)
        lib.add(self.folder_row)
        self.limit = self._combo("Discord-Limit", [v for _, v in self.LIMITS],
                                 [k for k, _ in self.LIMITS].index(self.cfg.discord_limit_mb)
                                 if self.cfg.discord_limit_mb in [k for k, _ in self.LIMITS] else 0)
        lib.add(self.limit)

        info = Adw.PreferencesGroup(title="Benachrichtigung")
        page.add(info)
        self.notif = Adw.SwitchRow(title="Benachrichtigung beim Speichern",
                                   active=self.cfg.notifications)
        self.sound = Adw.SwitchRow(title="Auslöser-Ton", active=self.cfg.sound)
        info.add(self.notif)
        info.add(self.sound)

        self.connect("closed", self._save)

    # -- Hotkey
    GTK_MODS = {"SUPER": "<Super>", "CTRL": "<Control>", "ALT": "<Alt>", "SHIFT": "<Shift>"}
    BARE_OK = {f"F{i}" for i in range(1, 25)} | {"Print", "Pause", "Scroll_Lock", "Insert"}

    def _show_hotkey(self, note: str | None = None):
        p = hotkey.parse(self.cfg.hotkey)
        accel = "".join(self.GTK_MODS[m] for m in p[0]) + p[1] if p else ""
        self.hotkey_label.set_accelerator(accel)
        if note:
            self.hotkey_row.set_subtitle(note)
            return
        st = recorder.read_state()
        mode = st.get("hotkey_mode")
        if not p:
            sub = "Aus – nur über den Knopf in der App"
        elif not st.get("daemon_pid"):
            sub = "Aktiv, sobald Instant Replay läuft"
        elif mode == "hyprland":
            sub = "Aktiv – funktioniert überall, auch im Vollbild-Spiel"
        elif mode == "evdev":
            sub = "Aktiv – direkt über die Tastatur"
        else:
            sub = st.get("hotkey_error") or "Wird gesetzt …"
        self.hotkey_row.set_subtitle(sub + "  ·  klicken zum Ändern")

    def _capture_hotkey(self, *_):
        old = self.cfg.hotkey
        hotkey.hypr_unbind(old)  # damit die alte Kombi beim Drücken hier ankommt
        dlg = Adw.AlertDialog(heading="Neuen Hotkey drücken",
                              body="Zum Beispiel Alt + F10.\n"
                                   "Esc = abbrechen · Rücktaste = Hotkey entfernen")
        dlg.add_response("cancel", "Abbrechen")
        dlg.set_close_response("cancel")
        result = {"combo": None}

        def pressed(ctrl, keyval, keycode, state):
            if keyval == Gdk.KEY_Escape:
                dlg.close()
                return True
            if keyval == Gdk.KEY_BackSpace and not state & Gtk.accelerator_get_default_mod_mask():
                result["combo"] = ""
                dlg.close()
                return True
            ok, base, *_ = self.get_display().translate_key(keycode, 0, 0)
            name = Gdk.keyval_name(base if ok else keyval) or ""
            if name.startswith(("Shift", "Control", "Alt", "Super", "Meta", "ISO_", "Caps")):
                return True
            mods = [m for m, mask in (("SUPER", Gdk.ModifierType.SUPER_MASK),
                                      ("CTRL", Gdk.ModifierType.CONTROL_MASK),
                                      ("ALT", Gdk.ModifierType.ALT_MASK),
                                      ("SHIFT", Gdk.ModifierType.SHIFT_MASK)) if state & mask]
            if not mods and name not in self.BARE_OK:
                dlg.set_body("Bitte mit Strg, Alt oder Super kombinieren – sonst löst es beim "
                             "Tippen aus.\nEsc = abbrechen")
                return True
            combo = "+".join(mods + [name])
            clash = hotkey.hypr_conflict(combo)
            if clash:
                dlg.set_body(f"{hotkey.pretty(combo)} ist schon belegt ({clash}).\n"
                             "Bitte eine andere Kombination drücken.")
                return True
            result["combo"] = combo
            dlg.close()
            return True

        keys = Gtk.EventControllerKey(propagation_phase=Gtk.PropagationPhase.CAPTURE)
        keys.connect("key-pressed", pressed)
        dlg.add_controller(keys)

        def closed(*_):
            combo = result["combo"]
            if combo is not None and combo != old:
                self.cfg.hotkey = combo
                saved = Config.load()
                saved.hotkey = combo
                saved.save()
                self._show_hotkey("Gespeichert – wird in ein paar Sekunden aktiv")
                GLib.timeout_add_seconds(4, lambda: (self._show_hotkey(), False)[1])
                self.win.refresh_status()
            else:
                self._show_hotkey()
        dlg.connect("closed", closed)
        dlg.present(self)

    @staticmethod
    def _combo(title, items, selected):
        row = Adw.ComboRow(title=title, model=Gtk.StringList.new(items))
        row.set_selected(selected)
        return row

    @staticmethod
    def _list_monitors() -> list[str]:
        try:
            out = subprocess.run(["gpu-screen-recorder", "--list-monitors"],
                                 capture_output=True, text=True, timeout=5).stdout
            return [ln.split("|")[0] for ln in out.splitlines() if "|" in ln]
        except (OSError, subprocess.SubprocessError):
            return []

    def _pick_folder(self, *_):
        dlg = Gtk.FileDialog(title="Speicherort für Clips")
        dlg.set_initial_folder(Gio.File.new_for_path(str(Config.load().library_path)))

        def done(d, res):
            try:
                f = d.select_folder_finish(res)
            except GLib.Error:
                return
            path = f.get_path()
            home = str(Path.home())
            self.cfg.library = "~" + path[len(home):] if path.startswith(home) else path
            self.folder_row.set_subtitle(self.cfg.library)
        dlg.select_folder(self.win, None, done)

    def _save(self, *_):
        c = self.cfg
        c.clip_seconds = int(self.seconds.get_value())
        c.fps = self.FPS[self.fps.get_selected()]
        c.quality = self.QUALITY[self.quality.get_selected()][0]
        c.codec = self.CODEC[self.codec.get_selected()][0]
        c.monitor = self.monitors[self.monitor.get_selected()]
        c.desktop_audio = self.desktop_audio.get_active()
        c.microphone = self.microphone.get_active()
        c.cursor = self.cursor.get_active()
        c.discord_limit_mb = self.LIMITS[self.limit.get_selected()][0]
        c.notifications = self.notif.get_active()
        c.sound = self.sound.get_active()
        old_library = Config.load().library
        c.save()
        if c.recorder_signature() != self.before or c.library != old_library:
            if recorder.reload_daemon():
                self.win.toast("Instant Replay mit neuen Einstellungen neu gestartet")
        self.win.reload()
        self.win.refresh_status()


# ====================================================================== Fenster

class KlippWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="Klipp", default_width=1180, default_height=760)
        self.set_size_request(380, 400)
        self.cfg = Config.load()
        self.filter = ("all", None)
        self.cards: list[ClipCard] = []
        self.thumb_pool = ThreadPoolExecutor(max_workers=3)
        self._generation = 0
        self._service_busy = False

        self.toasts = Adw.ToastOverlay()
        self.split = Adw.NavigationSplitView(min_sidebar_width=220, max_sidebar_width=280)
        self.toasts.set_child(self.split)
        self.set_content(self.toasts)

        bp = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 720sp"))
        bp.add_setter(self.split, "collapsed", True)
        self.add_breakpoint(bp)

        self._build_sidebar()
        self._build_content()

        self.reload()
        self.refresh_status()
        GLib.timeout_add_seconds(2, self._poll)
        self._watch_index()

        keys = Gtk.ShortcutController(scope=Gtk.ShortcutScope.GLOBAL)
        keys.add_shortcut(Gtk.Shortcut(trigger=Gtk.ShortcutTrigger.parse_string("<Control>f"),
                                       action=Gtk.CallbackAction.new(self._focus_search)))
        self.add_controller(keys)

    # -- Aufbau
    def _build_sidebar(self):
        view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        menu = Gio.Menu()
        menu.append("Einstellungen", "win.prefs")
        menu.append("Clip-Ordner öffnen", "win.folder")
        menu.append("Über Klipp", "win.about")
        header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu,
                                       primary=True, tooltip_text="Menü"))
        view.add_top_bar(header)
        for name, cb in (("prefs", lambda *_: Preferences(self).present(self)),
                         ("folder", lambda *_: library.open_folder(self._library_root())),
                         ("about", self._about)):
            a = Gio.SimpleAction.new(name, None)
            a.connect("activate", cb)
            self.add_action(a)

        self.sidebar = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
        self.sidebar.add_css_class("navigation-sidebar")
        self.sidebar.connect("row-activated", self._on_sidebar)
        scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(self.sidebar)
        view.set_content(scroller)

        status = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        status.add_css_class("status-box")
        row = Gtk.Box(spacing=10)
        self.status_dot = Gtk.Image(icon_name="media-record-symbolic")
        row.append(self.status_dot)
        texts = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True)
        self.status_title = Gtk.Label(label="Instant Replay", xalign=0)
        self.status_title.add_css_class("heading")
        self.status_sub = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
        self.status_sub.add_css_class("caption")
        self.status_sub.add_css_class("dim-label")
        texts.append(self.status_title)
        texts.append(self.status_sub)
        row.append(texts)
        self.replay_switch = Gtk.Switch(valign=Gtk.Align.CENTER)
        self._switch_handler = self.replay_switch.connect("state-set", self._on_switch)
        row.append(self.replay_switch)
        status.append(row)
        self.save_btn = Gtk.Button()
        self.save_btn.set_child(Adw.ButtonContent(icon_name="media-record-symbolic",
                                                  label="Clip speichern"))
        self.save_btn.add_css_class("pill")
        self.save_btn.add_css_class("suggested-action")
        self.save_btn.add_css_class("save-button")
        self.save_btn.connect("clicked", self._save_now)
        status.append(self.save_btn)
        view.add_bottom_bar(status)

        self.split.set_sidebar(Adw.NavigationPage(title="Klipp", child=view))

    def _build_content(self):
        self.nav = Adw.NavigationView()
        page_view = Adw.ToolbarView()
        header = Adw.HeaderBar()
        self.search = Gtk.SearchEntry(placeholder_text="Clips durchsuchen", width_chars=26)
        self.search.connect("search-changed", lambda *_: self._apply_filter())
        header.set_title_widget(self.search)
        page_view.add_top_bar(header)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.empty = Adw.StatusPage(icon_name="camera-video-symbolic", title="Noch keine Clips",
                                    description=self._empty_text())
        self.stack.add_named(self.empty, "empty")

        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True,
                                min_children_per_line=1, max_children_per_line=8,
                                row_spacing=6, column_spacing=6, valign=Gtk.Align.START,
                                halign=Gtk.Align.START,
                                margin_top=12, margin_bottom=24, margin_start=18, margin_end=18)
        self.flow.set_filter_func(self._filter_card)
        self.heading = Gtk.Label(xalign=0, margin_start=24, margin_top=18)
        self.heading.add_css_class("title-2")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.append(self.heading)
        box.append(self.flow)
        scroller = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroller.set_child(box)
        self.stack.add_named(scroller, "grid")
        page_view.set_content(self.stack)

        self.library_page = Adw.NavigationPage(title="Clips", tag="library", child=page_view)
        self.nav.add(self.library_page)
        self.split.set_content(Adw.NavigationPage(title="Clips", child=self.nav))

    def _empty_text(self) -> str:
        st = recorder.read_state()
        secs = Config.load().clip_seconds
        if st.get("running"):
            key = hotkey.pretty(Config.load().hotkey)
            return f"Drück im Spiel {key}, um die letzten {secs} Sekunden zu speichern."
        return "Schalte unten links Instant Replay ein – dann speichert dein Hotkey die letzten Sekunden."

    # -- Daten
    def _library_root(self) -> Path:
        root = Config.load().library_path
        root.mkdir(parents=True, exist_ok=True)
        return root

    def reload(self):
        self.cfg = Config.load()
        clips = library.scan(self.cfg)
        self._generation += 1
        gen = self._generation
        self.flow.remove_all()
        self.cards = []
        for clip in clips:
            card = ClipCard(clip, self.open_clip)
            self.cards.append(card)
            self.flow.append(card)
            self.thumb_pool.submit(self._load_thumb, card, gen)
        self._rebuild_sidebar(clips)
        self._apply_filter()

    def _load_thumb(self, card: ClipCard, gen: int):
        if gen != self._generation:
            return
        clip = card.clip
        duration = clip.duration
        if duration is None:
            duration = library.probe_duration(clip.path)
            if duration is not None:
                library.update_entry(clip.path, duration=duration, game=clip.game,
                                     created=clip.created)
                clip.duration = duration
        thumb = library.ensure_thumbnail(clip.path, duration)
        GLib.idle_add(card.set_thumbnail, thumb, duration)

    def _rebuild_sidebar(self, clips):
        selected = self.filter
        while (row := self.sidebar.get_row_at_index(0)) is not None:
            self.sidebar.remove(row)
        games: dict[str, int] = {}
        for c in clips:
            games[c.game] = games.get(c.game, 0) + 1
        entries = [(("all", None), "Alle Clips", "view-grid-symbolic", len(clips)),
                   (("fav", None), "Favoriten", "starred-symbolic",
                    sum(1 for c in clips if c.favorite))]
        entries += [(("game", g), g, "input-gaming-symbolic", n)
                    for g, n in sorted(games.items(), key=lambda kv: kv[0].lower())]
        restore = None
        for i, (key, label, icon, count) in enumerate(entries):
            row = Gtk.ListBoxRow()
            row.key = key
            box = Gtk.Box(spacing=12, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
            box.append(Gtk.Image(icon_name=icon))
            box.append(Gtk.Label(label=label, xalign=0, hexpand=True,
                                 ellipsize=Pango.EllipsizeMode.END))
            n = Gtk.Label(label=str(count))
            n.add_css_class("dim-label")
            n.add_css_class("numeric")
            box.append(n)
            row.set_child(box)
            self.sidebar.append(row)
            if i == 2:
                row.set_header(Gtk.Separator(margin_top=6, margin_bottom=6,
                                             margin_start=6, margin_end=6))
            if key == selected:
                restore = row
        if restore is None:
            self.filter = ("all", None)
            restore = self.sidebar.get_row_at_index(0)
        self.sidebar.select_row(restore)

    def _on_sidebar(self, box, row):
        if getattr(row, "key", None):
            self.filter = row.key
            self._apply_filter()
            if self.nav.get_visible_page() is not self.library_page:
                self.nav.pop_to_tag("library")
            self.split.set_show_content(True)

    def _filter_card(self, child) -> bool:
        return self._filter_card_clip(child.get_child().clip)

    def _apply_filter(self):
        self.flow.invalidate_filter()
        kind, value = self.filter
        title = {"all": "Alle Clips", "fav": "Favoriten"}.get(kind, value)
        visible = sum(1 for c in self.cards if self._filter_card_clip(c.clip))
        self.heading.set_label(title)
        self.library_page.set_title(title)
        if visible:
            self.stack.set_visible_child_name("grid")
        else:
            if self.cards:
                self.empty.set_title("Nichts gefunden" if self.search.get_text() else "Keine Clips hier")
                self.empty.set_description(None)
                self.empty.set_icon_name("edit-find-symbolic")
            else:
                self.empty.set_title("Noch keine Clips")
                self.empty.set_description(self._empty_text())
                self.empty.set_icon_name("camera-video-symbolic")
            self.stack.set_visible_child_name("empty")

    def _filter_card_clip(self, clip) -> bool:
        kind, value = self.filter
        if kind == "fav" and not clip.favorite:
            return False
        if kind == "game" and clip.game != value:
            return False
        q = self.search.get_text().strip().lower()
        return not q or q in clip.title.lower() or q in clip.game.lower()

    def _watch_index(self):
        INDEX_FILE.parent.mkdir(parents=True, exist_ok=True)
        self._monitor = Gio.File.new_for_path(str(INDEX_FILE.parent)).monitor_directory(
            Gio.FileMonitorFlags.WATCH_MOVES, None)
        self._reload_pending = False

        def changed(mon, f, other, event):
            name = (other or f).get_basename()
            if name == INDEX_FILE.name and not self._reload_pending:
                self._reload_pending = True
                GLib.timeout_add(400, self._deferred_reload)
        self._monitor.connect("changed", changed)

    def _deferred_reload(self):
        self._reload_pending = False
        # Nur neu laden, wenn sich die Dateimenge geändert hat (Favoriten etc. setzt die GUI selbst)
        current = {str(c.clip.path) for c in self.cards}
        fresh = {str(c.path) for c in library.scan(Config.load())}
        if current != fresh:
            self.reload()
        return False

    # -- Navigation
    def open_clip(self, clip: library.Clip):
        if not clip.path.exists():
            self.toast("Datei nicht mehr vorhanden")
            self.reload()
            return
        if self.nav.get_visible_page() is not self.library_page:
            self.nav.pop_to_tag("library")
        self.nav.push(PlayerPage(self, clip))
        self.split.set_show_content(True)

    def open_path(self, path: Path):
        for c in self.cards:
            if c.clip.path == path:
                self.open_clip(c.clip)
                return
        self.reload()
        for c in self.cards:
            if c.clip.path == path:
                self.open_clip(c.clip)
                return

    def _focus_search(self, *_):
        self.search.grab_focus()
        return True

    # -- Status / Recorder
    def refresh_status(self):
        st = recorder.read_state()
        running = bool(st.get("running"))
        self.replay_switch.handler_block(self._switch_handler)
        if not self._service_busy:
            self.replay_switch.set_active(running)
            self.replay_switch.set_state(running)
        self.replay_switch.handler_unblock(self._switch_handler)
        self.save_btn.set_sensitive(running)
        for c in ("rec-dot", "idle-dot"):
            self.status_dot.remove_css_class(c)
        self.status_dot.add_css_class("rec-dot" if running else "idle-dot")
        if running:
            self.status_sub.set_label(f"Läuft · letzte {st.get('seconds')} s · {st.get('fps')} FPS")
            self.status_sub.set_tooltip_text(f"Bildschirm {st.get('monitor')}")
        elif st.get("error"):
            self.status_sub.set_label(st["error"])
            self.status_sub.set_tooltip_text(st["error"])
        else:
            self.status_sub.set_label("Aus")
            self.status_sub.set_tooltip_text(None)

    def _poll(self):
        self.refresh_status()
        return True

    def _on_switch(self, sw, state):
        self._service_busy = True
        action = "start" if state else "stop"

        def work():
            if _has_service():
                subprocess.run(["systemctl", "--user", action, "klipp.service"], timeout=20)
            elif state:
                subprocess.Popen([sys.executable, "-m", "klipp", "daemon"],
                                 start_new_session=True, env=recorder._pkg_env(),
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                pid = recorder.read_state().get("daemon_pid")
                if pid:
                    import os
                    import signal
                    os.kill(pid, signal.SIGTERM)
            deadline = time.time() + 8
            while time.time() < deadline and bool(recorder.read_state().get("running")) != state:
                time.sleep(0.3)

        def done(_):
            self._service_busy = False
            self.refresh_status()
            if bool(recorder.read_state().get("running")) != state and state:
                err = recorder.read_state().get("error") or "Start fehlgeschlagen"
                self.toast(f"Instant Replay: {err}")
            self._apply_filter()
        run_bg(work, done, lambda e: (done(None), self.toast(str(e))))
        sw.set_state(state)
        return True

    def _save_now(self, *_):
        self.save_btn.set_sensitive(False)

        def done(path):
            self.save_btn.set_sensitive(True)
            self.reload()
            self.toast("Clip gespeichert", action=("Ansehen", lambda: self.open_path(path)))

        def fail(e):
            self.save_btn.set_sensitive(True)
            self.toast(str(e))
        run_bg(lambda: recorder.save_clip(notify=False), done, fail)

    # -- Sonstiges
    def toast(self, text: str, timeout: int = 4, action=None):
        t = Adw.Toast(title=GLib.markup_escape_text(text), timeout=timeout)
        if action:
            label, cb = action
            t.set_button_label(label)
            t.connect("button-clicked", lambda *_: cb())
        self.toasts.add_toast(t)

    def _about(self, *_):
        Adw.AboutDialog(application_name="Klipp", application_icon=APP_ID, version=__version__,
                        developer_name="Andrin & Claude",
                        comments="Instant-Replay-Clips für Linux – wie Medal, "
                                 "auf Basis von GPU Screen Recorder.",
                        license_type=Gtk.License.MIT_X11).present(self)


def _has_service() -> bool:
    r = subprocess.run(["systemctl", "--user", "cat", "klipp.service"],
                       capture_output=True, timeout=5)
    return r.returncode == 0


# ====================================================================== App

class KlippApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.HANDLES_OPEN)
        self.win = None
        self.connect("activate", self._activate)
        self.connect("open", self._open)

    def _ensure_window(self):
        if self.win is None:
            provider = Gtk.CssProvider()
            provider.load_from_string(CSS)
            Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), provider,
                                                      Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            self.win = KlippWindow(self)
            q = Gio.SimpleAction.new("quit", None)
            q.connect("activate", lambda *_: self.quit())
            self.add_action(q)
            self.set_accels_for_action("app.quit", ["<Control>q"])
            self.set_accels_for_action("win.prefs", ["<Control>comma"])
        return self.win

    def _activate(self, *_):
        self._ensure_window().present()

    def _open(self, app, files, n, hint):
        win = self._ensure_window()
        win.present()
        if files and files[0].get_path():
            path = Path(files[0].get_path())
            win.reload()
            win.open_path(path)


def run(args: list[str]) -> int:
    return KlippApp().run([sys.argv[0], *args])
