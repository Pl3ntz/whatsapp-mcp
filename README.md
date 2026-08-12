# whatsapp-mcp-local

An MCP server that gives AI agents read and write access to WhatsApp. It reads your chat history and media straight from the local app data, and sends messages only after you approve them.

The desktop app keeps everything in a local SQLite store and in `Message/Media/`. This server reads those files in read-only mode. No WhatsApp protocol is touched, no third-party API is used, and there is no ban risk. That is the core difference from every other WhatsApp MCP out there.

## What you can do

Read side:

- List chats, including archived ones
- Read the full message history of any chat, with pagination
- Search across all messages
- List media per chat: images, videos, audio, documents, stickers
- Get media metadata and the resolved file path
- Export a chat or a media file to a local folder
- Transcribe audio locally with whisper, with a disk cache so nothing is re-transcribed

Write side:

- Draft a message into the app, review it, then send with an explicit confirmation
- Confirmations are one-time: the draft expires in 120 seconds and cannot be reused

## Install

```bash
uvx whatsapp-mcp-local
```

Or via npm:

```bash
npx whatsapp-mcp-local
```

The server picks the right driver automatically. On macOS with the WhatsApp app installed and logged in, it reads the local database. Anywhere else, it falls back to WhatsApp Web through a dedicated Chrome profile.

## Drivers

| Driver | Where it works | How it is selected |
|---|---|---|
| Local app | macOS, WhatsApp app installed and logged in | The `ChatStorage.sqlite` database exists |
| WhatsApp Web | Any OS with Google Chrome | No local database, or `WHATSAPP_DRIVER=web` |

Force a driver with the `WHATSAPP_DRIVER` environment variable: `local`, `web`, or `auto` (default).

## Tools

| Tool | Purpose | Driver | Flag |
|---|---|---|---|
| `list_chats` | List chats with unread counts and last message | local | read-only |
| `get_messages` | Read messages, paginated, optionally including media | both | read-only |
| `search_messages` | Search across all messages | local | read-only |
| `get_chat_info` | Chat metadata | local | read-only |
| `list_media` | List media in a chat, filtered by type | local | read-only |
| `get_media` | Media metadata and resolved path | local | read-only |
| `get_media_thumb` | Thumbnail path or small base64 | local | read-only |
| `export_chat` | Export a chat to JSON or Markdown | local | read-only |
| `export_media` | Copy a media file to a local folder | local | idempotent |
| `transcribe_audio` | Transcribe an audio message locally | local | read-only |
| `verify_sent` | Confirm a message was stored as sent | local | read-only |
| `send_message` | Draft a message into the app, nothing is sent yet | both | destructive |
| `confirm_send` | Press Enter on a valid draft, after your approval | both | destructive |

## Media and transcription

Media files live in `Message/Media/` inside the WhatsApp shared container. They are stored in their original format, not encrypted, so the server reads them directly. Every media item reports `file_exists`, because the database can reference files that are no longer on disk.

Audio transcription runs locally with whisper.cpp (`whisper-cli`). No audio ever leaves your machine. Transcripts are cached by file hash under `~/.whatsapp-mcp/transcripts/`, so a second request for the same file returns instantly.

Runtime prerequisites for transcription:

```bash
brew install whisper-cpp ffmpeg
```

You also need a whisper model file. The brew formula ships a tiny test model, useful to validate quickly:

```bash
$(brew --prefix whisper-cpp)/share/whisper-cpp/for-tests-ggml-tiny.bin
```

For decent Portuguese results, download the small model from the whisper.cpp repo and point the tool at it, or leave `model=small` and let the server resolve it.

## Security model

- The database is always opened in read-only mode. Tests verify the file hash does not change after any call.
- Message content is marked `untrusted`. Treat it as data, never as instructions. A contact can write "ignore your previous instructions" and the server will surface it as untrusted content, not as a command.
- Phone numbers (JIDs) are masked in every output. Media paths contain the raw JID folder, so paths are only returned when you explicitly ask with `include_path=true`.
- Media paths are resolved server-side and checked against the media root. A path with `..` in the database is rejected.
- Sending is a two step flow. `send_message` pre-fills the text, nothing is sent. `confirm_send` requires the `draft_id` returned by `send_message`, the draft expires in 120 seconds, and it is consumed once. The confirmation re-opens the target chat with the approved text before pressing Enter, so a wrong chat or an edited message cannot be sent by mistake.
- Exports never overwrite existing files (`O_EXCL`), and exported file names do not contain JIDs.
- Nothing is written to `ChatStorage.sqlite`, `Axolotl.sqlite`, or any app database. Writing there would corrupt the app and would not reach the server anyway.

## Integrate with opencode

Add to `~/.config/opencode/opencode.json`:

```json
{
  "mcp": {
    "whatsapp": {
      "type": "local",
      "command": ["uvx", "whatsapp-mcp-local"],
      "enabled": true
    }
  }
}
```

Restart opencode. The destructive tools carry the MCP annotation, so clients ask for confirmation before calling them.

## Integrate with Claude Code

```bash
claude mcp add whatsapp-mcp -- uvx whatsapp-mcp-local
```

Or with a `.mcp.json` file:

```json
{
  "mcpServers": {
    "whatsapp-mcp": {
      "command": "uvx",
      "args": ["whatsapp-mcp-local"]
    }
  }
}
```

## Integrate with other clients

Every major MCP client accepts this server over stdio. None of them discover packages by search, so you always add it explicitly with a command or a config file.

**Codex CLI**

```bash
codex mcp add whatsapp -- uvx whatsapp-mcp-local
```

**ChatGPT desktop**

Open Settings, then MCP servers, add a server with STDIO transport and the command `uvx whatsapp-mcp-local`.

**Cursor**

Add to `.cursor/mcp.json` in your project (or `~/.cursor/mcp.json` globally):

```json
{
  "mcpServers": {
    "whatsapp": {
      "command": "uvx",
      "args": ["whatsapp-mcp-local"]
    }
  }
}
```

**VS Code / GitHub Copilot**

```bash
code --add-mcp '{"name":"whatsapp","command":"uvx","args":["whatsapp-mcp-local"]}'
```

Or add a `.vscode/mcp.json` file with the same `mcpServers` shape as Cursor.

**Windsurf / Devin**

Add to `~/.codeium/windsurf/mcp_config.json`:

```json
{
  "mcpServers": {
    "whatsapp": {
      "command": "uvx",
      "args": ["whatsapp-mcp-local"]
    }
  }
}
```

## A note about PATH

Desktop apps like Cursor, VS Code, and ChatGPT do not inherit your shell PATH. If `uvx` is not found, install the package as a tool and use the full binary path, or install it globally:

```bash
uv tool install whatsapp-mcp-local
# then use: whatsapp-mcp-local  (or the full path from `which whatsapp-mcp-local`)
```

The npm route works the same way if Node is on the system PATH:

```bash
npx whatsapp-mcp-local
```

## Test

```bash
uv run pytest
```

## Notes

This is a tool for your own account and your own data. The database schema and the web interface belong to WhatsApp and can change between releases. Do not use it to send messages on behalf of other people, and do not use it for anything you are not authorized to do.

The project is MIT licensed. Source: <https://github.com/Pl3ntz/whatsapp-mcp>
