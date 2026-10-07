"""
Сборка установщика: PyInstaller собирает программу в папку dist/TG Autocomment,
Inno Setup упаковывает её в dist/TG-Autocomment-<версия>-Setup.exe.

    pip install -r requirements.txt pyinstaller
    python build.py              # программа + установщик (нужен Inno Setup 6)
    python build.py --no-setup   # только программа, без установщика

Обычно собирает GitHub Actions при публикации тега vX.Y.Z (.github/workflows/release.yml).
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))
from core import VERSION  # noqa: E402

NAME = "TG Autocomment"
DIST = ROOT / "dist"


def find_iscc() -> str | None:
    for p in (shutil.which("iscc"),
              Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Inno Setup 6" / "ISCC.exe",
              Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Inno Setup 6" / "ISCC.exe",
              Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Inno Setup 6" / "ISCC.exe"):
        if p and Path(p).exists():
            return str(p)
    return None


def main():
    sep = os.pathsep
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
        "--name", NAME, "--icon", str(ROOT / "assets" / "icon.ico"),
        "--distpath", str(DIST), "--workpath", str(ROOT / "build"), "--specpath", str(ROOT / "build"),
        "--add-data", f"{ROOT / 'prompt.example.txt'}{sep}.",
        "--add-data", f"{ROOT / 'assets' / 'icon.ico'}{sep}assets",
        "--collect-data", "customtkinter",      # темы и шрифты customtkinter
        "--hidden-import", "pystray._win32",    # pystray выбирает бэкенд при запуске
        str(ROOT / "app.py"),
    ], check=True)
    print(f"Программа: {DIST / NAME / (NAME + '.exe')}")

    if "--no-setup" in sys.argv:
        return
    iscc = find_iscc()
    if not iscc:
        sys.exit("Не найден Inno Setup 6 (ISCC.exe) — установите его или запустите с --no-setup")
    subprocess.run([iscc, f"/DAppVersion={VERSION}", f"/DSourceDir={DIST / NAME}",
                    f"/O{DIST}", str(ROOT / "installer.iss")], check=True)
    print(f"Установщик: {DIST / f'TG-Autocomment-{VERSION}-Setup.exe'}")


if __name__ == "__main__":
    main()
