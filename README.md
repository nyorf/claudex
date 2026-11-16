# Claudex

Claudex lets you run the Claude Code CLI with models available through an eligible ChatGPT Plus
or Pro plan. Claude Code manages the conversation, tools, permissions, and workspace. The
local gateway sends model requests to OpenAI using the ChatGPT profile you select.

## Install

Install [Claude Code](https://code.claude.com/docs/en/setup) separately. Claudex stores ChatGPT
credentials in macOS Keychain, Windows Credential Manager, or Linux Secret Service. On Linux,
run it in a D-Bus session with an unlocked keyring.

Use the installer from the latest [GitHub release](https://github.com/nyorf/claudex/releases/latest).
Its menu lets you install, update, or remove the bundle for your user account. The installer
checks the archive's SHA-256 before replacing an installation. Removing the bundle keeps your
saved profiles, model map, and Claude settings. If you configured plain Claude Code, run
`claudex unconfigure` first to restore its previous settings.
Stop the proxy before updating or removing the bundle.

On macOS or Linux, run this in your terminal.

```sh
/bin/bash -c "$(curl -fsSL https://github.com/nyorf/claudex/releases/latest/download/install.sh)"
```

On Windows x64, run this in PowerShell.

```powershell
$installer = Join-Path ([IO.Path]::GetTempPath()) "claudex-$([guid]::NewGuid()).ps1"; try { Invoke-WebRequest -UseBasicParsing -ErrorAction Stop https://github.com/nyorf/claudex/releases/latest/download/install.ps1 -OutFile $installer; powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installer } finally { Remove-Item -LiteralPath $installer -ErrorAction SilentlyContinue }
```

On macOS and Linux, the installer adds `~/.local/bin` to `PATH` for Bash, Zsh, Fish, and sh
when needed. Open a new shell and run `claudex --version`. To add the directory yourself in
Fish, run `fish_add_path ~/.local/bin`.

The CLI smoke tests use Claude Code 2.1.316. The native model picker and context
settings depend on the installed Claude Code version.

For manual installation, the release provides `.tar.gz` bundles for macOS and Linux on x64 and
ARM64, a Windows x64 `.zip`, and a Python wheel. Keep the executable (`claudex.exe` on Windows)
in its extracted directory with the bundled dependencies. The release workflow tests on
macOS 15, Ubuntu 24.04, and Windows Server 2025. The Linux bundles require glibc and will not
run on Alpine Linux. On other systems, use the wheel with Python 3.14.

```sh
uv tool install --python 3.14 ./claudex-1.0.0-py3-none-any.whl
```

## Install with Homebrew

Add the tap and install Claudex.

```sh
brew tap nyorf/claudex https://github.com/nyorf/claudex.git
brew install nyorf/claudex/claudex
```

Homebrew installs a prebuilt bottle when one matches your system. To build from source, use
Homebrew 7 or later and run `brew install --build-from-source nyorf/claudex/claudex`.
Both install `man claudex`.
Use `brew upgrade` to update a Homebrew installation. Run `claudex desktop` after upgrading
to refresh the Desktop credential helper if its path changed. See
[release checks](docs/operations/releasing.md) for how to update the tap from a release.

## Open Claude Code

From the project you want to work in, run `claudex cli`. It prompts for a ChatGPT profile
and opens browser sign-in when needed. It then starts or reuses the background proxy and
launches Claude Code. Claude options and subcommands pass through to the installed executable.

When Claudex opens a saved profile, macOS may ask whether Claudex can access
`org.claudex.chatgpt` in Keychain. Enter your `login` keychain password in that dialog. It
usually matches the password you use to sign in to your Mac. Choose **Allow** for this
request or **Always Allow** to save permission for this copy of Claudex.
See [macOS Keychain prompt](docs/configuration.md#macos-keychain-prompt) if the prompt returns
after an update or access fails.

```sh
claudex cli --resume
claudex cli --proxy-profile work -p "Summarize this project" --output-format json
```

Run `claudex cli --proxy-help` for Claudex options. Use `--proxy-model` or `--proxy-effort`
to choose proxy defaults, and `--model` or `--effort` to pass those options to Claude Code.
The launch applies gateway settings for that Claude Code process. To set up plain `claude`
instead, start `claudex serve`, run `claudex configure` with Claude Code closed, then run `claude`.
Use `claudex unconfigure` to restore the previous settings.

## Open Claude Desktop

On macOS, quit Claude Desktop if it is open, then run one command.

```sh
claudex desktop
```

Claudex prompts for sign-in when needed, starts or reuses the proxy, sets up the Desktop
profile, and opens the app with the proxy's context window. In **Code**, start a session on
**This computer** and pick a folder. Run `claudex desktop` again after a reboot.
Choose **Effort** in the Desktop model picker for any bundled GPT route. If the control is
missing after an update, see [Desktop setup](docs/configuration.md#claude-desktop).

On a Desktop version where direct setup is unavailable, use `claudex configure desktop --export`.
Import the generated file under **Developer → Configure Third-Party Inference**, apply the
changes, and relaunch Desktop.

## Manage the proxy

```sh
claudex
claudex serve
claudex serve --foreground
claudex stop
claudex update
```

Bare `claudex` and `claudex status` show whether the proxy is running and which profile is the
default. `serve` starts the proxy in the background. Use `--foreground` to keep it in the current
terminal. Stop the proxy before changing its profile or routing options. `update` uses the
release installer to fetch and install the latest published bundle. Source and Homebrew installs
use their original installation tools to update.

## Manage profiles

Use `claudex login work` to add another account or workspace. Run `claudex use work` to make
it the default for future launches. For one launch, pass `--profile work` to `serve` or `desktop`,
or `--proxy-profile work` to `cli`.
`claudex models --available` lists models for the selected profile. Each open gateway keeps
its original profile. Use `claudex logout personal` to clear credentials while keeping the
registration, or `claudex remove personal` to forget the profile.

## Map models

`claudex models` lists the model-map path and routes, including their active context windows
when the proxy is running. `claudex models --available` lists the account catalog's defaults.

### Bundled YAML

The bundled [`models.yaml`](https://github.com/nyorf/claudex/blob/master/src/claudex/config/models.yaml)
lists each GPT target, its aliases, its fallback context window, and the GPT reasoning effort
for each Claude effort level. The `default` route selects GPT-6.1 Sol at medium effort.
Run `claudex models --example` to print the map from your installed version. Save an edited
copy to the model-map path, or save it in the current directory and launch with
`claudex cli --proxy-map ./models.yaml`.

For each of the eight bundled models, Claudex uses a window of 872,000 tokens when the selected
ChatGPT account's catalog reports support. Otherwise it uses the catalog default. You can
override the window for a model and profile in the YAML. See
[Configuration](docs/configuration.md#model-map) for overrides and model switching.

The launcher prints the selected profile, GPT model, context window source, and effort.
In Claude Code, `/model` shows the configured labels and `/context` reports the selected
model's window. Claude Code displays token counts from completed model requests and estimates
any dollar amounts it shows. See your plan and app limits in
[ChatGPT Usage](https://chatgpt.com/settings/usage).

## Compatibility

Claudex translates the Anthropic Messages API to the public OpenAI Responses API. The ChatGPT
plan endpoint determines output length and supports structured outputs within its JSON Schema
subset. Claude Code can execute local MCP tools. Gateway tool search and Anthropic-hosted
features require additional provider support.

When you select Auto mode in Claude Code, it sends separate safety checks through Claudex.
Those requests use your ChatGPT plan, and Claude Code still decides whether a tool can run.
If a check returns no usable verdict, switch to Manual with Shift+Tab to review tool actions
yourself. See [how Claude Code handles classifier requests](https://code.claude.com/docs/en/auto-mode-classifier-billing).

The [release checks](docs/operations/releasing.md) cover the CLI and Claude Desktop Code tab.

## License

Claudex is licensed under [Apache 2.0](LICENSE). Run `claudex license` to print the full text.
