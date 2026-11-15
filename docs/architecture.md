---
title: Architecture
sidebar_position: 2
---

# Architecture

`claudex run` starts an authenticated gateway on loopback and launches the installed Claude
Code executable. Each launch pins an account profile and writes a temporary gateway credential
to a private settings file. Claudex shuts down the gateway and removes the file when the Claude
Code process exits. Managed Claude Code settings can take precedence.

`claudex serve` runs the same loopback gateway as its own process. It pins the profile and
model routes when it starts. Claudex keeps the local gateway credential in private state bound
to the port, the profile's registration, and the effective model configuration.
`claudex configure` installs the connection settings in Claude's user settings file. The
temporary file for one-off sessions is removed when the server stops.

Claude Code sends its transcript, tool schemas, and selected model to the gateway. Claudex
translates each request and forwards it to OpenAI with the selected profile’s access token.
Claude Code uses its own permission settings to execute local and MCP tool calls, then sends
the results in a subsequent request.

Claudex sends a streaming Responses request with storage disabled and keeps translation state
in memory. Claude Code stores transcripts, resumes sessions, and compacts conversations.
Compaction requests use the same model route and profile as the rest of the session.

Sign in with ChatGPT opens the system browser and returns to an ephemeral loopback callback.
Each profile uses its own OAuth registration and workspace grant. Claudex saves credentials in
the operating system credential store. If several launches share a profile, only one can
refresh its credentials at a time.

Project hooks and local MCP servers run with the permissions you give them. Treat the settings
and extensions you load in Claude Code as trusted code. The gateway sends OpenAI the prompt
and any file contents or tool results Claude Code includes.

See [Sign in with ChatGPT preview limits](https://developers.openai.com/siwc/token-sharing-open-source/preview-limitations)
and [Claude Code gateway behavior](https://code.claude.com/docs/en/llm-gateway-protocol).
