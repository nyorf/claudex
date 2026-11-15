import argparse
import json
import re
from collections import Counter
from pathlib import Path

_PROTECTED = re.compile(
    r"`[^`\n]+`|https?://[^\s)>]+|\[[A-Z]\d+\]|\b\d+(?:[.,]\d+)*\b|[“\"][^“”\"\n]+[”\"]"
)


def tokens(path: Path) -> dict[str, int]:
    return dict(Counter(_PROTECTED.findall(path.read_text(encoding="utf-8"))))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Preserve protected references during prose edits."
    )
    parser.add_argument("action", choices=("snapshot", "check"))
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("paths", nargs="*", type=Path)
    args = parser.parse_args()
    if args.action == "snapshot":
        if not args.paths:
            parser.error("snapshot requires at least one file")
        args.snapshot.parent.mkdir(parents=True, exist_ok=True)
        baseline = {str(path): tokens(path) for path in args.paths}
        args.snapshot.write_text(json.dumps(baseline, sort_keys=True), encoding="utf-8")
        return 0
    baseline = json.loads(args.snapshot.read_text(encoding="utf-8"))
    failures = [file for file, before in baseline.items() if tokens(Path(file)) != before]
    for file in failures:
        print(f"Protected references changed in {file}.")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
