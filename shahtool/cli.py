"""Shahtool terminal interface."""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import threading
from typing import List, Optional

from . import __version__, core

USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(code: str) -> str:
    return f"\033[{code}m" if USE_COLOR else ""


ORANGE, CYAN, GREEN = _c("38;5;208"), _c("38;5;51"), _c("38;5;48")
RED, GRAY, YELLOW = _c("38;5;196"), _c("38;5;242"), _c("38;5;220")
PURPLE, BOLD, RESET = _c("38;5;141"), _c("1"), _c("0")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class Abort(Exception):
    pass


def vlen(s: str) -> int:
    return len(_ANSI.sub("", s))


def box(lines: List[str], color: str = PURPLE) -> None:
    width = max(vlen(l) for l in lines) + 4
    width = min(max(width, 44), max(shutil.get_terminal_size((80, 24)).columns - 2, 44))
    print(f"{color}╭{'─' * (width - 2)}╮{RESET}")
    for l in lines:
        print(f"{color}│{RESET} {l}{' ' * max(0, width - vlen(l) - 3)}{color}│{RESET}")
    print(f"{color}╰{'─' * (width - 2)}╯{RESET}")


def ask(prompt: str) -> str:
    try:
        return input(f"\n {BOLD}{CYAN}{prompt} ❯{RESET} ").strip()
    except (KeyboardInterrupt, EOFError):
        print()
        raise Abort


def ok(msg: str) -> None:
    print(f" {GREEN}✔{RESET} {msg}")


def warn(msg: str) -> None:
    print(f" {YELLOW}! {msg}{RESET}")


def err(msg: str) -> None:
    print(f" {RED}✖ {msg}{RESET}")


def progress_bar(done: int, total: int) -> None:
    pct = done / total if total else 0
    filled = int(pct * 30)
    sys.stdout.write(f"\r {CYAN}[{'█' * filled}{'░' * (30 - filled)}]{RESET} "
                     f"{pct * 100:5.1f}%  {core.human_size(done)} / {core.human_size(total)}  ")
    sys.stdout.flush()
    if done >= total:
        print()


def banner() -> None:
    fb = core.find_tool("fastboot")
    devs = core.fastboot_devices() if fb else []
    status = (f"{GREEN}device: {devs[0]}{RESET}" if devs else
              f"{YELLOW}no device in fastboot{RESET}") if fb else f"{RED}fastboot missing{RESET}"
    box([f"{BOLD}{CYAN}⚡ SHAHTOOL{RESET} {GRAY}v{__version__}{RESET}",
         f"{GRAY}Status:{RESET} {status}"])


def pick(items: list, label, prompt: str):
    """Numbered picker. Returns the item or None if the user types q/b."""
    for i, it in enumerate(items, 1):
        print(f"  {ORANGE}[{i}]{RESET} {label(it)}")
    while True:
        ch = ask(prompt).lower()
        if ch in ("q", "b", ""):
            return None
        if ch.isdigit() and 1 <= int(ch) <= len(items):
            return items[int(ch) - 1]
        err(f"Enter a number from 1 to {len(items)}, or q to go back")


def confirm(question: str, default: bool = False) -> bool:
    ans = ask(f"{question} [{'Y/n' if default else 'y/N'}]").lower()
    return default if not ans else ans.startswith("y")


# ---------------------------------------------------------------- flows
def choose_rom() -> Optional[str]:
    print(f"\n {GRAY}Scanning for ROMs (use 'shahtool config' to change folders)...{RESET}")
    roms = core.scan_roms()
    if roms:
        box([f"{BOLD}Detected ROMs ({len(roms)}){RESET}"], CYAN)
        labels = lambda r: (f"{r.name} {YELLOW}[{core.human_size(r.size)}]{RESET}" if r.kind == "archive"
                            else f"{r.name} {GREEN}[extracted folder]{RESET}")
        for i, r in enumerate(roms, 1):
            print(f"  {ORANGE}[{i}]{RESET} {labels(r)}\n      {GRAY}{r.path}{RESET}")
        print(f"  {ORANGE}[p]{RESET} Type a path yourself")
        while True:
            ch = ask("select ROM").lower()
            if ch in ("q", "b", ""):
                return None
            if ch == "p":
                break
            if ch.isdigit() and 1 <= int(ch) <= len(roms):
                return roms[int(ch) - 1].path
            err("Invalid choice")
    else:
        warn("No ROM found automatically.")
    path = ask("path to ROM archive or folder").strip("'\" ")
    return os.path.expanduser(path) if path else None


def verify_flow(path: str) -> bool:
    print(f"\n {CYAN}Verifying file integrity...{RESET}")
    try:
        res = core.verify_archive(path, progress=progress_bar)
    except Exception as exc:
        err(str(exc))
        return False
    if res["ok"] is None:
        warn(f"No expected checksum known. {res['algo']}: {res['actual']}")
        return True
    if res["ok"]:
        ok(f"{res['algo'].upper()} matches ({res['source']})")
        return True
    err(f"{res['algo'].upper()} MISMATCH ({res['source']}). The file may be corrupted.")
    return False


def flash_flow(path: Optional[str] = None) -> None:
    path = path or choose_rom()
    if not path:
        return
    if not os.path.exists(path):
        err(f"Not found: {path}")
        return
    if os.path.isfile(path) and confirm("Verify checksum before extracting?", False):
        if not verify_flow(path) and not confirm("Continue anyway?", False):
            return
    try:
        rom_dir = core.prepare_rom(path, log=lambda m: print(f" {GRAY}{m}{RESET}"),
                                   progress=progress_bar)
    except (Exception, KeyboardInterrupt) as exc:
        err("Cancelled" if isinstance(exc, KeyboardInterrupt) else str(exc))
        return
    ok(f"ROM folder: {rom_dir}")

    while True:
        scripts = core.discover_scripts(rom_dir)
        if scripts:
            break
        warn(f"No flash script found in {rom_dir}")
        ask("Put a script there and press Enter (Ctrl+C to cancel)")
    badge = {"clean": f"{RED}erases data{RESET}", "keep_data": f"{GREEN}keeps data{RESET}",
             "lock": f"{RED}RE-LOCKS bootloader{RESET}", "other": ""}
    box([f"{BOLD}Available flashing scripts{RESET}"], ORANGE)
    script = pick(scripts, lambda s: f"{s.name}  {badge[s.kind]}", "select script (q to cancel)")
    if not script:
        return
    if confirm("Preview the fastboot commands first?", False):
        for cmd in core.script_commands(script.path):
            print(f"   {GRAY}{cmd}{RESET}")

    print(f"\n {GRAY}Connect the phone in fastboot mode (or with USB debugging) via OTG.{RESET}")
    stop = threading.Event()
    spin = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
    tick = [0]

    def on_tick(msg: str) -> None:
        tick[0] += 1
        sys.stdout.write(f"\r\033[K {CYAN}{spin[tick[0] % 10]}{RESET} {GRAY}{msg}... "
                         f"(Ctrl+C to cancel){RESET}")
        sys.stdout.flush()

    try:
        serial = core.wait_for_device(on_tick=on_tick, cancel=stop)
    except KeyboardInterrupt:
        print("\n Cancelled.")
        return
    print("\r\033[K", end="")
    ok(f"Device connected: {serial}")

    checks = core.preflight(script, rom_dir, os.path.basename(path), serial)
    mark = {"ok": f"{GREEN}✔{RESET}", "warn": f"{YELLOW}!{RESET}", "fail": f"{RED}✖{RESET}",
            "info": f"{CYAN}i{RESET}"}
    box([f"{BOLD}Safety checks{RESET}"], YELLOW)
    for c in checks:
        print(f"  {mark[c.status]} {c.name}: {c.detail}")
    if core.has_failures(checks):
        err("Fix the failed checks above and try again.")
        return

    word = "LOCK" if script.locks_bootloader else "FLASH"
    try:
        if ask(f"Type {word} to start flashing (anything else cancels)") != word:
            print(" Cancelled.")
            return
    except Abort:
        return

    print(f"\n {GREEN}▶ Flashing{RESET} {BOLD}{script.name}{RESET}   "
          f"{RED}Do not unplug the cable or close Termux.{RESET}\n{GRAY}{'─' * 50}{RESET}")
    try:
        rc, log_path = core.run_flash(rom_dir, script, log=print, serial=serial)
    except Exception as exc:
        err(str(exc))
        return
    print(f"{GRAY}{'─' * 50}{RESET}")
    (ok if rc == 0 else err)(f"Flash {'completed' if rc == 0 else f'FAILED (exit code {rc})'}. "
                             f"Log: {log_path}")


def device_flow() -> None:
    serial = core.wait_for_device(timeout=5, auto_reboot_adb=False)
    if not serial:
        warn("No device in fastboot mode.")
        return
    for k, v in core.device_info(serial).items():
        print(f"  {GRAY}{k:<20}{RESET} {v if v is not None else '-'}")


def tools_flow() -> None:
    items = [("Reboot to system", "reboot_system"), ("Reboot to bootloader", "reboot_bootloader"),
             ("Reboot to recovery", "reboot_recovery"), ("Reboot to fastbootd", "reboot_fastbootd"),
             ("Set active slot A", "set_slot_a"), ("Set active slot B", "set_slot_b"),
             ("Dump all fastboot variables", "getvar_all"),
             ("Wipe data (fastboot -w)", "wipe_data")]
    sel = pick(items, lambda i: i[0], "choose action (q to go back)")
    if not sel:
        return
    if sel[1] in core.DANGEROUS_ACTIONS and ask("Type WIPE to erase all data") != "WIPE":
        print(" Cancelled.")
        return
    rc, out = core.do_action(sel[1])
    print(out.strip())
    (ok if rc == 0 else err)("Done" if rc == 0 else f"Failed (exit code {rc})")


def partition_flow() -> None:
    part = ask("partition name (for example boot, recovery, vbmeta)")
    img = os.path.expanduser(ask("path to image file").strip("'\" "))
    if not confirm(f"Flash {img} to '{part}'?", False):
        return
    try:
        rc, out = core.flash_partition(part, img)
    except ValueError as exc:
        err(str(exc))
        return
    print(out.strip())
    (ok if rc == 0 else err)("Done" if rc == 0 else f"Failed (exit code {rc})")


def clean_flow() -> None:
    root = core.extract_root()
    if confirm(f"Delete ROMs extracted by Shahtool in {root}?", False):
        ok(f"Freed {core.human_size(core.clean_extracted())}")


def update_flow() -> None:
    print(f" {GRAY}Checking for updates...{RESET}")
    latest = core.check_update()
    if not latest:
        ok(f"You are on the latest version ({__version__}) or the update server is unreachable")
        return
    print(f" New version available: {GREEN}{latest}{RESET} (you have {__version__})")
    if confirm("Update now?", True):
        rc, out = core.self_update()
        print(out.strip())
        (ok if rc == 0 else err)("Updated. Restart Shahtool." if rc == 0 else "Update failed")


def gui_flow() -> None:
    from . import gui
    gui.serve(open_browser=True)


MENU = [
    ("Flash a ROM", flash_flow),
    ("Device info", device_flow),
    ("Reboot and device tools", tools_flow),
    ("Flash a single partition", partition_flow),
    ("Verify a file checksum", lambda: verify_flow(os.path.expanduser(ask("path to file").strip("'\" ")))),
    ("Open web GUI", gui_flow),
    ("Delete extracted ROMs", clean_flow),
    ("Check for updates", update_flow),
]


def menu() -> None:
    while True:
        print()
        banner()
        lines = [f"{BOLD}MAIN MENU{RESET}"] + [f"{ORANGE}[{i}]{RESET} {name}" for i, (name, _) in
                                                 enumerate(MENU, 1)] + [f"{GRAY}[q] Exit{RESET}"]
        box(lines)
        try:
            ch = ask("shahtool").lower()
        except Abort:
            return
        if ch in ("q", "quit", "exit"):
            return
        if ch.isdigit() and 1 <= int(ch) <= len(MENU):
            try:
                MENU[int(ch) - 1][1]()
            except (Abort, KeyboardInterrupt):
                print(f"\n {GRAY}Back to menu.{RESET}")
        else:
            err("Invalid selection")


# ---------------------------------------------------------------- argparse
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="shahtool", description="Shahtool: Xiaomi fastboot ROM flasher")
    p.add_argument("--version", action="version", version=f"Shahtool {__version__}")
    sub = p.add_subparsers(dest="cmd")
    f = sub.add_parser("flash", help="flash a ROM archive or folder")
    f.add_argument("path", nargs="?")
    g = sub.add_parser("gui", help="start the web GUI")
    g.add_argument("--host", default="127.0.0.1")
    g.add_argument("--port", type=int, default=8765)
    g.add_argument("--no-browser", action="store_true")
    sub.add_parser("devices", help="list connected devices")
    sub.add_parser("info", help="show fastboot device info")
    sub.add_parser("scan", help="list ROMs found on this device")
    v = sub.add_parser("verify", help="verify a ROM checksum")
    v.add_argument("path")
    v.add_argument("--hash", help="expected md5 or sha256")
    r = sub.add_parser("reboot", help="reboot the connected device")
    r.add_argument("target", choices=["system", "bootloader", "recovery", "fastbootd"])
    sub.add_parser("clean", help="delete extracted ROMs")
    sub.add_parser("update", help="check for and install updates")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.cmd is None:
            menu()
        elif args.cmd == "flash":
            flash_flow(args.path)
        elif args.cmd == "gui":
            from . import gui
            gui.serve(args.host, args.port, open_browser=not args.no_browser)
        elif args.cmd == "devices":
            fb = core.fastboot_devices()
            adb = core.adb_devices()
            print("fastboot:", ", ".join(fb) or "none")
            print("adb:", ", ".join(f"{s} ({st})" for s, st in adb) or "none")
        elif args.cmd == "info":
            device_flow()
        elif args.cmd == "scan":
            for r in core.scan_roms():
                print(f"{r.kind:<8} {core.human_size(r.size):>9}  {r.path}")
        elif args.cmd == "verify":
            return 0 if verify_flow_cli(args.path, args.hash) else 1
        elif args.cmd == "reboot":
            rc, out = core.do_action(f"reboot_{args.target}")
            print(out.strip())
            return rc
        elif args.cmd == "clean":
            print(f"Freed {core.human_size(core.clean_extracted())}")
        elif args.cmd == "update":
            update_flow()
    except (Abort, KeyboardInterrupt):
        print("\nAborted.")
        return 130
    return 0


def verify_flow_cli(path: str, expected: Optional[str]) -> bool:
    try:
        res = core.verify_archive(path, expected, progress=progress_bar)
    except Exception as exc:
        err(str(exc))
        return False
    print(f"{res['algo']}: {res['actual']}")
    if res["ok"] is None:
        warn("No expected checksum to compare with")
        return True
    (ok if res["ok"] else err)("Checksum matches" if res["ok"] else "Checksum MISMATCH")
    return bool(res["ok"])


if __name__ == "__main__":
    sys.exit(main())
