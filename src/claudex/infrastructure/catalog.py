import re
from dataclasses import dataclass

import httpx2


class CatalogError(Exception):
    pass


@dataclass(frozen=True)
class CatalogModel:
    slug: str
    context_window: int | None
    max_context_window: int | None = None


def _window(value: object) -> int | None:
    return value if type(value) is int and 0 < value <= 2_000_000 else None


async def fetch_catalog(client: httpx2.AsyncClient, access_token: str) -> dict[str, CatalogModel]:
    try:
        response = await client.get(
            "https://api.openai.com/v1/models",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        response.raise_for_status()
        entries = response.json()["models"]
        if not isinstance(entries, list):
            raise ValueError
    except httpx2.HTTPStatusError as exc:
        raise CatalogError(
            f"ChatGPT model catalog returned HTTP {exc.response.status_code}. "
            "Check the selected profile and ChatGPT Usage settings."
        ) from exc
    except (httpx2.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise CatalogError("Could not read the ChatGPT model catalog.") from exc

    models: dict[str, CatalogModel] = {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("visibility") != "list":
            continue
        slug = entry.get("slug")
        if not isinstance(slug, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", slug):
            continue
        models[slug] = CatalogModel(
            slug, _window(entry.get("context_window")), _window(entry.get("max_context_window"))
        )
    return models
