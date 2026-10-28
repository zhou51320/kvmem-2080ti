"""Check that the frontend releases its shared log handles at shutdown, so Windows can remove the log's
temporary directory.  Without the handle close, the launcher's final cleanup raises WinError 32 and exits 1.

Windows only: other platforms unlink an open file without error, so the control cannot reproduce there.
"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

if sys.platform != "win32":
    print("SKIP: the leak only blocks removal on Windows")
    sys.exit(0)

sys.path.insert(0, str(ROOT / "backends" / "polystrata"))

from serve import images as images_api   # noqa: E402
from serve import server as srv          # noqa: E402

VCFG = {"exe": "vision.exe", "mmproj": "vision-mmproj.gguf", "model": "vision-model.gguf"}
ICFG = {"exe": "sd-cli.exe", "diffusion_model": "d.safetensors", "vae": "v.safetensors", "llm": "t.safetensors"}


def check_negative_control():
    """Pre-fix lifetime: the service keeps both objects alive, so their handles are still open at removal."""
    try:
        with tempfile.TemporaryDirectory(prefix="kvmem-log-close-") as directory:
            held = (srv.Vision(dict(VCFG), log=open(Path(directory) / "native.log", "a", encoding="utf-8"), lazy=True),
                    images_api.ImageGen(dict(ICFG), log=open(Path(directory) / "images.log", "a", encoding="utf-8")))
    except PermissionError as e:
        print(f"[control] reproduced the leak: {type(e).__name__} {e.winerror}: {e.filename}")
        return True
    print(f"[control] FAIL: the temporary directory was removable (the leak did not reproduce) {held!r}")
    return False


def check_fix():
    class Stub:
        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

        def shutdown(self):
            self.closed = True

    with tempfile.TemporaryDirectory(prefix="kvmem-log-close-") as directory:
        directory = Path(directory)
        vision_log = open(directory / "native.log", "a", encoding="utf-8")
        images_log = open(directory / "images.log", "a", encoding="utf-8")
        logs = {"vision": vision_log, "images": images_log}
        vision = srv.Vision(dict(VCFG), log=vision_log, lazy=True)
        gen = images_api.ImageGen(dict(ICFG), log=images_log)
        vision.unload()                            # unload() must keep the handle: the encoder restarts with it
        if logs["vision"].closed:
            print("[fix] FAIL: unload() closed the log (the encoder could not restart)")
            return False
        svc = type("Svc", (), {})()
        svc.engine, svc.vision, svc.images = Stub(), vision, gen
        httpd = Stub()
        srv.shutdown(svc, httpd, None)
        for name, handle in logs.items():
            if not handle.closed:
                print(f"[fix] FAIL: the {name} log handle is still open after shutdown()")
                return False
        print(f"[fix] shutdown() released the handles: engine={svc.engine.closed} "
              f"httpd={httpd.closed} vision={logs['vision'].closed} images={logs['images'].closed}")
        tmp = directory
    if tmp.exists():
        print("[fix] FAIL: the temporary directory survived")
        return False
    print("[fix] the temporary directory was removed: no WinError 32")
    return True


if __name__ == "__main__":
    ok = check_negative_control() & check_fix()
    print("PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)
