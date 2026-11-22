---
title: Configuration
sidebar_position: 3
---

# Configuration

## macOS Keychain prompt

Claudex stores your ChatGPT sign-in credentials under `org.claudex.chatgpt` in Keychain.
macOS may ask for permission when Claudex opens a saved profile. Enter your `login` keychain
password in the macOS dialog. It usually matches the password you use to sign in to your Mac.
If the passwords differ, use the `login` keychain password.
[Apple’s guide to the keychain password](https://support.apple.com/en-gb/guide/keychain-access/kyca2429/mac).

- **Allow** grants access for this request only.
- **Always Allow** lets this copy of Claudex access this item again without prompting.
- **Deny** blocks this request.

See [Apple’s guide to allowing app access](https://support.apple.com/en-gb/guide/mac-help/kychn002/26/mac/26).

macOS may ask again after a Claudex update, including one installed through Homebrew.
Approve the prompt to reuse the saved credential for your existing profile.
[Apple’s guide to app access after an update](https://support.apple.com/guide/keychain-access/if-a-trusted-app-asks-for-keychain-access-kyca1331/mac).

If access fails, retry the Claudex command in a terminal in your macOS desktop session and
approve the prompt. If the `login` keychain is locked, unlock it in Keychain Access and retry.

## Model map

Select `--model gpt-6.2-sol` to use the GPT name or `--model opus` to use the bundled
Opus alias. Both select the same GPT model with the bundled map.

Run `claudex models --example` to print the bundled map. The `default` field names the route
used when you omit `--model`. Configure each route under `routes` with these fields.

| Field | Meaning |
| --- | --- |
| `label` | Name shown in Claude Code alongside the GPT model ID |
| `aliases` | Additional names accepted by the Claudex launcher |
| `model` | GPT model slug returned by `claudex models --available` |
| `context_window` | Fallback token window when the catalog lacks a default and cannot establish support for 872,000 tokens |
| `context_window_overrides` | Optional map from saved profile names to verified token windows |
| `efforts` | Mapping from explicit Claude effort to GPT reasoning effort |
| `claude_default_effort` | Claude effort selected at launch when you omit `--effort` |
| `default_effort` | GPT reasoning effort when Claude omits effort |

Use route IDs starting with `claudex-`, such as `claudex-opus`. Put built-in Claude model IDs
such as `claude-opus-5-5` in the `aliases` field.

Saved YAML maps take precedence over the bundled map. To adopt a new bundled GPT target in a
custom map, copy the route's `model`, `label`, and GPT-name alias from `claudex models --example`.

Claudex selects a context window for each mapped model from the selected ChatGPT account's
catalog. If either the default or a valid maximum reaches 872,000 tokens, Claudex uses 872,000.
Otherwise it uses the catalog default. If the catalog gives no default, Claudex falls back to
`context_window` from the YAML.
Run `claudex models` while the proxy is running to see the windows in use.
`claudex models --available` shows the defaults reported by the account catalog.

A profile override takes precedence over automatic selection. To set Astra's window to
872,000 tokens for `personal`, add the following under `routes.claudex-fable`.

```yaml
context_window_overrides:
  personal: 872000
```

The override must fit within a valid maximum from the profile's catalog. Claudex reports an
error if the maximum is missing, below the catalog default, or lower than the override. Remove the
override to let Claudex select the window automatically. If you reuse a profile name for another
account, verify its window before reusing the override.

The picker lists all mapped routes available to the selected profile. Claudex sets a window
per model, so `/context` and automatic compaction follow your selection when you switch.
Claude Code accepts per-model windows from 100,000 through 1,000,000 tokens. If a smaller
window falls outside that range, use `--map` to start with a compatible set of routes.
For a conversation already beyond the next model's window, run `/compact` before switching.
For `opus`, `sonnet`, `haiku`, and `fable`, the launcher uses the corresponding
`claudex-` route when it is available in the launch.

Use `claudex cli --model claude-fable-5-2` to start a session with the mapped model and context
window. The launcher resolves the full Claude ID to its mapped route before Claude Code opens.
`/context` and automatic compaction follow that route's window.

You can also use full IDs in a CLI workflow's `model` field or with
`claude --model claude-fable-5-2` through the configured gateway. For older IDs with the
version before the family name, add the full ID to the route's `aliases` list. For configured
full IDs, Claude Code uses the mapped window up to its built-in limit for that ID. Use a short
name or the mapped route ID in workflows to use the full mapped window on any supported
Claude Code version.

## Standalone gateway

`claudex serve` starts a background proxy on `127.0.0.1:8765`. Use `--foreground` to run it
in the current terminal. `claudex stop` stops the proxy, and `claudex status` shows its state,
profile, and the default profile for new launches. Use the same `--port` for a custom port.
Only one proxy can run at a time for a given Claudex state directory.
`claudex cli` uses `--proxy-port` for this option because it sends native Claude flags to Claude.
Without `--model`, it uses the YAML model map's `default` route. Without `--effort`, it uses
that route's `claude_default_effort`. The bundled map defaults to GPT-6.2 Sol at medium effort.
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

When an update changes a route or context window, stop the running proxy and start it with
`claudex serve --profile PROFILE`, using your existing profile name.
Pass your previous `--model`, `--effort`, and `--map` options if you set them.
For plain `claude`, rerun `claudex configure` with Claude Code closed. For Desktop, quit the app
and run `claudex desktop` to use the updated settings. Pass the same `--port` to each Claudex
command if you use a custom port.

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
Claudex fills the model picker with the running proxy's available routes. The Code tab runs a
helper to read the saved gateway credential. If you move Claudex or change the gateway options,
the launcher refreshes the profile.

Claudex configures Desktop's short model names `opus`, `sonnet`, `haiku`, and `fable` to use
GPT-6.2 Sol, GPT-6.1 Terra, GPT-6 Luna, and GPT-6.2 Astra, respectively. Use these names for
workflow agents that should follow a Claude family across versions. Desktop may substitute
another model for a literal ID outside its list before the request reaches Claudex. When
Claudex receives a versioned Claude ID, it routes the request to that family's default.

Use the exact ID in this table to select a specific GPT model in a Desktop workflow.

| GPT model | Desktop workflow `model` |
| --- | --- |
| GPT-6.2 Sol | `claude-opus-5-5` |
| GPT-6.2 Astra | `claude-fable-5-2` |
| GPT-6.1 Terra | `claude-sonnet-5-5` |
| GPT-6 Sol | `claudex-sonnet-5` |
| GPT-6 Luna | `claude-haiku-5-5` |
| GPT-5.6 Sol | `claudex-opus-5` |
| GPT-5.6 Terra | `claudex-terra-5-6` |
| GPT-5.6 Luna | `claudex-luna-5-6` |

Each bundled route offers **Effort** in Desktop's model picker. Claudex translates the selected
effort through the route's YAML mapping before it sends the request to the GPT model. For a
custom route whose mapping includes every level from low through its highest supported level,
Claudex uses `behavesAs` to show Claude Sonnet 5's effort controls and caps the picker at the
highest mapped level. When the mapping has gaps, Desktop uses its default controls for that
route. After an update that changes routes, quit Desktop and follow the
[standalone gateway restart steps](#standalone-gateway) to refresh the picker.
[Desktop effort configuration](https://claude.com/docs/third-party/claude-desktop/models).

To reopen Desktop, run `claudex desktop`. It reuses the matching proxy when it is running.
Otherwise, it starts the proxy in the background with the saved profile, model, effort, and
model map, then opens the app once the proxy is ready. If background startup fails, run
`claudex serve --foreground --saved` to see the error using the same launch options.
Pass the same `--port` to `serve`, `configure desktop`, and `desktop` when using a custom port.
In Code, start sessions with **This computer** and choose your project folder as usual. If
Desktop remembers a different environment, switch back to **This computer** once.

The launcher uses the smallest context window published by the proxy for all local Code
sessions in that app process. The shared window must be between 100,000 and 1,000,000 tokens.
If all eight routes use a window of 872,000 tokens, `/context` shows 872k when you switch
models. With auto-compaction enabled, Claude Code uses its normal reserve. Quit Desktop before
running the launcher so the app can pick up the window.
A regular Dock launch uses Desktop's window for gateway aliases. See
[Desktop gateway context](https://claude.com/docs/third-party/claude-desktop/gateway#context-window-and-compaction).

Direct setup checks the Claude Desktop installation and validates existing local profile metadata
before applying changes. Organization-managed settings take precedence. To import the profile
manually, run `claudex configure desktop --export`. Import the generated JSON via
**Developer → Configure Third-Party Inference**, apply the changes, and relaunch Desktop. If
the Developer menu is hidden, use **Help → Troubleshooting → Enable Developer Mode**. To
switch back, apply your previous profile or choose Claude.ai sign-in. See the
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
