---
title: Release checks
sidebar_position: 4
---

# Release checks

## Verify Claude Code

Record the Claude Code version, operating system, model, window source, and outcome for each
check. Use a disposable workspace and a profile you are authorized to use for testing.

- Sign in through the browser with each test profile and check its model catalog. Switch
  profiles while Claude Code is open and start a second session. Verify each session uses its
  selected workspace. Exercise refresh and sign-out.
- Confirm the selected GPT model and explicit effort reach the public Responses API. Verify
  streaming, interruption, usage, and a rate-limit response.
- Run a file read and a shell tool with normal permission prompts. Exercise local MCP,
  subagents, agent teams, hooks, and skills. Verify tool call IDs link each result to its call.
- In a separate disposable workspace, exercise Auto mode through the actual ChatGPT profile.
  Verify a harmless shell command can run, and verify that an action forbidden in the
  conversation is blocked. A missing or unreadable classifier verdict must block the action.
- Resume a transcript. Continue the conversation after manual and automatic compaction.
  Send an image and verify structured output with a JSON Schema that both providers accept.
- Check that native `/context` and status-line context agree with the actual GPT window.
  Confirm the window from account metadata or a bounded upstream test. The offline context
  probe verifies how Claude Code displays the configured value. A successful model call
  verifies access to the model.
- Smoke-test the wheel and frozen archives on their target OS and CPU architecture. Verify
  sign-in against the native credential store on macOS, Linux, and Windows.

Run `make check` for local checks. Run `make context-smoke` to test the native Claude Code meter
with a synthetic 475,000-token window and zero inference calls. Run `make cli-smoke` to verify
that the installed Claude CLI executes a file read and sends its result back through the gateway
with synthetic OpenAI responses.

`make serve-smoke` starts the standalone gateway and uses synthetic OpenAI responses to
verify authentication, mixed model windows, and effort. It runs a separate Claude Code process with
the printed settings and then with user settings. It restarts the gateway on the same port
and checks that Claude Code still connects before restoring the original settings.

`make workflow-smoke` exercises local MCP, images in tool results, hooks, skills, a subagent,
permissions, structured output, session resume, and compaction with synthetic responses.

Verify agent teams in an interactive session. Ask Claude to start a named teammate and check
that the teammate completes a turn and its reply reaches the lead session.

```sh
CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1 claudex run -- --teammate-mode in-process
```

## Build release artifacts

Run `make wheel` to build the Python package and `make bundle` to build a native archive on the
current machine. On Windows, use `uv run --locked --group bundle python scripts/bundle.py`.
The release workflow runs on macOS and Linux for x64 and ARM64, and on Windows for x64. It
checks the extracted executables and exercises the installers against the built archives.

Run **Build release artifacts** manually to build and download the workflow artifacts. To
prepare a release, push a version tag that matches `pyproject.toml`, such as `v1.0.0`. After
the checks pass, the workflow creates a draft GitHub release with the bundles, Python
distributions, SHA-256 checksums, installer scripts, and Homebrew bottles.
The Intel macOS bottle packages the standalone bundle built in the same run. It includes
its Python runtime.

Review the draft and complete the release checks before publishing it. The one-line installers
use the latest published stable release. A draft remains unavailable to them until you publish it.
See [managing GitHub releases](https://docs.github.com/en/repositories/releasing-projects-on-github/managing-releases-in-a-repository).

Publishing a stable release starts **Update Homebrew formula**. Each run checks the latest
stable release's generated formula against its tag and the checksums of its four bottles.
It commits any changes to `Formula/claudex.rb` on `master`. If the workflow fails, use the
log to diagnose and fix the issue, then rerun it on `master` with
`gh workflow run homebrew.yml --ref master`.

After the formula update passes, verify that `brew install nyorf/claudex/claudex` installs
the published bottle on a supported platform. In a separate Homebrew installation, test
`brew install --build-from-source nyorf/claudex/claudex`. Run `claudex license` and `man claudex`
with each installation. An Intel macOS source build needs a Homebrew toolchain with
Python 3.14, OpenSSL 3, pkgconf, Rust, and uv. [Homebrew Intel support](https://docs.brew.sh/Support-Tiers#macos-on-intel)

## Verify the Desktop Code tab

Run `claudex configure desktop` on a supported macOS build with the app closed. In the Code
tab, start a **This computer** session, check the model list, and send a prompt through the
gateway. For each bundled route, choose a level under **Effort** and verify that the upstream
request uses the reasoning effort set in the route's YAML mapping. Switch through every
available model in that session and record each `/context` window and auto-compaction buffer.
Restart the proxy with the same options, quit Desktop, run `claudex desktop`, and repeat the
connection, effort, and context checks.

Verify the export path by importing the generated JSON under **Developer → Configure
Third-Party Inference**. See [Desktop gateway setup](https://code.claude.com/docs/en/llm-gateway-connect#desktop-app).
