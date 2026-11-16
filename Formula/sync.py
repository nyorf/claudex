import argparse
import hashlib
import io
import json
import re
import tarfile
from pathlib import Path

VERSION = r"[0-9]+\.[0-9]+\.[0-9]+"
SOURCE = (
    r'url "https://github\.com/nyorf/claudex\.git", tag: "v('
    + VERSION
    + r')"(?:, revision: "([0-9a-f]{40})")?'
)
BOTTLE = r"\n  bottle do\n(?:    [^\n]*\n)+  end\n"
LINE = re.compile(
    r'^    sha256 cellar: (?P<cellar>:any|:any_skip_relocation|"/[A-Za-z0-9._/-]+"), '
    r'(?P<tag>[a-z0-9_]+): "(?P<digest>[a-f0-9]{64})"$',
    re.MULTILINE,
)


def asset_digest(release: dict, name: str) -> str:
    assets = [asset for asset in release["assets"] if asset["name"] == name]
    if len(assets) != 1 or assets[0]["state"] != "uploaded":
        raise ValueError(f"Missing uploaded release asset {name}.")
    digest = assets[0]["digest"]
    if not isinstance(digest, str) or not re.fullmatch(r"sha256:[a-f0-9]{64}", digest):
        raise ValueError(f"Invalid release asset digest for {name}.")
    return digest.removeprefix("sha256:")


def recipe(source: str) -> str:
    source, count = re.subn(SOURCE, 'url "source", tag: "version"', source)
    if count != 1:
        raise ValueError("The formula source is invalid.")
    source, count = re.subn(BOTTLE, "\n", source)
    if count > 1:
        raise ValueError("The formula has multiple bottle blocks.")
    return "\n".join(line for line in source.splitlines() if line.strip())


def check_bottles(release: dict, candidate: str) -> None:
    tag = release["tag_name"]
    blocks = re.findall(BOTTLE, candidate)
    if len(blocks) != 1:
        raise ValueError("The formula must have one bottle block.")
    root = f'    root_url "https://github.com/nyorf/claudex/releases/download/{tag}"'
    lines = blocks[0].strip("\n").splitlines()
    if len(lines) != 7 or lines[:2] != ["  bottle do", root] or lines[-1] != "  end":
        raise ValueError("The formula bottle block is invalid.")
    entries = [LINE.fullmatch(line) for line in lines[2:-1]]
    if any(entry is None for entry in entries):
        raise ValueError("The formula bottle checksums are invalid.")
    bottles = {entry["tag"]: entry for entry in entries if entry is not None}
    linux = {"arm64_linux", "x86_64_linux"}
    mac = set(bottles) - linux
    if (
        len(bottles) != 4
        or not linux.issubset(bottles)
        or len(mac) != 2
        or sum(name.startswith("arm64_") for name in mac) != 1
    ):
        raise ValueError("Four macOS and Linux bottles are required.")
    intel = next(name for name in mac if not name.startswith("arm64_"))
    if bottles[intel]["cellar"] != ":any_skip_relocation":
        raise ValueError("The Intel bottle must be relocatable.")
    for name, entry in bottles.items():
        filename = f"claudex-{tag[1:]}.{name}.bottle.tar.gz"
        if asset_digest(release, filename) != entry["digest"]:
            raise ValueError(f"The checksum for {filename} does not match the release.")


def update(release: dict, archive: bytes, revision: str, tagged: str, current: str) -> str:
    tag = release["tag_name"]
    if release["draft"] or release["prerelease"] or not re.fullmatch("v" + VERSION, tag):
        raise ValueError("The release must be a stable version.")
    if not re.fullmatch(r"[a-f0-9]{40}", revision):
        raise ValueError("The release revision is invalid.")
    expected_digest = asset_digest(release, "claudex-homebrew-formula.tar.gz")
    if hashlib.sha256(archive).hexdigest() != expected_digest:
        raise ValueError("The formula archive checksum does not match the release.")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as bundle:
        members = bundle.getmembers()
        formulas = [member for member in members if member.name == "Formula/claudex.rb"]
        if len(formulas) != 1 or not formulas[0].isfile() or formulas[0].size > 100_000:
            raise ValueError("The formula archive is invalid.")
        file = bundle.extractfile(formulas[0])
        if file is None:
            raise ValueError("The formula is missing from the archive.")
        candidate = file.read().decode("utf-8")
    source = re.findall(SOURCE, candidate)
    if source != [(tag[1:], revision)]:
        raise ValueError("The formula source does not match the release tag and revision.")
    current_source = re.findall(SOURCE, current)
    if len(current_source) != 1:
        raise ValueError("The current formula source is invalid.")
    if tuple(map(int, current_source[0][0].split("."))) > tuple(map(int, tag[1:].split("."))):
        raise ValueError("The current formula is newer than the latest release.")
    if recipe(current) != recipe(tagged) or recipe(candidate) != recipe(tagged):
        raise ValueError("The formula recipe has changed since the release tag.")
    check_bottles(release, candidate)
    return candidate


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate and update the Homebrew formula.")
    parser.add_argument("release", type=Path)
    parser.add_argument("archive", type=Path)
    parser.add_argument("revision")
    parser.add_argument("tagged", type=Path)
    parser.add_argument("current", type=Path)
    args = parser.parse_args()
    candidate = update(
        json.loads(args.release.read_text(encoding="utf-8")),
        args.archive.read_bytes(),
        args.revision,
        args.tagged.read_text(encoding="utf-8"),
        args.current.read_text(encoding="utf-8"),
    )
    args.current.write_text(candidate, encoding="utf-8")


if __name__ == "__main__":
    main()
