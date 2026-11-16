class Claudex < Formula
  desc "Use ChatGPT models with Claude Code"
  homepage "https://github.com/nyorf/claudex"
  url "https://github.com/nyorf/claudex.git", tag: "v1.0.3", revision: "cff138da29136b4c75ad4c2353e2e0d79460fd57"
  head "https://github.com/nyorf/claudex.git"
  license "Apache-2.0"

  bottle do
    root_url "https://github.com/nyorf/claudex/releases/download/v1.0.3"
    sha256 cellar: :any_skip_relocation, arm64_linux: "aaeea074be2740578930a60869c28cf837c9c00b723f2e91ee5464346f5849bc"
    sha256 cellar: "/opt/homebrew/Cellar", arm64_sequoia: "dac7514fa81defeb5b55319b2c684fe442dfd91b88bef78c3df3843f6b7f0c23"
    sha256 cellar: :any_skip_relocation, sequoia: "98f6f9f919d6edfac93d0ba4f1b4a36df800630481a60b983ece46308f02a4ac"
    sha256 cellar: :any_skip_relocation, x86_64_linux: "3ce4e46a2ce832400a9dd4a0052f231349af550d0a84576640cd625f140caa27"
  end





  preserve_rpath if OS.mac? && Hardware::CPU.intel?

  on_macos do
    on_intel do
      depends_on "python@3.14" => :build
      depends_on "openssl@3" => :build
    end
    on_arm do
      depends_on "python@3.14"
      depends_on "openssl@3"
    end
  end
  on_linux do
    depends_on "python@3.14"
    depends_on "openssl@3"
  end
  depends_on "pkgconf" => :build
  depends_on "rust" => :build
  depends_on "uv" => :build

  def fetch
    ENV["UV_CACHE_DIR"] = (buildpath/"uv-cache").to_s
    ENV["UV_LINK_MODE"] = "copy"
    ENV["UV_PROJECT_ENVIRONMENT"] = (buildpath/"uv-fetch-env").to_s
    ENV["UV_PYTHON_DOWNLOADS"] = "never"
    ENV["OPENSSL_DIR"] = Formula["openssl@3"].opt_prefix.to_s
    groups = OS.mac? && Hardware::CPU.intel? ? ["--group", "bundle"] : []
    system "uv", "sync", "--locked", "--no-dev", "--no-editable", *groups, "--python",
           Formula["python@3.14"].opt_bin/"python3.14"
  end

  def install
    ENV["UV_CACHE_DIR"] = (buildpath/"uv-cache").to_s
    ENV["UV_LINK_MODE"] = "copy"
    ENV["UV_PYTHON_DOWNLOADS"] = "never"
    ENV["OPENSSL_DIR"] = Formula["openssl@3"].opt_prefix.to_s
    if OS.mac? && Hardware::CPU.intel?
      ENV["UV_PROJECT_ENVIRONMENT"] = (buildpath/"uv-fetch-env").to_s
      system "uv", "run", "--offline", "--locked", "--no-dev", "--no-editable", "--group", "bundle",
             "python", "scripts/bundle.py"
      system "tar", "-xzf", "dist/claudex-#{version}-darwin-x86_64.tar.gz"
      libexec.install (buildpath/"claudex").children
      bin.install_symlink libexec/"claudex"
      man1.install libexec/"claudex.1"
    else
      libexec.install "LICENSE", "README.md", "pyproject.toml", "uv.lock", "src", "man"
      ENV["UV_PROJECT_ENVIRONMENT"] = (libexec/".venv").to_s
      cd libexec do
        system "uv", "sync", "--offline", "--locked", "--no-dev", "--no-editable", "--python",
               Formula["python@3.14"].opt_bin/"python3.14"
      end
      bin.install_symlink libexec/".venv/bin/claudex"
      man1.install libexec/"man/claudex.1"
    end
  end

  test do
    assert_match "https://github.com/nyorf/claudex", shell_output("#{bin}/claudex --version")
    assert_match "claudex-opus", shell_output("#{bin}/claudex models --example")
    assert_equal (prefix/"LICENSE").read, shell_output("#{bin}/claudex license")
    assert (man1/"claudex.1").file? || (man1/"claudex.1.gz").file?
  end
end
