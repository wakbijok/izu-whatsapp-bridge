# izu-whatsapp-bridge

A WhatsApp gateway for **Claude Code**. Message a dedicated WhatsApp number and talk to a long-lived, headless Claude Code session: it keeps its context across messages, runs tools in your working directory, and tells you when background work finishes — without you having to ask.

Built on the [Claude Agent SDK](https://github.com/anthropics/claude-agent-sdk-python). WhatsApp connectivity comes from a vendored [Baileys](https://github.com/WhiskeySockets/Baileys) HTTP bridge taken from [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent) (MIT, see `vendor/whatsapp-bridge/LICENSE`). Claude Code is the default engine; a chat can also run on GPT through the OpenAI Codex CLI (ChatGPT subscription), per chat with `/engine`.

## Features

- **Persistent session per chat.** One Claude Code CLI per chat, resumed across restarts.
- **Per-chat engine.** `/engine gpt` moves a chat to the Codex CLI (one `codex exec` per turn, resumed by thread id; own `CODEX_HOME` outside the chat's folder, workspace-write sandbox, shell tool off, approvals never) and `/engine claude` moves it back; the recent conversation is handed over so it carries on. A profile can also start on Codex (`"engine": "codex"`).
- **Always-on stream.** The bridge reads Claude Code's output for the session's whole life, so turns Claude Code starts on its own (e.g. when a `run_in_background` task finishes) are delivered immediately, marked 🔔. Replies never lag one message behind.
- **Forward batching.** WhatsApp delivers a multi-select forward one message at a time; messages arriving within 2 s are merged into one prompt.
- **Message while it works.** A message sent mid-task is folded into the running turn instead of interrupting it; the answer to it is sent right away and the task carries on. `/stop` interrupts explicitly.
- **Exact attribution.** Every prompt carries a UUID that the CLI echoes back (`--replay-user-messages`) when it consumes it, so each result is matched to the turn that produced it and delivered exactly once, in order.
- **Watchdog.** Inactivity and hard-cap timeouts interrupt a stuck turn; a CLI that doesn't respond is restarted and unread messages are re-sent once.
- **Attachments.** Images, documents and voice notes are downloaded by the node bridge and handed to Claude Code as file paths.
- **Guest profiles.** Other numbers can get an isolated, guarded session with their own persona and memory, plus document and image tools (see below).
- **Allowlist with LID support.** Only listed phone numbers can use the bot, including WhatsApp's newer LID identities (resolved through the session's mapping files).

## Requirements

- Python 3.11+, Node.js 20+
- A WhatsApp number for the bot (a dedicated SIM is recommended; `self-chat` mode uses your own)
- Claude Code authentication: a subscription token (`claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN`) or an existing `claude` login. `ANTHROPIC_API_KEY` is deliberately ignored.

## Quick start

```bash
git clone <this repo> izu-whatsapp-bridge && cd izu-whatsapp-bridge
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env && chmod 600 .env     # set WA_ALLOWED_USERS
./scripts/pair.sh                          # scan the QR with the bot number's WhatsApp
.venv/bin/python bridge.py
```

To run it as a service, see `deploy/izu-whatsapp-bridge.service` (a systemd user unit). To re-pair after "logged out" / "device removed": stop the service, clear `data/wa-session/`, run `scripts/pair.sh`, and start it again.

## Configuration

Everything is environment variables. See [`.env.example`](.env.example). The main ones:

| Variable | Purpose |
|---|---|
| `WA_ALLOWED_USERS` | Allowed phone numbers, country code, no `+` (required) |
| `WA_MODE` | `bot` (dedicated number) or `self-chat` |
| `BRIDGE_CWD` | Claude Code working directory |
| `BRIDGE_MODEL` / `BRIDGE_EFFORT` | Model and thinking effort (also `/model`, `/effort` in chat) |
| `PERMISSION_MODE` | `default` auto-allows only read-only tools (no approval UI on WhatsApp); `bypassPermissions` allows everything |
| `BRIDGE_HARD_TIMEOUT` / `BRIDGE_IDLE_TIMEOUT` | Watchdog caps (seconds) |
| `BRIDGE_SYSTEM_PROMPT_FILE` | Your own persona / house rules (otherwise a generic default) |
| `BRIDGE_MCP_SERVERS_FILE` | Extra MCP servers as a JSON object |

Deployment-specific material (persona, MCP servers) belongs in `config/*.local.*` files, which git ignores. Point the env vars at them.

## Guest profiles, documents and images

`BRIDGE_PROFILES_FILE` (JSON, git-ignored) maps a phone number to a profile. A number without a profile gets the owner defaults from `.env`. A **guest** profile gives that person their own isolated Claude session:

- own persona (`persona_file`), working folder (`cwd`), permission mode and tool set;
- own MCP servers, e.g. a separate memory tenant, with `isolated: true` (no host-user settings, strict MCP config, no auto-memory/CLAUDE.md);
- an in-process **guard** the model cannot bypass: forbidden tools, reads outside `read_roots`, and WebFetch to private hosts are denied. The guard is a Claude Code PreToolUse hook, so it covers the **Claude Code engine only**; a guest on the Codex engine relies on Codex's own sandbox (workspace-write in its folder, shell tool off, only the profile's MCP servers plus the docs server);
- a guarded lane stays on its own engine (`engines` lists the ones `/engine` may pick; default: just `engine`) and only forwards the harmless Claude Code slashes `/clear /compact /context /recap` (`cc_slashes` overrides);
- files the guest sends are copied into `<files_dir or cwd>/received/` and the agent is given that copy, so `read_roots` never needs the bridge's shared media cache;
- `documents: true` adds `create_document` (Markdown → .docx/.pdf/.xlsx/.pptx/.md/.txt, sent as a WhatsApp attachment and kept in the folder), `send_file` and `list_files`;
- `images: true` adds `create_image`, which delegates to the OpenAI Codex CLI's built-in image generation (`codex exec … $imagegen`, needs a logged-in `codex` on the host) and sends the PNG; `BRIDGE_IMAGES_PER_DAY` caps it (default 12). Each run uses its own `CODEX_HOME` under the bridge's data dir (only an `auth.json` symlink, no MCP servers, approvals never, read-only sandbox, shell/view_image/apps/plugins/browser tools off), never the host user's Codex config. With bubblewrap (`bwrap`) installed the run is also filesystem-jailed: every home directory is hidden behind an empty tmpfs and only the codex install, that `CODEX_HOME`, `auth.json` and the lane's images folder are bound back. `BRIDGE_IMAGES_JAIL=require` (default; refuses to run without `bwrap`) | `auto` | `off`. **Residual risk:** Codex's built-in image tool runs through its code-mode host, so the run still has a code tool that can read what the jail exposes, including `auth.json`. A guest whose profile has `images: true` can therefore steer a run that holds that credential. Set `BRIDGE_IMAGES_AUTH_FILE` to a separate ChatGPT login's `auth.json` for image runs, and enable `images` only for guests you trust.

The owner's own number may also carry a profile with `documents`/`images` (and `files_dir`) to get the same tools without the sandbox. See `claude_wa/profiles.py` for the full schema.

## Chat commands

| Command | Action |
|---|---|
| `/help` | Command list |
| `/stop` | Interrupt the running task |
| `/reset` | Drop the session and start fresh |
| `/model fable\|sonnet\|opus\|haiku\|<id>` (on GPT: `sol\|astra`) | Switch model for the current engine |
| `/effort low\|medium\|high\|xhigh\|max` | Change thinking effort (context kept) |
| `/engine gpt\|claude` | Switch the chat's engine (Claude Code ↔ Codex/GPT), conversation carried over; guest lanes only if their profile's `engines` allows it |
| `/compact`, `/context`, `/usage`, `/code_review`, … | Forwarded to Claude Code (headless-proven allowlist; guest lanes: `/clear /compact /context /recap` unless `cc_slashes` says otherwise) |

## How it works

```
WhatsApp ─► Baileys node bridge (HTTP, localhost) ─► poll ─► coalesce 2s ─► Lane.outbox ─► writer ─► Claude Code CLI
WhatsApp ◄─ Baileys node bridge ◄─ Courier ◄─ stream routing ◄─ pump ◄───────────────────────────────────┘
```

The Python side starts the node bridge as a child process, polls it for messages, and sends replies through it. Each chat has a lane (ordered outbox, one writer, turn bookkeeping), each CLI has a pump that reads everything it emits, and each chat has a courier that sends replies in order. The same design is used by the Telegram sibling project, izu-telegram-bridge.

## Security notes

- Only `WA_ALLOWED_USERS` (and their LIDs) can talk to the bot; group chats are ignored.
- `bypassPermissions` gives the model unrestricted tool use on the host. Use it only on a machine and account you are prepared to hand to an agent.
- `data/wa-session/` holds the WhatsApp login. Treat it like a password: it is git-ignored and must stay private. Keep `.env` at mode 600.
- The node bridge listens on `127.0.0.1` only by default (`WA_BRIDGE_HOST`).

## Tests

```bash
.venv/bin/python -m pytest -q tests                       # Python (routing via a fake CLI stream)
(cd vendor/whatsapp-bridge && npm install && node --test)  # vendored node bridge
```

## License

[MIT](LICENSE). The vendored WhatsApp bridge is MIT © Nous Research.
