"""Build a deterministic, runtime-only ZIP without deleting any source files."""

import hashlib
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
FILES = [
    "main.py",
    "nte.py",
    "nte_auth.py",
    "api.py",
    "auth.py",
    "login_api.py",
    "models.py",
    "store.py",
    "service.py",
    "render.py",
    "ui.py",
    "metadata.yaml",
    "logo.png",
    "requirements.txt",
    "README.md",
    "LICENSE",
    "assets/MemoCJK-Regular.otf",
    "assets/OFL.txt",
    "assets/MemoSymbols.ttf",
    "assets/MemoThai.ttf",
    "assets/notosans-OFL.txt",
    "assets/notosansthai-OFL.txt",
    "assets/icons/genshin.jpg",
    "assets/icons/starrail.jpg",
    "assets/icons/zzz.jpg",
    "assets/icons/nte.jpg",
    "assets/icons/sources.json",
    "licenses/GPL-3.0.txt",
    "licenses/ATTRIBUTION.txt",
]


def build():
    output = ROOT.parent / "astrbot_plugin_daily_reminder-v0.5.6.zip"
    for name in FILES:
        if not (ROOT / name).is_file():
            raise RuntimeError(f"Missing required file: {name}")
    with ZipFile(output, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(FILES):
            info = ZipInfo(f"{ROOT.name}/{name}", date_time=(2026, 10, 3, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            archive.writestr(info, (ROOT / name).read_bytes(), compresslevel=9)
    with ZipFile(output) as archive:
        if archive.testzip() is not None:
            raise RuntimeError("ZIP CRC verification failed")
    size = output.stat().st_size
    if size >= 16_000_000:
        raise RuntimeError(f"Package exceeds 16 MiB: {size}")
    print(f"{output}\n{size / 1024 / 1024:.2f} MiB")
    print("SHA256 " + hashlib.sha256(output.read_bytes()).hexdigest())


if __name__ == "__main__":
    build()
