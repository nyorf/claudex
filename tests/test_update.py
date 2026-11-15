from pathlib import Path
from types import SimpleNamespace

import pytest

from claudex.infrastructure import update


def test_source_installation_gets_update_guidance(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update.sys, "frozen", False, raising=False)
    with pytest.raises(update.UpdateError, match="not a release bundle"):
        update.update_installation()


def test_installed_bundle_runs_saved_installer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / ".local" / "opt" / "claudex"
    directory.mkdir(parents=True)
    executable = directory / "claudex"
    installer = directory / "install.sh"
    executable.touch()
    installer.touch()
    (directory / ".claudex-install").touch()
    monkeypatch.setattr(update.sys, "frozen", True, raising=False)
    monkeypatch.setattr(update.sys, "executable", str(executable))
    monkeypatch.setattr(update.sys, "platform", "darwin")
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    calls = []

    def run(command: list[str], *, check: bool) -> SimpleNamespace:
        calls.append((command, check))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(update.subprocess, "run", run)
    assert update.update_installation() == 0
    assert calls == [(["/bin/bash", str(installer), "--update"], False)]
    (directory / ".claudex-install").unlink()
    with pytest.raises(update.UpdateError, match="not managed"):
        update.update_installation()


def test_windows_helper_captures_parent_before_returning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = tmp_path / "install.ps1"
    installer.write_text("fixture", encoding="ascii")
    system = tmp_path / "system"
    powershell = system / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    powershell.parent.mkdir(parents=True)
    powershell.touch()
    monkeypatch.setenv("SYSTEMROOT", str(system))
    monkeypatch.setenv("PsModulePath", "synthetic-incompatible-path")
    monkeypatch.setenv("CLAUDEX_TEST_ENV", "preserved")
    monkeypatch.setattr(update.tempfile, "mkdtemp", lambda **kwargs: str(tmp_path))
    calls = []

    def start(command: list[str], *, env: dict[str, str]) -> SimpleNamespace:
        calls.append((command, env))
        Path(command[command.index("-Ready") + 1]).touch()
        return SimpleNamespace(poll=lambda: None)

    monkeypatch.setattr(update.subprocess, "Popen", start)
    installer_copy = tmp_path / "source.ps1"
    installer.rename(installer_copy)
    update._start_windows_update(installer_copy)
    command, env = calls[0]
    assert command[0] == str(powershell)
    assert command[-2:] == ["-PowerShell", str(powershell)]
    assert all(key.upper() != "PSMODULEPATH" for key in env)
    assert env["CLAUDEX_TEST_ENV"] == "preserved"
    assert update.os.environ["PsModulePath"] == "synthetic-incompatible-path"
    assert (tmp_path / "install.ps1").read_text(encoding="ascii") == "fixture"
    assert "$parent.WaitForExit()" in (tmp_path / "update.ps1").read_text(encoding="utf-8")
