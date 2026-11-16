from pathlib import Path

import httpx2
import pytest

from claudex.config.launch import plan_launch
from claudex.config.models import ModelConfigError, ModelMap, load_model_map
from claudex.infrastructure.catalog import CatalogModel, fetch_catalog


def test_launcher_keeps_available_routes_with_their_context_windows(tmp_path: Path) -> None:
    path = tmp_path / "models.yaml"
    path.write_text(
        """
default: claudex-opus
routes:
  claudex-opus:
    label: Opus 5.5
    model: gpt-sol
    aliases: [opus, claude-opus-5-5]
    context_window: 500000
    efforts: {high: xhigh}
  claudex-sonnet:
    label: Sonnet
    model: gpt-sol
    context_window: 500000
    efforts: {high: high}
  claudex-haiku:
    label: Haiku
    model: gpt-luna
    context_window: 200000
    efforts: {high: high}
  claudex-fable:
    label: Fable
    model: gpt-unavailable
    context_window: 800000
    efforts: {high: high}
""",
        encoding="utf-8",
    )
    launch = plan_launch(
        load_model_map(path),
        "claude-opus-5-5",
        {"gpt-sol": CatalogModel("gpt-sol", 475000), "gpt-luna": CatalogModel("gpt-luna", None)},
    )
    assert launch.model == "claudex-opus"
    assert list(launch.routes) == ["claudex-opus", "claudex-sonnet", "claudex-haiku"]
    assert launch.context_source == "ChatGPT model catalog"
    assert launch.context_window == 475_000
    assert launch.routes["claudex-sonnet"].context_window == 475_000
    assert launch.routes["claudex-haiku"].context_window == 200_000
    url = "http://127.0.0.1:12345"
    env = launch.environment(
        {
            "ANTHROPIC_API_KEY": "original",
            "CLAUDE_CODE_EFFORT_LEVEL": "low",
            "CLAUDE_CODE_USE_MANTLE": "1",
            "DISABLE_COMPACT": "1",
            "HOME": "/home/user",
        },
        url,
        "local-token",
    )
    assert env["ANTHROPIC_API_KEY"] == ""
    assert env["ANTHROPIC_AUTH_TOKEN"] == "local-token"
    assert env["ANTHROPIC_DEFAULT_OPUS_MODEL"] == "claudex-opus"
    assert env["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "475000"
    assert env["CLAUDE_CODE_EFFORT_LEVEL"] == ""
    assert env["CLAUDE_CODE_USE_MANTLE"] == "0"
    assert env["CLAUDE_CODE_AUTO_MODE_SERVER"] == "0"
    assert env["DISABLE_COMPACT"] == "0"
    flags = launch.arguments(tmp_path / "settings.json", "high", ["-p", "sample"])
    settings = launch.settings(url, "local-token")
    assert settings["modelPicker"]["options"][0] == {
        "model": "claudex-opus",
        "label": "Opus 5.5 (gpt-sol)",
    }
    assert settings["env"]["ANTHROPIC_AUTH_TOKEN"] == "local-token"
    assert settings["env"]["CLAUDE_CODE_AUTO_MODE_SERVER"] == "0"
    assert settings["autoCompactEnabled"] is True
    assert settings["modelSettings"] == {
        "claudex-opus": {"effortLevel": "high", "autoCompactWindow": 475_000},
        "claudex-sonnet": {"effortLevel": "high", "autoCompactWindow": 475_000},
        "claudex-haiku": {"effortLevel": "high", "autoCompactWindow": 200_000},
    }
    assert "--autocompact" not in flags
    assert "local-token" not in " ".join(flags)
    assert flags[-4:] == ["--effort", "high", "-p", "sample"]

    smaller = plan_launch(
        load_model_map(path),
        "claudex-haiku",
        {"gpt-sol": CatalogModel("gpt-sol", 475000), "gpt-luna": CatalogModel("gpt-luna", None)},
    )
    persistent = smaller.persistent_settings(url, "local-token", "high", environ={})
    assert smaller.context_window == 200_000
    assert smaller.context_source == "configured window"
    assert persistent["env"]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "475000"
    assert persistent["model"] == "claudex-haiku"
    assert persistent["modelSettings"] == settings["modelSettings"]


def test_launcher_refuses_unrepresentable_smaller_context_windows(tmp_path: Path) -> None:
    path = tmp_path / "models.yaml"
    path.write_text(
        """
default: claudex-opus
routes:
  claudex-opus:
    {label: Opus, model: gpt-sol, context_window: 475000, efforts: {high: high}}
  claudex-haiku:
    {label: Haiku, model: gpt-luna, context_window: 99000, efforts: {high: high}}
""",
        encoding="utf-8",
    )
    model_map = load_model_map(path)
    with pytest.raises(ModelConfigError, match=r"99,000-token window for claudex-haiku.*--map"):
        plan_launch(
            model_map,
            None,
            {
                "gpt-sol": CatalogModel("gpt-sol", None),
                "gpt-luna": CatalogModel("gpt-luna", None),
            },
        )
    single = plan_launch(model_map, "claudex-haiku", {"gpt-luna": CatalogModel("gpt-luna", None)})
    settings = single.settings("http://127.0.0.1:12345", "local-token")
    assert settings["env"]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "99000"
    assert settings["modelSettings"]["claudex-haiku"]["autoCompactWindow"] == "auto"


def test_invalid_alias_or_effort_fails_before_launch(tmp_path: Path) -> None:
    model_map = load_model_map(tmp_path / "models.yaml")
    catalog = {"gpt-6.1-sol": CatalogModel("gpt-6.1-sol", None)}
    with pytest.raises(ModelConfigError, match="Unknown model"):
        plan_launch(model_map, "missing", catalog)
    launch = plan_launch(model_map, "opus", catalog)
    assert launch.context_source == "configured window"
    with pytest.raises(ModelConfigError, match="local session flags"):
        launch.arguments(tmp_path / "settings.json", "high", ["--model=another"])
    with pytest.raises(ModelConfigError, match="local session flags"):
        launch.arguments(tmp_path / "settings.json", "high", ["--cloud"])


def test_yaml_duplicate_keys_fail(tmp_path: Path) -> None:
    path = tmp_path / "models.yaml"
    path.write_text("default: claudex-one\ndefault: claudex-two\n", encoding="utf-8")
    with pytest.raises(ModelConfigError, match="Invalid model map"):
        load_model_map(path)


def test_route_id_cannot_embed_a_recognized_claude_model(tmp_path: Path) -> None:
    path = tmp_path / "models.yaml"
    path.write_text(
        "default: claudex-claude-opus-5-5\nroutes:\n"
        "  claudex-claude-opus-5-5:\n"
        "    {label: Opus, model: gpt-sol, context_window: 475000, efforts: {high: high}}\n",
        encoding="utf-8",
    )
    with pytest.raises(ModelConfigError, match="custom route ID"):
        load_model_map(path)


def test_default_effort_is_passed_to_claude(tmp_path: Path) -> None:
    launch = plan_launch(
        load_model_map(tmp_path / "models.yaml"),
        "opus",
        {"gpt-6.1-sol": CatalogModel("gpt-6.1-sol", None)},
    )
    assert launch.arguments(tmp_path / "settings.json", None, [])[-2:] == ["--effort", "medium"]


def test_bundled_models_use_supported_default_for_compaction(tmp_path: Path) -> None:
    model_map = load_model_map(tmp_path / "models.yaml")
    catalog = {
        route.model: CatalogModel(route.model, route.context_window, 872_000)
        for route in model_map.routes.values()
    }

    launch = plan_launch(model_map, None, catalog, profile="work")
    settings = launch.settings("http://127.0.0.1:8765", "synthetic")

    assert len(launch.routes) == 8
    assert {route.context_window for route in launch.routes.values()} == {872_000}
    assert launch.context_source == "Claudex default window"
    assert settings["env"]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "872000"
    assert settings["autoCompactEnabled"] is True
    assert {fields["autoCompactWindow"] for fields in settings["modelSettings"].values()} == {
        872_000
    }


def test_launch_uses_each_models_supported_window(tmp_path: Path) -> None:
    model_map = load_model_map(tmp_path / "models.yaml")
    catalog = {
        "gpt-6.1-sol": CatalogModel("gpt-6.1-sol", 500_000, 1_000_000),
        "gpt-6-astra": CatalogModel("gpt-6-astra", 272_000, 500_000),
        "gpt-6.1-terra": CatalogModel("gpt-6.1-terra", None),
    }
    launch = plan_launch(model_map, "opus", catalog, profile="work")
    settings = launch.settings("http://127.0.0.1:8765", "synthetic")

    assert {name: route.context_window for name, route in launch.routes.items()} == {
        "claudex-opus": 872_000,
        "claudex-fable": 272_000,
        "claudex-sonnet": 500_000,
    }
    assert {
        name: fields["autoCompactWindow"] for name, fields in settings["modelSettings"].items()
    } == {"claudex-opus": 872_000, "claudex-fable": 272_000, "claudex-sonnet": 500_000}


@pytest.mark.parametrize(
    ("default", "maximum", "expected", "source"),
    [
        (272_000, 872_000, 872_000, "Claudex default window"),
        (None, 872_000, 872_000, "Claudex default window"),
        (872_000, None, 872_000, "Claudex default window"),
        (1_000_000, 900_000, 872_000, "Claudex default window"),
        (272_000, 871_999, 272_000, "ChatGPT model catalog"),
        (500_000, 272_000, 500_000, "ChatGPT model catalog"),
        (272_000, None, 272_000, "ChatGPT model catalog"),
        (None, None, 500_000, "configured window"),
    ],
)
def test_launch_selects_context_from_catalog_evidence(
    tmp_path: Path, default: int | None, maximum: int | None, expected: int, source: str
) -> None:
    model_map = load_model_map(tmp_path / "models.yaml")
    catalog = {"gpt-6.1-sol": CatalogModel("gpt-6.1-sol", default, maximum)}
    launch = plan_launch(model_map, None, catalog, profile="personal")

    assert launch.context_window == expected
    assert launch.context_source == source


@pytest.fixture
def override_map(tmp_path: Path) -> ModelMap:
    path = tmp_path / "models.yaml"
    path.write_text(
        """
default: claudex-opus
routes:
  claudex-opus:
    {label: Opus, model: gpt-sol, context_window: 500000, efforts: {high: high}}
  claudex-fable:
    label: Fable
    model: gpt-astra
    context_window: 200000
    context_window_overrides: {personal: 700000}
    efforts: {high: high}
""",
        encoding="utf-8",
    )
    return load_model_map(path)


def test_profile_override_applies_to_one_route_and_one_profile(override_map: ModelMap) -> None:
    catalog = {
        "gpt-astra": CatalogModel("gpt-astra", 272000, 872000),
        "gpt-sol": CatalogModel("gpt-sol", None),
    }
    personal = plan_launch(override_map, "claudex-fable", catalog, profile="personal")
    work = plan_launch(override_map, "claudex-fable", catalog, profile="work")
    unspecified = plan_launch(override_map, "claudex-fable", catalog)
    assert personal.context_window == 700_000
    assert personal.context_source == "profile override"
    assert personal.routes["claudex-opus"].context_window == 500_000
    assert work.context_window == unspecified.context_window == 872_000
    assert work.context_source == "Claudex default window"

    selected_sol = plan_launch(override_map, None, catalog, profile="personal")
    assert selected_sol.context_window == 500_000
    assert selected_sol.context_source == "configured window"
    settings = selected_sol.settings("http://127.0.0.1:8765", "synthetic")
    assert settings["modelSettings"]["claudex-fable"]["autoCompactWindow"] == 700_000
    assert settings["env"]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "700000"


@pytest.mark.parametrize(
    ("maximum", "message"),
    [
        (None, "no valid maximum context window"),
        (200_000, "no valid maximum context window"),
        (500_000, "exceeds the ChatGPT catalog maximum of 500,000"),
    ],
)
def test_profile_override_requires_a_supported_catalog_maximum(
    override_map: ModelMap, maximum: int | None, message: str
) -> None:
    catalog = {
        "gpt-astra": CatalogModel("gpt-astra", 272000, maximum),
        "gpt-sol": CatalogModel("gpt-sol", None),
    }
    with pytest.raises(ModelConfigError, match=message):
        plan_launch(override_map, None, catalog, profile="personal")
    assert plan_launch(override_map, None, catalog, profile="work").context_window == 500_000


async def test_catalog_uses_context_window_instead_of_maximum() -> None:
    def upstream(request: httpx2.Request) -> httpx2.Response:
        assert str(request.url) == "https://api.openai.com/v1/models"
        assert request.headers["authorization"] == "Bearer sample-access"
        return httpx2.Response(
            200,
            json={
                "models": [
                    {"slug": "gpt-a", "visibility": "list", "context_window": 475000},
                    {"slug": "gpt-b", "visibility": "list", "max_context_window": 1000000},
                    {"slug": "gpt-c", "visibility": "list", "max_context_window": True},
                    {"slug": "gpt-d", "visibility": "list", "max_context_window": "872000"},
                    {"slug": "gpt-e", "visibility": "list", "max_context_window": 2000001},
                    {"slug": "hidden", "visibility": "hide"},
                ],
            },
        )

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(upstream)) as client:
        assert await fetch_catalog(client, "sample-access") == {
            "gpt-a": CatalogModel("gpt-a", 475000),
            "gpt-b": CatalogModel("gpt-b", None, 1_000_000),
            "gpt-c": CatalogModel("gpt-c", None),
            "gpt-d": CatalogModel("gpt-d", None),
            "gpt-e": CatalogModel("gpt-e", None),
        }
