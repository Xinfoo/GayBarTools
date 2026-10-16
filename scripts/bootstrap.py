"""Install only into this project's virtual environment, using the stdlib."""

import argparse
import os
from pathlib import Path
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description="创建项目专用虚拟环境并安装依赖")
    parser.add_argument("--browser", action="store_true", help="同时下载项目专用 Chromium")
    args = parser.parse_args()
    project = Path(__file__).resolve().parent.parent
    runtime = project / ".runtime"
    temporary = runtime / "tmp"
    temporary.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update({"PIP_CACHE_DIR": str(runtime / "pip-cache"),
                "PLAYWRIGHT_BROWSERS_PATH": str(runtime / "browsers"),
                "TMPDIR": str(temporary), "TMP": str(temporary), "TEMP": str(temporary),
                "PYTHONDONTWRITEBYTECODE": "1"})
    interpreter = project / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not interpreter.is_file():
        subprocess.run([sys.executable, "-m", "venv", str(project / ".venv")], env=env, cwd=project, check=True)
    subprocess.run([str(interpreter), "-m", "pip", "install", "-r", "requirements.lock"], env=env, cwd=project, check=True)
    if args.browser:
        subprocess.run([str(interpreter), "-m", "gaybar_tools", "--install-browser"], env=env, cwd=project, check=True)
    print(f"安装完成。启动：{interpreter} -B -m gaybar_tools")


if __name__ == "__main__":
    main()
