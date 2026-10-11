# Klipp

Instant-replay game clips for Linux, like Medal, built on [GPU Screen Recorder](https://git.dec05eba.com/gpu-screen-recorder/).

- **Always recording in the background**: Klipp keeps the last 30 s (configurable) in RAM and barely costs any FPS thanks to NVENC
- **Press a hotkey** (Alt+F10): the clip is saved, with a shutter sound and a notification that shows a thumbnail
- **Sorted by game**: Steam games are detected by their app ID (Proton too), anything else by its window
- **Library**: thumbnails, search, favorites and a sidebar with every game
- **Player with trimming**: drag the handles and click "Schnitt speichern" (save cut); the original is kept
- **Discord-ready**: compresses clips to 10/50/500 MB and copies them to the clipboard, or you can drag cards straight into Discord

The UI is in German.

## Install (Arch Linux + Hyprland)

```sh
sudo pacman -Syu
git clone https://github.com/Silvertree2010/klipp.git ~/klipp && cd ~/klipp
./install.sh
```

The installer pulls missing packages and installs Klipp to `~/.local`. It enables `klipp.service`, so instant replay starts whenever you log in. With a classic `hyprland.conf` it adds the Alt+F10 hotkey itself. With a Lua config you have to bind a key to `~/.local/bin/klipp save` yourself.

Update: `git pull && ./install.sh`. Uninstall: `./install.sh --remove`. Your clips and settings are kept.

## Commands

| Command | |
|---|---|
| `klipp` | Open the library |
| `klipp save [SEC]` | Save a clip (bind this to a hotkey) |
| `klipp status` | Is instant replay running? |
| `klipp start` / `stop` | Turn instant replay on/off |

## Where things live

- Clips: `~/Videos/Klipp/<Game>/`, Discord exports in `~/Videos/Klipp/.discord/`
- Settings: `~/.config/klipp/config.toml` (or in the app: menu → Einstellungen)
- Favorites/metadata: `~/.local/share/klipp/index.json`
- Logs: `journalctl --user -u klipp`

## License

MIT
