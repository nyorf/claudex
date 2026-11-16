class Claudex < Formula
  desc "Use ChatGPT models with Claude Code"
  homepage "https://github.com/nyorf/claudex"
  url "https://github.com/nyorf/claudex.git", tag: "v1.0.5", revision: "0aa5cf68435744355b3c26af541dd1cc7165a529"
  head "https://github.com/nyorf/claudex.git"
  license "Apache-2.0"

  bottle do
    root_url "https://github.com/nyorf/claudex/releases/download/v1.0.5"
    sha256 cellar: :any_skip_relocation, arm64_linux: "3e651e8fb2075d5b66a5a866f0a89899828482205fbb432e4bd2c3e15d0195e4"
    sha256 cellar: "/opt/homebrew/Cellar", arm64_sequoia: "5aea946c15f84c2972f675ece78e06544c006d697aeeeea95f6e6174b480d522"
    sha256 cellar: :any_skip_relocation, sequoia: "2da9ef8551fc7fe6fd2e49e1d86f559fd01c5d9eb907db7a19a1ff86c6a3c02a"
    sha256 cellar: :any_skip_relocation, x86_64_linux: "937319b579e2046ddd08a4ef65460c8e4d4fffb1f93e29747d929ea1ddbc9456"
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
