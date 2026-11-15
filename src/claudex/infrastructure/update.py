import os
import shutil
import subprocess  # nosec B404
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path


class UpdateError(Exception):
    pass


_WINDOWS_HELPER = r"""param(
    [int]$ParentPid, [string]$Installer, [string]$Ready, [string]$Directory, [string]$PowerShell
)
$ErrorActionPreference = 'Stop'
try {
    $parent = [System.Diagnostics.Process]::GetProcessById($ParentPid)
    $null = $parent.Handle
    [IO.File]::WriteAllText($Ready, 'ready')
    $parent.WaitForExit()
    & $PowerShell -NoProfile -ExecutionPolicy Bypass -File $Installer -Update
    if ($LASTEXITCODE -ne 0) { throw "The installer exited with code $LASTEXITCODE." }
} catch {
    [Console]::Error.WriteLine("Claudex update failed. $($_.Exception.Message)")
    exit 1
} finally {
    Remove-Item -LiteralPath $Directory -Recurse -Force -ErrorAction SilentlyContinue
}
"""


def _installer_path() -> Path:
    if not getattr(sys, "frozen", False):
        raise UpdateError(
            "This Claudex installation is not a release bundle. "
            "Use brew upgrade for Homebrew or the tool used to install it."
        )
    if sys.platform == "win32":
        root = os.environ.get("LOCALAPPDATA")
        if not root:
            raise UpdateError("LOCALAPPDATA is unavailable. Run the release installer again.")
        directory = Path(root) / "Programs" / "Claudex" / "current"
        executable = directory / "claudex.exe"
        script = directory / "install.ps1"
    elif sys.platform in ("darwin", "linux"):
        directory = Path.home() / ".local" / "opt" / "claudex"
        executable = directory / "claudex"
        script = directory / "install.sh"
    else:
        raise UpdateError("This platform does not have a release installer.")
    marker = directory / ".claudex-install"
    try:
        valid = (
            not directory.is_symlink()
            and not executable.is_symlink()
            and executable.is_file()
            and Path(sys.executable).resolve(strict=True) == executable.resolve(strict=True)
            and not marker.is_symlink()
            and marker.is_file()
            and not script.is_symlink()
            and script.is_file()
        )
    except OSError:
        valid = False
    if not valid:
        raise UpdateError(
            "This bundle is not managed by the Claudex installer. Run the release installer "
            "to install or update it, or use brew upgrade for a Homebrew installation."
        )
    return script


def _start_windows_update(installer: Path) -> None:
    system_root = os.environ.get("SYSTEMROOT")
    if not system_root:
        raise UpdateError("SystemRoot is unavailable. Run the saved installer manually.")
    powershell = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    if not powershell.is_file():
        raise UpdateError("Windows PowerShell is unavailable. Run the saved installer manually.")
    directory = Path(tempfile.mkdtemp(prefix="claudex-update-"))
    process = None
    try:
        copied = directory / "install.ps1"
        shutil.copyfile(installer, copied)
        helper = directory / "update.ps1"
        helper.write_text(_WINDOWS_HELPER, encoding="utf-8")
        ready = directory / "ready"
        process = subprocess.Popen(  # noqa: S603  # nosec B603
            [
                str(powershell),
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(helper),
                "-ParentPid",
                str(os.getpid()),
                "-Installer",
                str(copied),
                "-Ready",
                str(ready),
                "-Directory",
                str(directory),
                "-PowerShell",
                str(powershell),
            ],
        )
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if ready.is_file():
                print("Updating Claudex. The installer will continue after this command exits.")
                return
            if process.poll() is not None:
                break
            time.sleep(0.05)
        raise UpdateError("The update installer did not start. Run the saved installer manually.")
    except OSError:
        raise UpdateError("Could not start the update installer. Run it manually.") from None
    finally:
        if process is None or not ready.is_file():
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
            shutil.rmtree(directory, ignore_errors=True)


def update_installation(before_update: Callable[[], None] | None = None) -> int:
    installer = _installer_path()
    if before_update is not None:
        before_update()
    if sys.platform == "win32":
        _start_windows_update(installer)
        return 0
    try:
        result = subprocess.run(  # noqa: S603  # nosec B603
            ["/bin/bash", str(installer), "--update"], check=False
        )
    except OSError:
        raise UpdateError("Could not start the update installer. Run it manually.") from None
    return result.returncode
