# Shahtool

Xiaomi / Redmi / POCO **fastboot ROM flasher** for **Termux (Android)** and **PC**, with a terminal menu and a web GUI.

Based on the idea of Ritik's Hybrid ROM flasher, rewritten with bug fixes and many new features.

## Install

### Termux (flash from one phone to another with an OTG cable)
1. Install [Termux](https://github.com/termux/termux-app/releases/latest) and [Termux:API](https://github.com/termux/termux-api/releases/latest).
2. In Termux:
```bash
termux-setup-storage
curl -fsSL https://raw.githubusercontent.com/YOUR_GITHUB_USERNAME/Shahtool/main/install.sh | bash
```

### PC (Windows / Linux / macOS)
Install Python 3.8+ and [platform-tools](https://developer.android.com/tools/releases/platform-tools) (fastboot must be in PATH), then:
```bash
pip install git+https://github.com/YOUR_GITHUB_USERNAME/Shahtool.git
```

## Use

| Command | What it does |
|---|---|
| `shahtool` | Terminal menu |
| `shahtool gui` | Web GUI. Open the printed link in your browser (phone or PC) |
| `shahtool flash [path]` | Flash a ROM archive or folder |
| `shahtool scan` / `devices` / `info` | List ROMs, devices, device details |
| `shahtool verify FILE [--hash H]` | Check md5 / sha256 |
| `shahtool reboot system\|bootloader\|recovery\|fastbootd` | Reboot the phone |
| `shahtool clean` / `update` | Delete extracted ROMs / update Shahtool |

## What is new compared with the original

**Bug fixes**
- tar.gz ROMs without a top folder lost their first path component (`--strip-components=1`). Fixed.
- Folder flattening crashed when an inner folder had the same name as its child. Fixed.
- Importing the script ran everything at module level and used a wrong `#!/usr/bin/python` shebang. Now a proper package.
- All shell commands were built with `os.system` and quotes, so file names with `'` broke or injected commands. Everything now uses argument lists.
- Flash result was ignored (always exit 0). Now the real exit code is shown and logged.
- ROM scripts were edited in place (`sed -i`). Now a cleaned **copy** runs and the original stays untouched.
- Extraction could leave a half-extracted ROM, and the previous ROM was deleted without asking. Now partial extractions are removed and finished ones are reused.
- Archives could write outside the target folder (path traversal). Now blocked.
- The whole of `/sdcard` was scanned without limits and ROM folders were skipped if their path contained words like `kernel`. Scan is now depth limited and only checks file names.
- Version mismatch (2.0.0 vs 2.0.1), broken README code block, hidden installer errors. Fixed.

**New features**
- **Web GUI** (works in the phone browser inside Termux, no extra packages)
- Safety checks before flashing: device connected, bootloader unlocked, ROM codename vs phone, battery (Termux), anti-rollback level
- Script type detection: clean flash (erases data), dirty flash (keeps data), **re-lock bootloader** (extra confirmation word)
- Preview the fastboot commands before running
- Checksum verification (sidecar `.md5`/`.sha256`, MD5 tag in Xiaomi file names, or your own hash)
- Native zip / tar.gz extraction with a progress bar and free space check (7z/rar via 7z or unrar)
- Auto reboot from ADB to bootloader, with a cancel option
- Device tools: reboot targets, switch slot A/B, dump variables, flash one partition, wipe data
- Flash logs in `~/.shahtool/logs`
- Update check and one-command update, cleanup of extracted ROMs

## GUI security
The GUI listens on `127.0.0.1` only. Every request needs the random token in the printed link and a matching Host header, so websites you visit cannot control your flasher. Use `--host 0.0.0.0` only on a network you trust.

## Safety
Flashing can brick a phone. Use the official fastboot ROM for **your exact device**, keep the battery charged, never unplug during a flash, and never flash an older ROM than your anti-rollback level. You use this tool at your own risk.

## Develop
```bash
python -m unittest discover -s tests -v
python -m shahtool
```
Before publishing, replace `YOUR_GITHUB_USERNAME` in `shahtool/__init__.py`, `install.sh` and this README.

## Credits
Original idea and Termux flow: **Ritik** ([Rittik-Hybrib-rom-flasher](https://github.com/rittik55/Rittik-Hybrib-rom-flasher)). termux-adb by [nohajc](https://github.com/nohajc/termux-adb).
