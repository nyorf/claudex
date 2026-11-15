import hashlib
import os
import platform
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path
from tempfile import TemporaryDirectory
from tomllib import load

ROOT = Path(__file__).resolve().parents[1]


def _run(*args: str, env: dict[str, str] | None = None) -> None:
    subprocess.run(args, cwd=ROOT, check=True, env=env)  # noqa: S603


def main() -> None:
    with (ROOT / "pyproject.toml").open("rb") as file:
        version = load(file)["project"]["version"]
    system = platform.system().lower()
    machine = (
        sysconfig.get_platform().lower().removeprefix("win-")
        if system == "windows"
        else platform.machine().lower()
    )
    suffix = {"amd64": "x86_64", "aarch64": "arm64"}.get(machine, machine)
    if (
        system not in {"darwin", "linux", "windows"}
        or suffix not in {"arm64", "x86_64"}
        or (system == "windows" and suffix != "x86_64")
    ):
        raise SystemExit("Bundles support macOS and Linux on ARM64 and x86-64, and Windows x86-64.")
    archive_name = f"claudex-{version}-{system}-{suffix}"
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    cache = ROOT / "build" / "pyinstaller-cache"
    env = dict(os.environ, PYINSTALLER_CONFIG_DIR=str(cache))
    with TemporaryDirectory(prefix="claudex-build-", dir=dist) as temporary:
        build = Path(temporary)
        _run(
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--onedir",
            "--name",
            "claudex",
            "--paths",
            "src",
            "--distpath",
            str(build),
            "--workpath",
            str(build / "work"),
            "--specpath",
            str(build),
            "--copy-metadata",
            "claudex",
            "--collect-data",
            "claudex",
            "--collect-submodules",
            "keyring.backends",
            "--hidden-import",
            "uvicorn.logging",
            "--hidden-import",
            "uvicorn.loops.auto",
            "--hidden-import",
            "uvicorn.protocols.http.auto",
            "--hidden-import",
            "uvicorn.protocols.websockets.auto",
            "--hidden-import",
            "uvicorn.lifespan.on",
            "src/claudex/__main__.py",
            env=env,
        )
        shutil.copyfile(ROOT / "LICENSE", build / "claudex" / "LICENSE")
        shutil.copyfile(ROOT / "man" / "claudex.1", build / "claudex" / "claudex.1")
        archive_format = "zip" if system == "windows" else "gztar"
        archive = Path(
            shutil.make_archive(str(dist / archive_name), archive_format, build, "claudex")
        )
        with archive.open("rb") as file:
            digest = hashlib.file_digest(file, "sha256").hexdigest()
        checksum = dist / f"{archive.name}.sha256"
        checksum.write_text(f"{digest}  {archive.name}\n", encoding="ascii")
    with TemporaryDirectory(prefix="claudex-smoke-", dir=dist) as temporary:
        extracted = Path(temporary)
        if system == "windows":
            shutil.unpack_archive(archive, extracted)
        else:
            shutil.unpack_archive(archive, extracted, filter="data")
        executable = extracted / "claudex" / ("claudex.exe" if system == "windows" else "claudex")
        _run(os.fspath(executable), "--version")
        _run(os.fspath(executable), "models", "--example")


if __name__ == "__main__":
    main()
