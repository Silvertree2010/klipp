"""klipp – Kommandozeile.

  klipp                 Bibliothek öffnen
  klipp save [SEK]      Clip speichern (für den Hotkey)
  klipp status          Zeigt, ob Instant Replay läuft
  klipp start|stop      Instant Replay starten/stoppen
  klipp daemon          Recorder im Vordergrund (für systemd)
  klipp open DATEI      Clip in der Bibliothek öffnen
"""
import subprocess
import sys

from . import __version__


def _service(action: str) -> int:
    return subprocess.run(["systemctl", "--user", action, "klipp.service"]).returncode


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    cmd = argv[0] if argv else "gui"

    if cmd in ("-h", "--help", "help"):
        print(__doc__.strip())
        return 0
    if cmd in ("-V", "--version"):
        print(f"klipp {__version__}")
        return 0
    if cmd == "daemon":
        from .recorder import run_daemon
        return run_daemon()
    if cmd == "save":
        from .recorder import save_clip
        seconds = int(argv[1]) if len(argv) > 1 and argv[1].isdigit() else None
        try:
            path = save_clip(seconds)
        except Exception as e:  # noqa: BLE001 – Fehler als Notification zeigen
            subprocess.run(["notify-send", "-a", "Klipp", "-u", "critical",
                            "Clip nicht gespeichert", str(e)])
            print(f"klipp: {e}", file=sys.stderr)
            return 1
        print(path)
        return 0
    if cmd == "status":
        from .recorder import read_state
        st = read_state()
        if st.get("running"):
            print(f"Instant Replay läuft · {st.get('monitor')} · {st.get('fps')} FPS · "
                  f"letzte {st.get('seconds')} s")
            return 0
        print("Instant Replay aus" + (f" – {st['error']}" if st.get("error") else ""))
        return 3
    if cmd in ("start", "stop", "restart"):
        return _service(cmd)
    if cmd == "_notify":
        from .recorder import notify_and_wait
        notify_and_wait(argv[1], argv[2])
        return 0
    if cmd in ("gui", "open"):
        from .gui import run
        return run(argv[1:] if cmd == "open" else [])
    print(f"klipp: unbekannter Befehl '{cmd}'\n\n{__doc__.strip()}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
