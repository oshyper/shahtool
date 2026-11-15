import io
import json
import os
import stat
import sys
import tarfile
import tempfile
import threading
import time
import unittest
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ["SHAHTOOL_HOME"] = tempfile.mkdtemp(prefix="shahtool-test-home-")

from shahtool import core  # noqa: E402

FLASH_ALL = """#!/bin/bash
fastboot=bin/linux/fastboot
if [ ! -f "$fastboot" ]; then
  echo "missing"; exit 1
fi
$fastboot getvar product 2>&1 | grep violet
$fastboot erase userdata
$fastboot flash boot images/boot.img
$fastboot reboot
"""
KEEP = FLASH_ALL.replace("$fastboot erase userdata\n", "")
LOCK = FLASH_ALL + "$fastboot oem lock\n"


def make_rom_files(prefix=""):
    return {
        prefix + "flash_all.sh": FLASH_ALL.replace("\n", "\r\n"),
        prefix + "flash_all_except_storage.sh": KEEP,
        prefix + "flash_all_lock.sh": LOCK,
        prefix + "flash_all.bat": "fastboot.exe flash boot images\\boot.img\n",
        prefix + "images/boot.img": "x" * 100,
    }


def write_zip(path, files):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)


class ScriptTests(unittest.TestCase):
    def test_classification(self):
        a = core.classify_script("flash_all.sh", FLASH_ALL)
        b = core.classify_script("flash_all_except_storage.sh", KEEP)
        c = core.classify_script("flash_all_lock.sh", LOCK)
        self.assertEqual((a.kind, a.wipes_data), ("clean", True))
        self.assertEqual(b.kind, "keep_data")
        self.assertEqual((c.kind, c.locks_bootloader), ("lock", True))

    def test_unlock_is_not_lock(self):
        s = core.classify_script("flash_all.sh", "fastboot flashing unlock\nfastboot erase userdata")
        self.assertFalse(s.locks_bootloader)

    def test_preview_commands(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "s.sh")
        Path(p).write_text(FLASH_ALL)
        cmds = core.script_commands(p)
        self.assertEqual(len(cmds), 4)
        self.assertTrue(all("fastboot" in c for c in cmds))

    def test_md5_tag(self):
        self.assertEqual(core.filename_md5_tag("violet_global_images_V11.0.8.0_fd8d1a3a0e_9.0.tgz"),
                         "fd8d1a3a0e")
        self.assertIsNone(core.filename_md5_tag("rom_2019112100_a.tgz"))

    def test_codename_hint(self):
        self.assertTrue(core.codename_hint("violet", "miui_VIOLET_global.tgz"))
        self.assertFalse(core.codename_hint("violet", "miui_ginkgo_global.tgz"))
        self.assertIsNone(core.codename_hint(None, "x"))


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        cfg = core.load_config()
        cfg["extract_dir"] = os.path.join(self.tmp, "out")
        core.save_config(cfg)

    def test_zip_with_top_folder_and_same_name_child(self):
        # old tool crashed when inner folder had the same name as one of its children
        files = make_rom_files("rom/")
        files["rom/rom/readme.txt"] = "hi"
        z = os.path.join(self.tmp, "violet_rom.zip")
        write_zip(z, files)
        rom_dir = core.prepare_rom(z, log=lambda m: None)
        self.assertTrue(os.path.isfile(os.path.join(rom_dir, "flash_all.sh")))
        self.assertTrue(os.path.isfile(os.path.join(rom_dir, "rom", "readme.txt")))

    def test_tar_without_top_folder_keeps_files(self):
        # old tool used --strip-components=1 and lost the first path component
        t = os.path.join(self.tmp, "flat.tgz")
        with tarfile.open(t, "w:gz") as tf:
            for name, data in make_rom_files().items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = 0o755
                tf.addfile(info, io.BytesIO(data.encode()))
        rom_dir = core.prepare_rom(t, log=lambda m: None)
        self.assertTrue(os.path.isfile(os.path.join(rom_dir, "flash_all.sh")))
        self.assertTrue(os.path.isfile(os.path.join(rom_dir, "images", "boot.img")))

    def test_path_traversal_blocked(self):
        z = os.path.join(self.tmp, "evil.zip")
        write_zip(z, {"../evil.txt": "x", "ok.txt": "y"})
        with self.assertRaises(ValueError):
            core.prepare_rom(z, log=lambda m: None)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "evil.txt")))

    def test_reuse_and_clean(self):
        z = os.path.join(self.tmp, "again.zip")
        write_zip(z, make_rom_files("r/"))
        msgs = []
        core.prepare_rom(z, log=msgs.append)
        core.prepare_rom(z, log=msgs.append)
        self.assertTrue(any("reusing" in m for m in msgs))
        self.assertGreater(core.clean_extracted(), 0)

    def test_verify(self):
        f = os.path.join(self.tmp, "a.bin")
        Path(f).write_bytes(b"hello")
        import hashlib
        good = hashlib.sha256(b"hello").hexdigest()
        self.assertTrue(core.verify_archive(f, good)["ok"])
        self.assertFalse(core.verify_archive(f, "0" * 64)["ok"])

    def test_scan_finds_archive_and_folder(self):
        big = os.path.join(self.tmp, "scan", "violet_images.zip")
        os.makedirs(os.path.dirname(big))
        write_zip(big, {"a": "x"})
        folder = os.path.join(self.tmp, "scan", "extracted")
        for name, data in make_rom_files().items():
            p = os.path.join(folder, name)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            Path(p).write_text(data)
        found = core.scan_roms([os.path.join(self.tmp, "scan")], min_mb=0)
        self.assertEqual({r.kind for r in found}, {"archive", "folder"})


class FlashRunTests(unittest.TestCase):
    """Runs a real ROM script against a fake fastboot to prove the whole path works."""

    @unittest.skipIf(os.name == "nt", "bash only")
    def test_run_flash_with_fake_fastboot(self):
        tmp = tempfile.mkdtemp()
        fakebin = os.path.join(tmp, "bin")
        os.makedirs(fakebin)
        fake = os.path.join(fakebin, "fastboot")
        Path(fake).write_text('#!/bin/sh\necho "FAKE fastboot $@"\n')
        os.chmod(fake, 0o755)
        old = os.environ["PATH"]
        os.environ["PATH"] = fakebin + os.pathsep + old
        try:
            rom = os.path.join(tmp, "rom")
            for name, data in make_rom_files().items():
                p = os.path.join(rom, name)
                os.makedirs(os.path.dirname(p), exist_ok=True)
                Path(p).write_text(data)
            script = core.discover_scripts(rom)
            self.assertEqual({s.name for s in script},
                             {"flash_all.sh", "flash_all_except_storage.sh", "flash_all_lock.sh"})
            s = next(x for x in script if x.name == "flash_all.sh")
            lines = []
            rc, logp = core.run_flash(rom, s, lines.append)
            self.assertEqual(rc, 0, lines)
            self.assertTrue(any("FAKE fastboot flash boot" in l for l in lines))
            self.assertTrue(os.path.isfile(logp))
            self.assertFalse(os.path.exists(os.path.join(rom, core.RUN_COPY)))
            # original script must be untouched (still CRLF)
            self.assertIn("\r\n", Path(s.path).read_bytes().decode())
        finally:
            os.environ["PATH"] = old


class GuiTests(unittest.TestCase):
    def test_token_and_api(self):
        from http.server import ThreadingHTTPServer
        from shahtool import gui
        gui.Handler.token = "tok123"
        gui.Handler.enforce_host = True
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), gui.Handler)
        port = httpd.server_address[1]
        gui.Handler.allowed_hosts = {f"127.0.0.1:{port}"}
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        base = f"http://127.0.0.1:{port}"

        def get(path, token=None, host=None):
            req = urllib.request.Request(base + path)
            if token:
                req.add_header("X-Token", token)
            if host:
                req.add_header("Host", host)
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, r.read()
            except urllib.error.HTTPError as e:
                return e.code, e.read()

        try:
            self.assertEqual(get("/")[0], 403)
            code, body = get("/?t=tok123")
            self.assertEqual(code, 200)
            self.assertIn(b"Shahtool", body)
            self.assertEqual(get("/api/state")[0], 401)
            code, body = get("/api/state", "tok123")
            self.assertEqual(code, 200)
            self.assertIn("version", json.loads(body))
            self.assertEqual(get("/api/state", "tok123", host="evil.example.com")[0], 403)
            req = urllib.request.Request(base + "/api/scan", data=b'{"path":"/nonexistent-x"}',
                                         headers={"X-Token": "tok123"}, method="POST")
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req, timeout=5)
            self.assertEqual(cm.exception.code, 400)
        finally:
            httpd.shutdown()


if __name__ == "__main__":
    unittest.main()
