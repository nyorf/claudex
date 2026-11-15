import argparse
import hashlib
import re
from pathlib import Path


def generate(assets: Path, version: str, revision: str, output: Path) -> None:
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
        raise ValueError("Version must contain three numeric components.")
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ValueError("Revision must be a Git commit hash.")
    targets = ("darwin-arm64", "darwin-x86_64", "linux-arm64", "linux-x86_64")
    checksums = {}
    for target in targets:
        name = f"claudex-{version}-{target}.tar.gz"
        file = assets / name
        digest = hashlib.sha256(file.read_bytes()).hexdigest()
        if (assets / f"{name}.sha256").read_text(encoding="ascii").split() != [digest, name]:
            raise ValueError(f"Invalid checksum for {name}.")
        checksums[target] = digest
    root = "https://github.com/nyorf/claudex/releases/download/v#{version}"
    formula = f'''class ClaudexBin < Formula
  desc "Use ChatGPT models with Claude Code"
  homepage "https://github.com/nyorf/claudex"
  version "{version}"
  license "Apache-2.0"

  conflicts_with "claudex", because: "both install the claudex command"

  on_macos do
    on_arm do
      url "{root}/claudex-#{{version}}-darwin-arm64.tar.gz"
      sha256 "{checksums["darwin-arm64"]}"
    end
    on_intel do
      url "{root}/claudex-#{{version}}-darwin-x86_64.tar.gz"
      sha256 "{checksums["darwin-x86_64"]}"
    end
  end
  on_linux do
    on_arm do
      url "{root}/claudex-#{{version}}-linux-arm64.tar.gz"
      sha256 "{checksums["linux-arm64"]}"
    end
    on_intel do
      url "{root}/claudex-#{{version}}-linux-x86_64.tar.gz"
      sha256 "{checksums["linux-x86_64"]}"
    end
  end

  def install
    libexec.install buildpath.children
    bin.install_symlink libexec/"claudex"
    man1.install libexec/"claudex.1"
  end

  test do
    assert_match "claudex #{{version}}", shell_output("#{{bin}}/claudex --version")
    assert_match "claudex-opus", shell_output("#{{bin}}/claudex models --example")
  end
end
'''
    source = (Path(__file__).parent / "claudex.rb").read_text(encoding="utf-8")
    source = re.sub(
        r'tag: "v[0-9]+\.[0-9]+\.[0-9]+"',
        f'tag: "v{version}", revision: "{revision}"',
        source,
        count=1,
    )
    if f'revision: "{revision}"' not in source:
        raise ValueError("The source formula tag could not be replaced.")
    output.mkdir(parents=True, exist_ok=True)
    (output / "claudex.rb").write_text(source, encoding="utf-8")
    (output / "claudex-bin.rb").write_text(formula, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate Homebrew formulae from release assets.")
    parser.add_argument("assets", type=Path)
    parser.add_argument("version")
    parser.add_argument("revision")
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    generate(args.assets, args.version, args.revision, args.output)


if __name__ == "__main__":
    main()
