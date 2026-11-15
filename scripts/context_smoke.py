import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from claudex.config.launch import Launch
from claudex.domain.models import ModelRoute

_WINDOW = 475_000


def main() -> None:
    binary = shutil.which(os.environ.get("CLAUDEX_CLAUDE_BIN", "claude"))
    if binary is None:
        raise SystemExit("Install Claude Code or set CLAUDEX_CLAUDE_BIN.")
    with TemporaryDirectory(prefix="claudex-context-") as temporary:
        root = Path(temporary)
        for name in ("home", "config", "tmp"):
            (root / name).mkdir()
        isolated = {
            "HOME": str(root / "home"),
            "XDG_CONFIG_HOME": str(root / "home"),
            "CLAUDE_CONFIG_DIR": str(root / "config"),
            "TMPDIR": str(root / "tmp"),
            "PATH": "/usr/bin:/bin",
            "CLAUDE_CODE_MAX_RETRIES": "0",
        }
        route = ModelRoute("gpt-synthetic", "Opus (gpt-synthetic)", _WINDOW, {"high": "high"})
        launch = Launch("claudex-opus", {"claudex-opus": route}, _WINDOW, "synthetic")
        base_url = "http://127.0.0.1:9"
        env = launch.environment(isolated, base_url, "synthetic-meter-probe")
        settings = root / "settings.json"
        settings.write_text(
            json.dumps(launch.settings(base_url, "synthetic-meter-probe")), encoding="utf-8"
        )
        args = launch.arguments(
            settings,
            "high",
            [
                "--strict-mcp-config",
                "--setting-sources",
                "",
                "--no-session-persistence",
                "--tools",
                "",
                "--output-format",
                "json",
                "-p",
                "/context",
            ],
        )
        run = subprocess.run(  # noqa: S603
            [binary, *args],
            env=env,
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
    result = json.loads(run.stdout)
    if (
        result.get("num_turns") != 0
        or result.get("duration_api_ms") != 0
        or not re.search(r"/ 475k\b", result.get("result", ""))
        or "Autocompact buffer" not in result.get("result", "")
    ):
        raise SystemExit("Claude Code did not show the expected context and compaction buffer.")
    print(
        "Claude Code /context showed 475k with an auto-compaction buffer and zero inference calls."
    )


if __name__ == "__main__":
    main()
