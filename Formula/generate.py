import argparse
import hashlib
import json
import re
import shutil
from pathlib import Path
from urllib.parse import unquote


def _bottles(assets: Path, version: str) -> dict[str, tuple[str, str]]:
    root = f"https://github.com/nyorf/claudex/releases/download/v{version}"
    tags: dict[str, tuple[str, str]] = {}
    for metadata in sorted(assets.glob("*.bottle.json")):
        entries = json.loads(metadata.read_text(encoding="utf-8"))
        if len(entries) != 1:
            raise ValueError(f"Invalid bottle metadata in {metadata.name}.")
        entry = next(iter(entries.values()))
        bottle = entry["bottle"]
        if entry["formula"]["name"] != "claudex" or entry["formula"]["pkg_version"] != version:
            raise ValueError(f"Bottle version does not match in {metadata.name}.")
        if bottle["root_url"] != root or bottle["rebuild"] != 0:
            raise ValueError(f"Bottle URL or rebuild does not match in {metadata.name}.")
        for tag, record in bottle["tags"].items():
            cellar, digest = bottle["cellar"], record["sha256"]
            filename, local = unquote(record["filename"]), record["local_filename"]
            if (
                not re.fullmatch(r"[a-z0-9_]+", tag)
                or tag in tags
                or not re.fullmatch(r"[a-f0-9]{64}", digest)
                or not re.fullmatch(r"[a-zA-Z0-9_.+-]+", filename)
                or not re.fullmatch(r"[a-zA-Z0-9_.+-]+", local)
                or not re.fullmatch(r"(:?any|:?any_skip_relocation|/[^\"\n]+)", cellar)
            ):
                raise ValueError(f"Invalid bottle tag in {metadata.name}.")
            archive = assets / local
            with archive.open("rb") as file:
                if hashlib.file_digest(file, "sha256").hexdigest() != digest:
                    raise ValueError(f"Invalid checksum for {local}.")
            if filename != local:
                shutil.copyfile(archive, assets / filename)
            tags[tag] = (cellar, digest)
    if len(tags) != 4 or not {"x86_64_linux", "arm64_linux"}.issubset(tags):
        raise ValueError("Four macOS and Linux bottle tags are required.")
    mac_tags = set(tags) - {"x86_64_linux", "arm64_linux"}
    if len(mac_tags) != 2 or sum(tag.startswith("arm64_") for tag in mac_tags) != 1:
        raise ValueError("One ARM and one Intel macOS bottle tag are required.")
    return tags


def generate(assets: Path, version: str, revision: str, output: Path) -> None:
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("Version must contain three numeric components.")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Revision must be a Git commit hash.")
    tags = _bottles(assets, version)
    root = f"https://github.com/nyorf/claudex/releases/download/v{version}"
    lines = ["  bottle do", f'    root_url "{root}"']
    for tag, (cellar, digest) in sorted(tags.items()):
        cellar_value = (
            f":{cellar.lstrip(':')}"
            if cellar.lstrip(":") in {"any", "any_skip_relocation"}
            else f'"{cellar}"'
        )
        lines.append(f'    sha256 cellar: {cellar_value}, {tag}: "{digest}"')
    lines.append("  end")
    source = (Path(__file__).parent / "claudex.rb").read_text(encoding="utf-8")
    source = re.sub(r"\n  bottle do\n(?:    [^\n]*\n)+  end\n", "\n", source, count=1)
    source, replacements = re.subn(
        r'tag: "v[0-9]+\.[0-9]+\.[0-9]+"(?:, revision: "[0-9a-f]{40}")?',
        f'tag: "v{version}", revision: "{revision}"',
        source,
        count=1,
    )
    if replacements != 1:
        raise ValueError("The source formula tag could not be replaced.")
    source = source.replace(
        '  license "Apache-2.0"\n', '  license "Apache-2.0"\n\n' + "\n".join(lines) + "\n", 1
    )
    output.mkdir(parents=True, exist_ok=True)
    (output / "claudex.rb").write_text(source, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate a Homebrew formula from release bottles."
    )
    parser.add_argument("assets", type=Path)
    parser.add_argument("version")
    parser.add_argument("revision")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    generate(args.assets, args.version, args.revision, args.output)


if __name__ == "__main__":
    main()
