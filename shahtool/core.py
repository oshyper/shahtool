"""Shahtool core logic: devices, ROM discovery, extraction, flashing.

Standard library only. No UI code lives here, so the CLI and the web GUI
share exactly the same behaviour.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.request
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import RAW_VERSION_URL, __version__

ARCHIVE_EXTS = (".tgz", ".tar.gz", ".zip", ".7z", ".rar")
IGNORED_KEYWORDS = ("module", "ksun", "magisk", "susfs", "kernel")
JUNK_NAMES = {"__MACOSX", ".DS_Store"}
MARKER = ".shahtool_extracted"
RUN_COPY = ".shahtool_run.sh"

DATA_DIR = Path(os.environ.get("SHAHTOOL_HOME", str(Path.home() / ".shahtool")))
LOG_DIR = DATA_DIR / "logs"
CONFIG_FILE = DATA_DIR / "config.json"

Log = Callable[[str], None]
Progress = Callable[[int, int], None]


class Cancelled(Exception):
    """Raised when the user cancels a long running job."""


# --------------------------------------------------------------------------
# Environment / config
# --------------------------------------------------------------------------
def is_termux() -> bool:
    return "com.termux" in os.environ.get("PREFIX", "")


def is_windows() -> bool:
    return os.name == "nt"


def is_android() -> bool:
    return is_termux() or (os.name == "posix" and os.path.isdir("/sdcard"))


DEFAULT_CONFIG = {
    "min_rom_mb": 100,
    "scan_depth": 4,
    "extract_dir": None,
    "scan_dirs": [],
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    return cfg


def save_config(cfg: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def find_tool(name: str) -> Optional[str]:
    names = [name]
    if is_termux() and name in ("fastboot", "adb"):
        names.insert(0, "termux-" + name)
    for n in names:
        path = shutil.which(n)
        if path:
            return path
    return None


def run(cmd: Sequence[str], timeout: int = 15) -> Tuple[int, str]:
    """Run a command (never through a shell) and return (code, output)."""
    try:
        p = subprocess.run(
            list(cmd), capture_output=True, text=True, errors="replace", timeout=timeout
        )
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except FileNotFoundError:
        return 127, f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except OSError as exc:
        return 126, str(exc)


# --------------------------------------------------------------------------
# Devices
# --------------------------------------------------------------------------
def fastboot_devices() -> List[str]:
    fb = find_tool("fastboot")
    if not fb:
        return []
    _, out = run([fb, "devices"], 8)
    serials = []
    for line in out.splitlines():
        parts = line.split()
        # "no permissions" lines have parts[1] == "no" and are skipped on purpose
        if len(parts) >= 2 and parts[1].lower() == "fastboot":
            serials.append(parts[0])
    return serials


def adb_devices() -> List[Tuple[str, str]]:
    adb = find_tool("adb")
    if not adb:
        return []
    _, out = run([adb, "devices"], 8)
    devs = []
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            devs.append((parts[0], parts[1]))
    return devs


def getvar(name: str, serial: Optional[str] = None) -> Optional[str]:
    fb = find_tool("fastboot")
    if not fb:
        return None
    cmd = [fb] + (["-s", serial] if serial else []) + ["getvar", name]
    _, out = run(cmd, 10)
    m = re.search(rf"^{re.escape(name)}:\s*(.*)$", out, re.M)
    return m.group(1).strip() if m else None


INFO_KEYS = ("product", "serialno", "unlocked", "current-slot", "anti",
             "version-bootloader", "secure", "is-userspace")


def device_info(serial: Optional[str] = None) -> Dict[str, Optional[str]]:
    return {k: getvar(k, serial) for k in INFO_KEYS}


def wait_for_device(
    on_tick: Optional[Callable[[str], None]] = None,
    timeout: Optional[float] = None,
    cancel: Optional[threading.Event] = None,
    auto_reboot_adb: bool = True,
) -> Optional[str]:
    """Block until a device is in fastboot. A device in adb mode is rebooted
    into the bootloader automatically. Returns the serial or None."""
    start = time.time()
    rebooted = set()
    while True:
        devs = fastboot_devices()
        if devs:
            return devs[0]
        state_msg = "waiting"
        if auto_reboot_adb:
            adb = find_tool("adb")
            for serial, state in adb_devices():
                if state == "device" and serial not in rebooted and adb:
                    run([adb, "-s", serial, "reboot", "bootloader"], 10)
                    rebooted.add(serial)
                    state_msg = "adb device found, rebooting to bootloader"
                elif state == "unauthorized":
                    state_msg = "allow the USB debugging prompt on the phone"
        if on_tick:
            on_tick(state_msg)
        if cancel is not None and cancel.is_set():
            return None
        if timeout is not None and time.time() - start > timeout:
            return None
        time.sleep(1.5)


def battery_percent() -> Optional[int]:
    """Battery of the phone running Shahtool (Termux only)."""
    if is_termux() and shutil.which("termux-battery-status"):
        _, out = run(["termux-battery-status"], 8)
        try:
            return int(json.loads(out)["percentage"])
        except (ValueError, KeyError, TypeError):
            return None
    return None


def free_bytes(path: str) -> int:
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    return shutil.disk_usage(str(p)).free


# --------------------------------------------------------------------------
# ROM discovery
# --------------------------------------------------------------------------
@dataclass
class RomItem:
    path: str
    name: str
    kind: str  # "archive" | "folder"
    size: int = 0
    mtime: float = 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["size_h"] = human_size(self.size) if self.size else ""
        return d


def default_scan_roots() -> List[str]:
    roots = list(load_config().get("scan_dirs") or [])
    if os.path.isdir("/sdcard"):
        roots.append("/sdcard")
    else:
        home = Path.home()
        roots += [str(home / "Downloads"), str(home / "Desktop"),
                  str(home / "Documents"), str(DATA_DIR / "roms"), os.getcwd()]
    seen, out = set(), []
    for r in roots:
        real = os.path.realpath(r)
        if os.path.isdir(real) and real not in seen:
            seen.add(real)
            out.append(real)
    return out


def _looks_like_rom_dir(entries: list) -> bool:
    dirs = {e.name.lower() for e in entries if _is_dir(e)}
    files = [e.name.lower() for e in entries if not _is_dir(e)]
    return "images" in dirs and any(f.startswith("flash_all") for f in files)


def _is_dir(entry) -> bool:
    try:
        return entry.is_dir(follow_symlinks=False)
    except OSError:
        return False


def scan_roms(
    roots: Optional[Sequence[str]] = None,
    depth: Optional[int] = None,
    min_mb: Optional[int] = None,
    cancel: Optional[threading.Event] = None,
) -> List[RomItem]:
    """Find fastboot ROM archives and extracted ROM folders (depth limited)."""
    cfg = load_config()
    roots = list(roots) if roots else default_scan_roots()
    depth = cfg["scan_depth"] if depth is None else depth
    min_bytes = (cfg["min_rom_mb"] if min_mb is None else min_mb) * 1024 * 1024
    found: Dict[str, RomItem] = {}

    def walk(d: str, level: int) -> None:
        if cancel is not None and cancel.is_set():
            return
        try:
            entries = list(os.scandir(d))
        except OSError:
            return
        if _looks_like_rom_dir(entries):
            real = os.path.realpath(d)
            try:
                found.setdefault(real, RomItem(real, os.path.basename(real), "folder",
                                               0, os.stat(real).st_mtime))
            except OSError:
                pass
            return
        for e in entries:
            try:
                if _is_dir(e):
                    if level < depth and not e.name.startswith(".") and e.name != "Android":
                        walk(e.path, level + 1)
                elif e.is_file():
                    low = e.name.lower()
                    if low.endswith(ARCHIVE_EXTS) and not any(k in low for k in IGNORED_KEYWORDS):
                        st = e.stat()
                        if st.st_size >= min_bytes:
                            real = os.path.realpath(e.path)
                            found.setdefault(real, RomItem(real, e.name, "archive",
                                                           st.st_size, st.st_mtime))
            except OSError:
                continue

    for r in roots:
        walk(r, 0)
    return sorted(found.values(), key=lambda i: i.mtime, reverse=True)


# --------------------------------------------------------------------------
# Checksums
# --------------------------------------------------------------------------
def file_hash(path: str, algo: str = "sha256", progress: Optional[Progress] = None,
              cancel: Optional[threading.Event] = None) -> str:
    h = hashlib.new(algo)
    total = os.path.getsize(path)
    done = 0
    with open(path, "rb") as f:
        while True:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            chunk = f.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    return h.hexdigest()


def find_expected_checksum(archive: str) -> Optional[Tuple[str, str, str]]:
    """Look for <archive>.sha256 / .md5 next to the file. Returns (algo, hex, source)."""
    for suffix, algo, length in ((".sha256", "sha256", 64), (".sha256sum", "sha256", 64),
                                 (".md5", "md5", 32), (".md5sum", "md5", 32)):
        p = archive + suffix
        if os.path.isfile(p):
            try:
                token = Path(p).read_text(encoding="utf-8", errors="ignore").split()[0].lower()
            except IndexError:
                continue
            if re.fullmatch(rf"[0-9a-f]{{{length}}}", token):
                return algo, token, os.path.basename(p)
    return None


def filename_md5_tag(name: str) -> Optional[str]:
    """Xiaomi ROM file names usually contain the first 10 hex chars of the MD5."""
    for tok in re.findall(r"(?<![0-9A-Za-z])[0-9a-f]{10}(?![0-9A-Za-z])", name):
        if re.search(r"[a-f]", tok):
            return tok
    return None


def verify_archive(path: str, expected: Optional[str] = None,
                   progress: Optional[Progress] = None,
                   cancel: Optional[threading.Event] = None) -> dict:
    """Verify a file. Uses `expected` (md5/sha256 by length), a sidecar file,
    or the MD5 tag in the file name, in that order."""
    algo, source = "sha256", "none"
    if expected:
        expected = expected.strip().lower()
        algo = {32: "md5", 64: "sha256"}.get(len(expected), "")
        if not algo:
            raise ValueError("Expected hash must be 32 (md5) or 64 (sha256) hex characters")
        source = "you"
    else:
        side = find_expected_checksum(path)
        if side:
            algo, expected, source = side
        else:
            tag = filename_md5_tag(os.path.basename(path))
            if tag:
                algo, expected, source = "md5", tag, "file name tag"
    actual = file_hash(path, algo, progress, cancel)
    if not expected:
        return {"algo": algo, "actual": actual, "expected": None, "source": source, "ok": None}
    ok = actual.startswith(expected) if source == "file name tag" else actual == expected
    return {"algo": algo, "actual": actual, "expected": expected, "source": source, "ok": ok}


# --------------------------------------------------------------------------
# Extraction
# --------------------------------------------------------------------------
def _safe_join(base: str, name: str) -> str:
    dest = os.path.realpath(os.path.join(base, name))
    if dest != base and not dest.startswith(base + os.sep):
        raise ValueError(f"Unsafe path in archive: {name}")
    return dest


def _copy_stream(src, dst, step: Callable[[int], None], cancel) -> None:
    while True:
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        chunk = src.read(1 << 20)
        if not chunk:
            return
        dst.write(chunk)
        step(len(chunk))


def _extract_zip(src: str, dst: str, progress, cancel) -> None:
    with zipfile.ZipFile(src) as z:
        infos = z.infolist()
        total = sum(i.file_size for i in infos) or 1
        need = total * 1.05
        if free_bytes(dst) < need:
            raise RuntimeError(f"Not enough free space: need {human_size(need)}, "
                               f"have {human_size(free_bytes(dst))}")
        done = [0]

        def step(n):
            done[0] += n
            if progress:
                progress(done[0], total)

        for i in infos:
            target = _safe_join(dst, i.filename)
            if i.is_dir():
                os.makedirs(target, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with z.open(i) as s, open(target, "wb") as o:
                _copy_stream(s, o, step, cancel)
            mode = (i.external_attr >> 16) & 0o777
            if mode:
                os.chmod(target, mode | 0o600)


def _extract_tar(src: str, dst: str, progress, cancel) -> None:
    size = os.path.getsize(src) or 1
    if free_bytes(dst) < size * 2.2:
        raise RuntimeError(f"Not enough free space: need about {human_size(size * 2.2)}, "
                           f"have {human_size(free_bytes(dst))}")
    with open(src, "rb") as raw, tarfile.open(fileobj=raw, mode="r|*") as t:
        for m in t:
            if cancel is not None and cancel.is_set():
                raise Cancelled()
            target = _safe_join(dst, m.name)
            if m.isdir():
                os.makedirs(target, exist_ok=True)
            elif m.isfile():
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with t.extractfile(m) as s, open(target, "wb") as o:
                    _copy_stream(s, o, lambda n: progress and progress(min(raw.tell(), size), size),
                                 cancel)
                os.chmod(target, (m.mode & 0o777) | 0o600)
            # symlinks, hardlinks and device nodes are skipped on purpose (safety)
            if progress:
                progress(min(raw.tell(), size), size)


def _extract_external(src: str, dst: str, log: Log, cancel) -> None:
    tool = next((shutil.which(t) for t in ("7z", "7zz", "7za", "7zr") if shutil.which(t)), None)
    if tool:
        cmd = [tool, "x", "-y", f"-o{dst}", src]
    elif src.lower().endswith(".rar") and shutil.which("unrar"):
        cmd = [shutil.which("unrar"), "x", "-y", src, dst + os.sep]
    else:
        raise RuntimeError("7z/rar support missing. Install it: pkg install p7zip unrar "
                           "(Termux) or install 7-Zip (PC).")
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, errors="replace")
    last = ""
    for line in proc.stdout:
        last = line.strip() or last
        if cancel is not None and cancel.is_set():
            proc.terminate()
            raise Cancelled()
    if proc.wait() != 0:
        raise RuntimeError(f"Extraction failed: {last}")


def flatten_single_dirs(d: str) -> None:
    """Collapse folders that contain just one folder. (The original tool crashed
    when the inner folder had the same name as one of its children.)"""
    while True:
        for junk in JUNK_NAMES:
            p = os.path.join(d, junk)
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            elif os.path.exists(p):
                os.remove(p)
        items = os.listdir(d)
        if len(items) != 1 or not os.path.isdir(os.path.join(d, items[0])):
            return
        tmp = os.path.join(d, ".shahtool_flat_tmp")
        os.rename(os.path.join(d, items[0]), tmp)
        for child in os.listdir(tmp):
            shutil.move(os.path.join(tmp, child), os.path.join(d, child))
        os.rmdir(tmp)


def extract_root() -> Path:
    cfg = load_config()
    if cfg.get("extract_dir"):
        return Path(cfg["extract_dir"])
    if os.path.isdir("/sdcard"):
        return Path("/sdcard/Download/shahtool-rom")
    return DATA_DIR / "roms"


def archive_stem(name: str) -> str:
    low = name.lower()
    for ext in ARCHIVE_EXTS:
        if low.endswith(ext):
            name = name[: -len(ext)]
            break
    return re.sub(r"[^\w.\-]+", "_", name)[:80] or "rom"


def find_rom_root(base: str) -> str:
    base = str(base)
    fallback = None
    for root, dirs, files in os.walk(base):
        if root[len(base):].count(os.sep) > 3:
            dirs[:] = []
            continue
        low = [f.lower() for f in files]
        if "images" in (d.lower() for d in dirs) and any(f.startswith("flash_all") for f in low):
            return root
        if fallback is None and any(f.startswith("flash") and f.endswith((".sh", ".bat")) for f in low):
            fallback = root
    return fallback or base


def prepare_rom(path: str, log: Log = print, progress: Optional[Progress] = None,
                cancel: Optional[threading.Event] = None, force: bool = False) -> str:
    """Return the directory that contains the flash scripts, extracting if needed."""
    if os.path.isdir(path):
        return find_rom_root(path)
    low = path.lower()
    if not low.endswith(ARCHIVE_EXTS):
        raise ValueError("Unsupported file. Use .tgz, .tar.gz, .zip, .7z or .rar")
    dst = extract_root() / archive_stem(os.path.basename(path))
    if (dst / MARKER).exists() and not force:
        log(f"Already extracted, reusing {dst}")
        return find_rom_root(str(dst))
    shutil.rmtree(dst, ignore_errors=True)
    os.makedirs(dst, exist_ok=True)
    real = os.path.realpath(dst)
    log(f"Extracting to {dst}")
    try:
        if low.endswith(".zip"):
            _extract_zip(path, real, progress, cancel)
        elif low.endswith((".tgz", ".tar.gz")):
            _extract_tar(path, real, progress, cancel)
        else:
            _extract_external(path, real, log, cancel)
        flatten_single_dirs(real)
    except BaseException:
        shutil.rmtree(dst, ignore_errors=True)  # never leave a half extracted ROM
        raise
    (dst / MARKER).write_text(os.path.basename(path), encoding="utf-8")
    log("Extraction finished")
    return find_rom_root(str(dst))


def dir_size(path: str) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def clean_extracted() -> int:
    """Delete ROMs that Shahtool itself extracted. Returns bytes freed."""
    freed = 0
    root = extract_root()
    if root.is_dir():
        for d in root.iterdir():
            if d.is_dir() and (d / MARKER).exists():
                freed += dir_size(str(d))
                shutil.rmtree(d, ignore_errors=True)
    return freed


# --------------------------------------------------------------------------
# Flash scripts
# --------------------------------------------------------------------------
@dataclass
class FlashScript:
    name: str
    path: str
    kind: str  # clean | keep_data | lock | other
    title: str
    wipes_data: bool
    locks_bootloader: bool

    def to_dict(self) -> dict:
        return asdict(self)


def _read_text(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def classify_script(name: str, text: str) -> FlashScript:
    low, ctext = name.lower(), text.lower()
    locks = bool(re.search(r"(^|[_\-.])lock", low)) or \
        bool(re.search(r"\b(oem|flashing)\s+lock\b", ctext))
    wipes = bool(re.search(r"\berase\s+(userdata|metadata)\b|\s-w(\s|$)", ctext))
    if not wipes and not re.search(r"except_storage|dirty|keep|save", low):
        wipes = "flash_all" in low and "except" not in low
    if locks:
        kind, title = "lock", "Flash everything and re-lock the bootloader"
    elif wipes:
        kind, title = "clean", "Clean flash: erases all data"
    elif re.search(r"except_storage|dirty|keep|save", low):
        kind, title = "keep_data", "Dirty flash: keeps your data"
    else:
        kind, title = "other", name
    return FlashScript(name, "", kind, title, wipes, locks)


def discover_scripts(rom_dir: str) -> List[FlashScript]:
    out: List[FlashScript] = []
    ext = ".bat" if is_windows() else ".sh"
    try:
        entries = sorted(os.scandir(rom_dir), key=lambda e: e.name.lower())
    except OSError:
        return out
    for e in entries:
        low = e.name.lower()
        if not e.is_file() or e.name.startswith(".") or low.startswith("termux_"):
            continue
        if not low.endswith(ext):
            continue
        if ("mac" in low or "darwin" in low) and "linux" not in low:
            continue
        text = _read_text(e.path)
        if "fastboot" not in text.lower():
            continue
        s = classify_script(e.name, text)
        s.path = e.path
        out.append(s)
    return out


_CMD_RE = re.compile(
    r"\b(flash|erase|reboot|set_active|oem|getvar|format|delete-logical-partition|"
    r"create-logical-partition|resize-logical-partition|snapshot-update|flashing|boot)\b|\s-w\b")


def script_commands(path: str) -> List[str]:
    """The fastboot commands a script would run (a safe preview, nothing is executed)."""
    cmds = []
    for line in _read_text(path).replace("\r", "").splitlines():
        s = line.strip()
        if not s or s.startswith(("#", "echo", "if", "then", "fi", "[")) or s.startswith("fastboot="):
            continue
        if "fastboot" in s.lower() and _CMD_RE.search(s):
            cmds.append(s)
    return cmds


def _write_wrapper(path: str, real: str) -> None:
    sh = shutil.which("sh") or "/bin/sh"
    Path(path).write_text(f'#!{sh}\nexec "{real}" "$@"\n', encoding="utf-8")
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def link_fastboot(rom_dir: str, real: str, replace_bundled: Optional[bool] = None) -> None:
    """Make every `fastboot` the ROM scripts might use point at the system one.
    On Android the bundled x86 binaries cannot run, so they are always replaced."""
    if replace_bundled is None:
        replace_bundled = is_android()
    for root, dirs, files in os.walk(rom_dir):
        dirs[:] = [d for d in dirs if d.lower() != "images"]
        if "fastboot" in files and replace_bundled:
            p = os.path.join(root, "fastboot")
            os.remove(p)
            _write_wrapper(p, real)
    wanted = os.path.join(rom_dir, "bin", "linux", "fastboot")
    if not os.path.exists(wanted) and "bin/linux/fastboot" in "".join(
            _read_text(s.path) for s in discover_scripts(rom_dir)):
        os.makedirs(os.path.dirname(wanted), exist_ok=True)
        _write_wrapper(wanted, real)


def _normalize_script(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    for old in ("$PWD/fastboot", "./linux/fastboot", "./bin/fastboot", "./fastboot", "fastboot.exe"):
        text = text.replace(old, "fastboot")
    return text


def new_log_path(prefix: str) -> Path:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    return LOG_DIR / f"{prefix}_{datetime.now():%Y%m%d_%H%M%S}.log"


def run_flash(rom_dir: str, script: FlashScript, log: Log,
              cancel: Optional[threading.Event] = None,
              serial: Optional[str] = None) -> Tuple[int, str]:
    """Run a ROM flash script and stream its output. Returns (exit_code, log_path).
    The ROM's own script is never edited: a cleaned copy is run instead."""
    fb = find_tool("fastboot")
    if not fb:
        raise RuntimeError("fastboot not found. Install platform-tools first.")
    log_path = new_log_path("flash")
    logf = open(log_path, "w", encoding="utf-8")

    def emit(line: str) -> None:
        logf.write(line + "\n")
        logf.flush()
        log(line)

    shim = None
    prepared = os.path.join(rom_dir, RUN_COPY)
    try:
        env = os.environ.copy()
        env["PYTHONUNBUFFERED"] = "1"
        if serial:
            env["ANDROID_SERIAL"] = serial
        if is_windows():
            cmd = ["cmd", "/c", script.path]
        else:
            link_fastboot(rom_dir, fb)
            shim = tempfile.mkdtemp(prefix="shahtool-")
            _write_wrapper(os.path.join(shim, "fastboot"), fb)
            env["PATH"] = shim + os.pathsep + env.get("PATH", "")
            Path(prepared).write_text(_normalize_script(_read_text(script.path)), encoding="utf-8")
            os.chmod(prepared, 0o755)
            cmd = [shutil.which("bash") or "sh", prepared]
        emit(f"Started {datetime.now():%Y-%m-%d %H:%M:%S}: {script.name}")
        proc = subprocess.Popen(cmd, cwd=rom_dir, env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, errors="replace", bufsize=1)
        done = threading.Event()

        def watcher():
            while not done.is_set():
                if cancel is not None and cancel.wait(0.5):
                    proc.terminate()
                    return

        threading.Thread(target=watcher, daemon=True).start()
        for line in proc.stdout:
            emit(line.rstrip("\n"))
        proc.stdout.close()
        rc = proc.wait()
        done.set()
        emit(f"Finished with exit code {rc}")
        return rc, str(log_path)
    finally:
        logf.close()
        if shim:
            shutil.rmtree(shim, ignore_errors=True)
        try:
            os.remove(prepared)
        except OSError:
            pass


# --------------------------------------------------------------------------
# Quick device actions
# --------------------------------------------------------------------------
ACTIONS = {
    "reboot_system": ["reboot"],
    "reboot_bootloader": ["reboot", "bootloader"],
    "reboot_recovery": ["reboot", "recovery"],
    "reboot_fastbootd": ["reboot", "fastboot"],
    "set_slot_a": ["set_active", "a"],
    "set_slot_b": ["set_active", "b"],
    "wipe_data": ["-w"],
    "getvar_all": ["getvar", "all"],
}
DANGEROUS_ACTIONS = {"wipe_data"}


def fastboot_run(args: Sequence[str], serial: Optional[str] = None,
                 timeout: int = 120) -> Tuple[int, str]:
    fb = find_tool("fastboot")
    if not fb:
        return 127, "fastboot not found"
    return run([fb] + (["-s", serial] if serial else []) + list(args), timeout)


def do_action(name: str, serial: Optional[str] = None) -> Tuple[int, str]:
    if name not in ACTIONS:
        raise ValueError(f"Unknown action: {name}")
    return fastboot_run(ACTIONS[name], serial, timeout=300 if name == "wipe_data" else 60)


def flash_partition(partition: str, image: str, serial: Optional[str] = None) -> Tuple[int, str]:
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,40}", partition):
        raise ValueError("Invalid partition name")
    if not os.path.isfile(image):
        raise ValueError(f"Image not found: {image}")
    return fastboot_run(["flash", partition, image], serial, timeout=900)


# --------------------------------------------------------------------------
# Safety checks
# --------------------------------------------------------------------------
@dataclass
class Check:
    name: str
    status: str  # ok | warn | fail | info
    detail: str

    def to_dict(self) -> dict:
        return asdict(self)


def codename_hint(codename: Optional[str], *texts: str) -> Optional[bool]:
    if not codename:
        return None
    pat = re.compile(rf"(?<![a-z0-9]){re.escape(codename.lower())}(?![a-z0-9])")
    return any(pat.search((t or "").lower()) for t in texts)


def preflight(script: Optional[FlashScript] = None, rom_dir: str = "", archive_name: str = "",
              serial: Optional[str] = None) -> List[Check]:
    checks: List[Check] = []
    fb = find_tool("fastboot")
    if not fb:
        return [Check("fastboot", "fail", "fastboot is not installed")]
    checks.append(Check("fastboot", "ok", fb))
    devs = fastboot_devices()
    if not devs:
        checks.append(Check("Device", "fail", "No phone in fastboot mode"))
    else:
        serial = serial if serial in devs else devs[0]
        checks.append(Check("Device", "ok" if len(devs) == 1 else "warn",
                            serial if len(devs) == 1 else f"{len(devs)} devices connected, using {serial}"))
        info = device_info(serial)
        unlocked = (info.get("unlocked") or "").lower()
        if unlocked in ("yes", "true"):
            checks.append(Check("Bootloader", "ok", "Unlocked"))
        elif unlocked in ("no", "false"):
            checks.append(Check("Bootloader", "fail", "Locked. Unlock it first (Mi Unlock)"))
        else:
            checks.append(Check("Bootloader", "warn", "Could not read lock state"))
        if info.get("anti"):
            checks.append(Check("Anti-rollback", "info",
                                f"Device level {info['anti']}. Never flash an older ROM."))
        if info.get("product"):
            script_text = _read_text(script.path) if script else ""
            hit = codename_hint(info["product"], archive_name, os.path.basename(rom_dir), script_text)
            if hit:
                checks.append(Check("ROM matches device", "ok", f"Found codename '{info['product']}'"))
            else:
                checks.append(Check("ROM matches device", "warn",
                                    f"Codename '{info['product']}' not found in the ROM name. "
                                    "Make sure this ROM is for your phone."))
    if is_termux():
        pct = battery_percent()
        if pct is None:
            checks.append(Check("Battery", "info", "Unknown (install Termux:API)"))
        elif pct < 20:
            checks.append(Check("Battery", "fail", f"{pct}%. Charge this phone first"))
        elif pct < 50:
            checks.append(Check("Battery", "warn", f"{pct}%. Keep it charging"))
        else:
            checks.append(Check("Battery", "ok", f"{pct}%"))
    if script:
        if script.locks_bootloader:
            checks.append(Check("Script", "warn", "Re-locks the bootloader. A wrong or modified ROM "
                                                  "can brick the phone."))
        elif script.wipes_data:
            checks.append(Check("Script", "warn", "All data on the phone will be erased."))
        else:
            checks.append(Check("Script", "ok", "Keeps your data"))
    return checks


def has_failures(checks: Sequence[Check]) -> bool:
    return any(c.status == "fail" for c in checks)


# --------------------------------------------------------------------------
# Updates
# --------------------------------------------------------------------------
def _ver(v: str) -> Tuple[int, ...]:
    return tuple(int(x) for x in re.findall(r"\d+", v)[:3])


def check_update(timeout: float = 5) -> Optional[str]:
    """Return the newest version string if newer than this one, else None."""
    try:
        with urllib.request.urlopen(RAW_VERSION_URL, timeout=timeout) as r:
            latest = r.read().decode("utf-8", "ignore").strip()
        return latest if _ver(latest) > _ver(__version__) else None
    except Exception:
        return None


def self_update() -> Tuple[int, str]:
    app = Path(__file__).resolve().parent.parent
    if not (app / ".git").exists():
        return 1, "Not a git install. Update with: pip install -U git+https://github.com/" \
                  "YOUR_GITHUB_USERNAME/Shahtool.git"
    return run(["git", "-C", str(app), "pull", "--ff-only"], 60)
