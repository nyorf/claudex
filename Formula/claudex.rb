class Claudex < Formula
  desc "Use ChatGPT models with Claude Code"
  homepage "https://github.com/nyorf/claudex"
  url "https://github.com/nyorf/claudex.git", tag: "v1.0.0"
  head "https://github.com/nyorf/claudex.git"
  license "Apache-2.0"

  depends_on "python@3.14"
  depends_on "openssl@3"
  depends_on "pkgconf" => :build
  depends_on "rust" => :build
  depends_on "uv" => :build

  conflicts_with "claudex-bin", because: "both install the claudex command"

  def install
    libexec.install "LICENSE", "README.md", "pyproject.toml", "uv.lock", "src", "man"
    ENV["UV_PROJECT_ENVIRONMENT"] = (libexec/".venv").to_s
    ENV["UV_CACHE_DIR"] = (buildpath/"uv-cache").to_s
    ENV["UV_PYTHON_DOWNLOADS"] = "never"
    ENV["OPENSSL_DIR"] = Formula["openssl@3"].opt_prefix.to_s
    cd libexec do
      system "uv", "sync", "--locked", "--no-dev", "--no-editable", "--python",
             Formula["python@3.14"].opt_bin/"python3.14"
    end
    bin.install_symlink libexec/".venv/bin/claudex"
    man1.install libexec/"man/claudex.1"
  end

  test do
    assert_match "https://github.com/nyorf/claudex", shell_output("#{bin}/claudex --version")
    assert_match "claudex-opus", shell_output("#{bin}/claudex models --example")
  end
end
