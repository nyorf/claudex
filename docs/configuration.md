---
title: Configuration
sidebar_position: 3
---

# Configuration

## Model map

Select `--model gpt-6.1-sol` to use the GPT name or `--model opus` to use the bundled
Opus alias. Both select the same GPT model with the bundled map.

Run `claudex models --example` to print the bundled map. The `default` field names the route
used when you omit `--model`. Configure each route under `routes` with these fields.

| Field | Meaning |
| --- | --- |
| `label` | Name shown in Claude Code alongside the GPT model ID |
| `aliases` | Additional names accepted by the Claudex launcher |
| `model` | GPT model slug returned by `claudex models --available` |
| `context_window` | Declared token window for the model when the account catalog provides no window |
| `context_window_overrides` | Optional map from saved profile names to verified token windows |
| `efforts` | Mapping from explicit Claude effort to GPT reasoning effort |
| `claude_default_effort` | Claude effort selected at launch when you omit `--effort` |
| `default_effort` | GPT reasoning effort when Claude omits effort |

Use route IDs starting with `claudex-`, such as `claudex-opus`. Put built-in Claude model IDs
such as `claude-opus-5-5` in the `aliases` field. Use the window verified for the selected
upstream route and profile. The bundled 500,000-token example comes from the published GPT-6.1
Sol default in Codex and ChatGPT Work. The direct Sign in with ChatGPT endpoint may have a
different effective window. By default, Claudex uses the account catalog's reported window.

To use a larger window you have verified for a profile, add a profile-specific override to
that model's route in your local map. For example, add this under `routes.claudex-fable`
if Astra supports the larger window on your `personal` profile.

```yaml
context_window_overrides:
  personal: 872000
```

The override must fit within the maximum reported by that profile's account catalog.
Claudex reports an error if the maximum is missing or lower than the override. Remove the
override to use the catalog's default window. If you reuse a profile name for another
account, verify its window before reusing the override.

The picker lists all mapped routes available to the selected profile. Claudex sets a window
per model, so `/context` and automatic compaction follow your selection when you switch.
Claude Code accepts per-model windows from 100,000 through 1,000,000 tokens. If a smaller
window falls outside that range, use `--map` to start with a compatible set of routes.
For a conversation already beyond the next model's window, run `/compact` before switching.
For `opus`, `sonnet`, `haiku`, and `fable`, the launcher uses the corresponding
`claudex-` route when it is available in the launch.

## Standalone gateway

`claudex serve` starts a background proxy on `127.0.0.1:8765`. Use `--foreground` to run it
in the current terminal. `claudex stop` stops the proxy, and `claudex status` shows its state,
profile, and the default profile for new launches. Use the same `--port` for a custom port.
Only one proxy can run at a time for a given Claudex state directory.
`claudex cli` uses `--proxy-port` for this option because it sends native Claude flags to Claude.
Without `--model`, it uses the YAML model map's `default` route. Without `--effort`, it uses
that route's `claude_default_effort`. The bundled map defaults to GPT-6.1 Sol at medium effort.
If this port has saved launch options and they match the default profile, Claudex reuses them.
Pass an explicit profile or route option to change them after stopping the proxy. Use
`claudex serve --foreground --saved` to diagnose startup with the same launch options.

After `serve` has started once, `claudex configure` applies its URL, local credential,
context window, model picker, and effort defaults to your Claude user settings. It uses
`~/.claude/settings.json` or the file under `CLAUDE_CONFIG_DIR`. The proxy accepts the same
local credential when restarted on the same port with the same ChatGPT registration and model
configuration. Changing the registration or effective model configuration replaces the
credential, so existing clients cannot use the new routes until you reconfigure.

Wait for Claudex to print its URL before using `claude`. Close Claude Code before running
`configure` or `unconfigure`. Rerun `configure` after starting the proxy with a different
port, profile, model map, context window, or effort. Project and managed settings can
override user settings.
Use `/status` to check the API base URL in projects with their own provider settings.
`claudex unconfigure` restores the settings Claudex applied and keeps unrelated changes.
If it reports a conflict, restore the configured value for the named entry before retrying.

With `--effort max`, Claude Code applies max effort across the session. Each selected route
must have a mapping for that effort.

The settings use `ANTHROPIC_BASE_URL` and `ANTHROPIC_AUTH_TOKEN`. Keep these private files
out of project settings and version control. See the
[Claude Code gateway settings](https://code.claude.com/docs/en/llm-gateway-connect).

Claudex configures the CLI to send separate Auto mode classifier requests through the gateway.
The usual classifier model is the configured Sonnet route. Claude Code can fall back to the
session model. These checks use your ChatGPT plan. If you update an existing installation,
restart the proxy and rerun `claudex configure` with Claude Code closed to refresh its settings.

## Claude Desktop

On macOS, quit Claude Desktop and run `claudex desktop`. It prompts for ChatGPT sign-in if needed,
starts or reuses the proxy, and sets up the local gateway profile before opening Desktop.
The Code tab loads the model list from the gateway and runs a helper to read the saved gateway
credential. If you move Claudex or change the gateway options, the launcher refreshes the profile.

To reopen Desktop, run `claudex desktop`. It reuses the matching proxy when it is running.
Otherwise, it starts the proxy in the background with the saved profile, model, effort, and
model map, then opens the app once the proxy is ready. If background startup fails, run
`claudex serve --foreground --saved` to see the error using the same launch options.
Pass the same `--port` to `serve`, `configure desktop`, and `desktop` when using a custom port.
In Code, start sessions with **This computer** and choose your project folder as usual. If
Desktop remembers a different environment, switch back to **This computer** once.

The launcher uses the smallest context window published by the proxy for all local Code
sessions in that app process. The shared window must be between 100,000 and 1,000,000 tokens.
If all eight routes support a window of 872,000 tokens, `/context` shows 872k when you switch
models. With auto-compaction enabled, Claude Code uses its normal reserve. Quit Desktop before
running the launcher so the app can pick up the window.
A regular Dock launch uses Desktop's window for gateway aliases. See
[Desktop gateway context](https://claude.com/docs/third-party/claude-desktop/gateway#context-window-and-compaction).

Direct setup supports local profiles on Claude Desktop build `5.11492.5`. Organization-managed
settings take precedence. If your build requires manual import, run
`claudex configure desktop --export`. Import the generated JSON via **Developer → Configure
Third-Party Inference**, apply the changes, and relaunch Desktop. If the Developer menu is
hidden, use **Help → Troubleshooting → Enable Developer Mode**. To switch back, apply your
previous profile or choose Claude.ai sign-in. See the
[Claude Desktop configuration guide](https://claude.com/docs/third-party/claude-desktop/in-app-configuration).

If you import the profile manually, you can set `CLAUDE_CODE_MAX_CONTEXT_TOKENS` in a saved
local Code environment. Set it to a window all available routes support. See
[local environment variables](https://code.claude.com/docs/en/desktop#set-environment-variables-for-local-sessions).

## Local paths

| Variable | Meaning |
| --- | --- |
| `CLAUDEX_CONFIG_DIR` | Directory containing the default `models.yaml` |
| `CLAUDEX_STATE_DIR` | Directory for the profile index and lock files |
| `CLAUDEX_CLAUDE_BIN` | Claude Code executable, normally `claude` on your path |

The operating system supplies the default config and state paths. `claudex models` prints the
model map path. Set gateway and model options through Claudex for these sessions.
