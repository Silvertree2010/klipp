#!/usr/bin/env bash
# Klipp installieren (Arch Linux, pro Benutzer, ohne root ausser für Pakete)
#   ./install.sh            installieren / aktualisieren
#   ./install.sh --remove   deinstallieren (Clips bleiben erhalten)
set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB="$HOME/.local/lib/klipp"
BIN="$HOME/.local/bin/klipp"
APPS="$HOME/.local/share/applications"
ICONS="$HOME/.local/share/icons/hicolor/scalable/apps"
UNIT="$HOME/.config/systemd/user/klipp.service"
DEPS=(gpu-screen-recorder python python-gobject gtk4 libadwaita gst-plugins-good
      gst-libav ffmpeg wl-clipboard libnotify pipewire)

say() { printf '\033[1;35m::\033[0m %s\n' "$*"; }

if [[ "${1:-}" == "--remove" ]]; then
  systemctl --user disable --now klipp.service 2>/dev/null || true
  rm -rf "$LIB" "$BIN" "$UNIT" "$APPS/dev.klipp.Klipp.desktop" "$ICONS/dev.klipp.Klipp.svg"
  systemctl --user daemon-reload
  say "Klipp entfernt. Clips und Einstellungen liegen weiter in ~/Videos/Klipp und ~/.config/klipp."
  exit 0
fi

missing=()
for p in "${DEPS[@]}"; do pacman -Qq "$p" &>/dev/null || missing+=("$p"); done
if ((${#missing[@]})); then
  say "Fehlende Pakete: ${missing[*]}"
  if ! sudo pacman -S --needed --noconfirm "${missing[@]}"; then
    echo "Pakete konnten nicht installiert werden. Erst 'sudo pacman -Syu' laufen lassen, dann nochmal." >&2
    exit 1
  fi
fi

say "Kopiere Programm nach $LIB"
rm -rf "$LIB"
mkdir -p "$LIB" "$(dirname "$BIN")" "$APPS" "$ICONS" "$(dirname "$UNIT")"
cp -r "$SRC/klipp" "$LIB/"
find "$LIB" -name __pycache__ -prune -exec rm -rf {} +

cat > "$BIN" <<EOF
#!/bin/sh
export PYTHONPATH="$LIB\${PYTHONPATH:+:\$PYTHONPATH}"
exec /usr/bin/python3 -m klipp "\$@"
EOF
chmod +x "$BIN"

install -m644 "$SRC/data/dev.klipp.Klipp.desktop" "$APPS/"
sed -i "s|^Exec=klipp|Exec=$BIN|" "$APPS/dev.klipp.Klipp.desktop"
install -m644 "$SRC/data/dev.klipp.Klipp.svg" "$ICONS/"
install -m644 "$SRC/data/klipp.service" "$UNIT"
update-desktop-database "$APPS" &>/dev/null || true
gtk-update-icon-cache -q "$HOME/.local/share/icons/hicolor" &>/dev/null || true

say "Starte Instant Replay (startet ab jetzt automatisch beim Login)"
systemctl --user daemon-reload
systemctl --user enable klipp.service &>/dev/null
systemctl --user restart klipp.service

# Hotkey: klassische hyprland.conf automatisch, Lua-Config nur als Hinweis
HYPR="$HOME/.config/hypr/hyprland.conf"
BIND="bind = ALT, F10, exec, $BIN save"
if [[ -n "${KLIPP_NO_HOTKEY:-}" ]]; then
  say "Hotkey übersprungen (KLIPP_NO_HOTKEY)."
elif grep -qs "klipp save" "$HOME"/.config/hypr/*.conf "$HOME"/.config/hypr/**/*.lua 2>/dev/null; then
  say "Hotkey ist schon eingerichtet."
elif [[ -f "$HYPR" ]] && ! compgen -G "$HOME/.config/hypr/*.lua" >/dev/null && \
     ! compgen -G "$HOME/.config/hypr/config/*.lua" >/dev/null; then
  printf '\n# Klipp – Clip speichern\n%s\n' "$BIND" >> "$HYPR"
  hyprctl reload &>/dev/null || true
  say "Hotkey Alt+F10 in hyprland.conf eingetragen."
else
  say "Hotkey bitte selbst in der Hyprland-Config eintragen – Befehl: $BIN save"
fi

sleep 2
"$BIN" status || true
say "Fertig. Klipp im App-Launcher öffnen, im Spiel Alt+F10 drücken."
