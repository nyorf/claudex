import argparse
import os
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from tempfile import TemporaryDirectory
from tomllib import load

ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "https://github.com/nyorf/claudex"


def _run(
    command: list[str], environment: dict[str, str], *, choice: str | None = None, ok: bool = True
) -> str:
    result = subprocess.run(  # noqa: S603
        command,
        cwd=ROOT,
        env=environment,
        input=choice,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    if (result.returncode == 0) != ok:
        raise SystemExit(result.stdout + result.stderr or f"Unexpected exit {result.returncode}.")
    return result.stdout


@contextmanager
def _windows_path() -> Iterator[str]:
    import winreg

    original = r"%USERPROFILE%\Claudex fixture;C:\Claudex fixture"
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
        saved = None
        with suppress(FileNotFoundError):
            saved = winreg.QueryValueEx(key, "Path")
        winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, original)
        try:
            yield original
        finally:
            if saved is None:
                with suppress(FileNotFoundError):
                    winreg.DeleteValue(key, "Path")
            else:
                winreg.SetValueEx(key, "Path", 0, saved[1], saved[0])


def _check_windows_path(value: str) -> None:
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
        assert winreg.QueryValueEx(key, "Path") == (value, winreg.REG_EXPAND_SZ)


def _smoke(archive: Path, workspace: Path, version: str, windows_path: str | None) -> None:
    environment = dict(os.environ)
    expected_version = f"claudex {version} ({REPOSITORY})"
    if os.name == "nt":
        shell = shutil.which("powershell.exe")
        if shell is None:
            raise SystemExit("Windows PowerShell is required for the installer smoke test.")
        environment["LOCALAPPDATA"] = str(workspace)
        install = workspace / "Programs" / "Claudex" / "current"
        executable = install / "claudex.exe"
        script = "install.ps1"
        command = [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File"]
        local_arg = "-Archive"
        options = ["-NoPath"] if windows_path is None else []
    else:
        environment["HOME"] = str(workspace)
        install = workspace / ".local" / "opt" / "claudex"
        executable = workspace / ".local" / "bin" / "claudex"
        script = "install.sh"
        command = ["/bin/bash"]
        local_arg = "--archive"
        options = []

    installer = [*command, str(ROOT / "scripts" / script), *options]
    install_archive = [*installer, local_arg, str(archive)]
    _run(install_archive, environment, choice="1\n")
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert (install / "LICENSE").read_text(encoding="utf-8") == license_text
    assert _run([str(executable), "license"], environment) == license_text
    if os.name != "nt":
        manual = workspace / ".local" / "share" / "man" / "man1" / "claudex.1"
        assert manual.is_symlink() and manual.resolve() == (install / "claudex.1").resolve()
    if windows_path is not None:
        _check_windows_path(f"{windows_path};{install}")
    assert _run([str(executable), "--version"], environment).strip() == expected_version
    assert "claudex-opus" in _run([str(executable), "models", "--example"], environment)
    _run(install_archive, environment, choice="2\n")
    assert _run([str(executable), "--version"], environment).strip() == expected_version
    assert _run([str(executable), "license"], environment) == license_text
    update_option = "-Update" if os.name == "nt" else "--update"
    _run([*install_archive, update_option], environment)
    assert _run([str(executable), "--version"], environment).strip() == expected_version
    if windows_path is not None:
        _check_windows_path(f"{windows_path};{install}")

    # a failed verification must leave the current installation available
    damaged = workspace / archive.name
    shutil.copyfile(archive, damaged)
    damaged.with_name(f"{damaged.name}.sha256").write_text(
        f"{'0' * 64}  {damaged.name}\n", encoding="ascii"
    )
    _run([*installer, local_arg, str(damaged)], environment, choice="2\n", ok=False)
    assert _run([str(executable), "--version"], environment).strip() == expected_version

    _run([*command, str(install / script), *options], environment, choice="3\n")
    if windows_path is not None:
        _check_windows_path(windows_path)
    assert not executable.exists() and not executable.is_symlink()
    assert not install.exists()
    if os.name != "nt":
        assert not manual.exists() and not manual.is_symlink()
    print("Installer install, update, checksum rejection, and removal passed.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify the bundle installer in a temporary home.")
    parser.add_argument("archive", type=Path)
    parser.add_argument("--verify-windows-path", action="store_true", help="Windows CI only.")
    args = parser.parse_args()
    if args.verify_windows_path and (os.name != "nt" or os.environ.get("GITHUB_ACTIONS") != "true"):
        parser.error("Windows PATH verification is only available on a GitHub Actions runner.")
    with (ROOT / "pyproject.toml").open("rb") as file:
        version = load(file)["project"]["version"]
    with TemporaryDirectory(prefix="claudex-installer-smoke-") as temporary:
        archive = args.archive.resolve(strict=True)
        workspace = Path(temporary)
        if args.verify_windows_path:
            with _windows_path() as value:
                _smoke(archive, workspace, version, value)
        else:
            _smoke(archive, workspace, version, None)


if __name__ == "__main__":
    main()
