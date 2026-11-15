from pathlib import Path

from platformdirs import PlatformDirs
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

_dirs = PlatformDirs("claudex", appauthor=False)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="CLAUDEX_")

    config_dir: Path = Field(default_factory=lambda: _dirs.user_config_path)
    state_dir: Path = Field(default_factory=lambda: _dirs.user_state_path)
    claude_bin: str = "claude"
