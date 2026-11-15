"""Shahtool web GUI: a small local HTTP server (standard library only).

Works on a PC and inside Termux (open the printed link in the phone browser).
Every API call needs a random token that is part of the printed link, and the
Host header is checked, so other websites cannot drive your flasher.
"""
from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import parse_qs, urlparse

from . import __version__, core

WEB_DIR = Path(__file__).resolve().parent / "web"


class Job:
    def __init__(self, kind: str):
        self.id = uuid.uuid4().hex[:8]
        self.kind = kind
        self.lines: list = []
        self.state = "running"  # running | done | error | cancelled
        self.progress: Optional[float] = None
        self.result: dict = {}
        self.cancel = threading.Event()
        self._lock = threading.Lock()

    def log(self, msg: str) -> None:
        with self._lock:
            self.lines.append(msg)
            del self.lines[:-3000]  # keep memory bounded


class JobManager:
    def __init__(self):
        self.current: Optional[Job] = None
        self._lock = threading.Lock()

    def start(self, kind: str, fn: Callable[[Job], dict]) -> Job:
        with self._lock:
            if self.current and self.current.state == "running":
                raise RuntimeError("Another job is already running")
            job = Job(kind)
            self.current = job

        def runner():
            try:
                job.result = fn(job) or {}
                job.state = "cancelled" if job.cancel.is_set() else "done"
            except core.Cancelled:
                job.log("Cancelled")
                job.state = "cancelled"
            except Exception as exc:  # shown to the user in the log
                job.log(f"Error: {exc}")
                job.result = {"error": str(exc)}
                job.state = "error"

        threading.Thread(target=runner, daemon=True).start()
        return job


JOBS = JobManager()


def _progress(job: Job):
    def cb(done: int, total: int) -> None:
        job.progress = done / total if total else None
    return cb


# ------------------------------------------------------------------ job bodies
def job_prepare(body: dict):
    def fn(job: Job) -> dict:
        path = body["path"]
        if os.path.isfile(path) and body.get("verify"):
            job.log("Verifying checksum...")
            res = core.verify_archive(path, progress=_progress(job), cancel=job.cancel)
            if res["ok"] is False:
                raise RuntimeError(f"{res['algo']} mismatch ({res['source']}). File may be corrupted.")
            job.log("Checksum OK" if res["ok"] else f"{res['algo']}: {res['actual']} (nothing to compare)")
            job.progress = None
        rom_dir = core.prepare_rom(path, job.log, _progress(job), job.cancel)
        scripts = [s.to_dict() for s in core.discover_scripts(rom_dir)]
        job.log(f"Found {len(scripts)} flash script(s)")
        return {"rom_dir": rom_dir, "scripts": scripts}
    return fn


def _find_script(rom_dir: str, name: str) -> core.FlashScript:
    if os.path.basename(name) != name:
        raise ValueError("Invalid script name")
    for s in core.discover_scripts(rom_dir):
        if s.name == name:
            return s
    raise ValueError("Script not found in ROM folder")


def job_flash(body: dict):
    script = _find_script(body["dir"], body["name"])
    word = "LOCK" if script.locks_bootloader else "FLASH"
    if body.get("confirm") != word:
        raise ValueError(f"Type {word} to confirm")

    def fn(job: Job) -> dict:
        serial = body.get("serial") or None
        job.log("Waiting for device...")
        serial = core.wait_for_device(cancel=job.cancel) if not serial else serial
        if not serial:
            raise core.Cancelled()
        checks = core.preflight(script, body["dir"], body.get("archive", ""), serial)
        if core.has_failures(checks):
            raise RuntimeError("; ".join(c.detail for c in checks if c.status == "fail"))
        rc, log_path = core.run_flash(body["dir"], script, job.log, job.cancel, serial)
        if rc != 0 and not job.cancel.is_set():
            raise RuntimeError(f"Flash failed with exit code {rc}. Log: {log_path}")
        return {"exit_code": rc, "log": log_path}
    return fn


def job_verify(body: dict):
    def fn(job: Job) -> dict:
        res = core.verify_archive(body["path"], body.get("expected") or None,
                                  _progress(job), job.cancel)
        job.log(f"{res['algo']}: {res['actual']}")
        if res["ok"] is None:
            job.log("Nothing to compare with. Paste the expected hash to check it.")
        else:
            job.log("MATCH: file is intact" if res["ok"] else "MISMATCH: file is corrupted")
        job.progress = None
        return res
    return fn


# ------------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "Shahtool/" + __version__
    token = ""
    allowed_hosts: set = set()
    enforce_host = True

    def log_message(self, *args):  # keep the terminal quiet
        pass

    # helpers
    def _send(self, code: int, payload, ctype: str = "application/json") -> None:
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self) -> bool:
        return not self.enforce_host or self.headers.get("Host", "") in self.allowed_hosts

    def _authed(self) -> bool:
        return secrets.compare_digest(self.headers.get("X-Token", ""), self.token)

    # routes
    def do_GET(self):
        url = urlparse(self.path)
        if not self._host_ok():
            return self._send(403, {"error": "bad host"})
        if url.path == "/":
            tok = parse_qs(url.query).get("t", [""])[0]
            if not secrets.compare_digest(tok, self.token):
                return self._send(403, b"Open the full link printed by Shahtool (it contains ?t=...).",
                                  "text/plain")
            return self._send(200, (WEB_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        if url.path == "/api/state":
            job = JOBS.current
            return self._send(200, {
                "version": __version__,
                "platform": "termux" if core.is_termux() else os.name,
                "fastboot": core.find_tool("fastboot"),
                "adb": core.find_tool("adb"),
                "devices": core.fastboot_devices(),
                "adb_devices": core.adb_devices(),
                "job": {"id": job.id, "kind": job.kind, "state": job.state} if job else None,
            })
        if url.path == "/api/job":
            job = JOBS.current
            if not job:
                return self._send(200, {"job": None})
            since = int(parse_qs(url.query).get("since", ["0"])[0])
            with job._lock:
                lines = job.lines[since:]
                total = len(job.lines)
            return self._send(200, {"id": job.id, "kind": job.kind, "state": job.state,
                                    "progress": job.progress, "lines": lines, "next": total,
                                    "result": job.result if job.state != "running" else {}})
        self._send(404, {"error": "not found"})

    def do_POST(self):
        if not self._host_ok():
            return self._send(403, {"error": "bad host"})
        if not self._authed():
            return self._send(401, {"error": "unauthorized"})
        try:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"{}")
            self._send(200, self.route(urlparse(self.path).path, body))
        except (ValueError, KeyError, RuntimeError) as exc:
            self._send(400, {"error": str(exc) or exc.__class__.__name__})
        except Exception as exc:
            self._send(500, {"error": f"{exc.__class__.__name__}: {exc}"})

    def route(self, path: str, body: dict) -> dict:
        if path == "/api/scan":
            roots = [body["path"]] if body.get("path") else None
            if roots and not os.path.exists(roots[0]):
                raise ValueError("Folder not found")
            return {"roms": [r.to_dict() for r in core.scan_roms(roots)]}
        if path == "/api/scripts":
            rom_dir = core.find_rom_root(body["dir"])
            return {"rom_dir": rom_dir, "scripts": [s.to_dict() for s in core.discover_scripts(rom_dir)]}
        if path == "/api/preview":
            return {"commands": core.script_commands(_find_script(body["dir"], body["name"]).path)}
        if path == "/api/device":
            return {"info": core.device_info(body.get("serial") or None)}
        if path == "/api/preflight":
            script = _find_script(body["dir"], body["name"]) if body.get("name") else None
            checks = core.preflight(script, body.get("dir", ""), body.get("archive", ""),
                                    body.get("serial") or None)
            return {"checks": [c.to_dict() for c in checks], "blocked": core.has_failures(checks)}
        if path == "/api/prepare":
            return {"job": JOBS.start("prepare", job_prepare(body)).id}
        if path == "/api/flash":
            return {"job": JOBS.start("flash", job_flash(body)).id}
        if path == "/api/verify":
            return {"job": JOBS.start("verify", job_verify(body)).id}
        if path == "/api/cancel":
            if JOBS.current:
                JOBS.current.cancel.set()
            return {"ok": True}
        if path == "/api/action":
            name = body["name"]
            if name in core.DANGEROUS_ACTIONS and body.get("confirm") != "WIPE":
                raise ValueError("Type WIPE to confirm")
            rc, out = core.do_action(name, body.get("serial") or None)
            return {"code": rc, "output": out.strip()}
        if path == "/api/partition":
            rc, out = core.flash_partition(body["partition"], body["image"], body.get("serial") or None)
            return {"code": rc, "output": out.strip()}
        if path == "/api/clean":
            return {"freed": core.human_size(core.clean_extracted())}
        if path == "/api/update":
            latest = core.check_update()
            if not latest:
                return {"message": f"You are on the latest version ({__version__})."}
            rc, out = core.self_update()
            return {"message": f"New version {latest}. " + out.strip(), "code": rc}
        raise ValueError("Unknown endpoint")


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    token = secrets.token_urlsafe(18)
    Handler.token = token
    local = host in ("127.0.0.1", "localhost", "::1")
    Handler.enforce_host = local
    Handler.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}
    try:
        httpd = ThreadingHTTPServer((host, port), Handler)
    except OSError as exc:
        raise SystemExit(f"Cannot start GUI on {host}:{port}: {exc}")
    url = f"http://{'127.0.0.1' if local else host}:{port}/?t={token}"
    print(f"\n  Shahtool GUI is running. Open this link:\n\n    {url}\n")
    if not local:
        print("  WARNING: the GUI is reachable from your network. Only do this on a network you trust.\n")
    print("  Press Ctrl+C to stop.\n")
    if open_browser:
        opener = shutil.which("termux-open-url")
        try:
            if opener:
                subprocess.Popen([opener, url])
            else:
                webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  GUI stopped.")
    finally:
        httpd.server_close()
