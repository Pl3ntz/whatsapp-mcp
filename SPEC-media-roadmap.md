# SPEC: whatsapp-mcp-local v0.5 — Local Media, Audio Transcription

## What

This server gives an AI agent read and write access to WhatsApp, entirely from local files. It reads chat history, media, and transcripts from the WhatsApp app data on this machine, and it sends messages only through an explicit confirmation gate.

## Why

`get_messages` used to filter media out. Any message without text was invisible to the agent, which meant more than 93,000 media events were never surfaced. The local disk holds 32,571 media files (about 4.9 GB) in plain format, including 1,316 audio messages. The goal is simple: if the data is on this machine, the agent should be able to read it.

## Verified facts

- Database: `ChatStorage.sqlite`, always opened in read-only mode.
- `ZWAMEDIAITEM` holds 802,076 rows. Only 2,569 have a local path. The rest are orphans or cloud references.
- Physical media lives under `Message/Media/<jid>/<a>/<b>/<hash>.<ext>`. The database column stores `Media/<jid>/...`, so the resolver prepends `Message/`.
- Message type mapping, confirmed by real file extensions:
  - 1 and 42: image (jpg)
  - 2 and 11: video (mp4)
  - 3: audio (opus, m4a)
  - 8: document (pdf)
  - 15: sticker (webp, older `.was`)
  - 0: text
- Magic bytes confirmed for each format. Files on disk are not encrypted. `ZMEDIAKEY` is never touched.
- Runtime prerequisites: `whisper-cpp` and `ffmpeg` via Homebrew, plus a whisper model file. `whisper-cli` decodes flac, mp3, ogg, and wav directly; m4a and `.was` are converted to wav 16k mono with ffmpeg first.

## Tools added in v0.3 through v0.5

| Tool | Input | Output | Flag | Version |
|---|---|---|---|---|
| `list_media` | chat_id, media_type?, limit (max 500), before | metadata + file_exists, no paths | read-only | 0.3.0 |
| `get_media` | chat_id, media_id, include_path? | metadata, path only when asked | read-only | 0.3.0 |
| `get_messages` | + include_media? | media items with media_id and marker | read-only | 0.3.0 |
| `transcribe_audio` | media_id, model?, language? | text, segments, cache_hit | read-only, writes cache | 0.4.0 |
| `export_media` | media_id, dest_dir?, overwrite? | sanitized path without JID | idempotent | 0.5.0 |
| `get_media_thumb` | media_id, as_base64? | thumb path or small base64 | read-only | 0.5.0 |

## Security model

- Paths are resolved server-side and checked against the media root. A `..` entry in the database is rejected.
- Media content and captions are marked `untrusted`.
- JIDs never appear in outputs or in exported file names.
- Exports use `O_EXCL`, so existing files are never overwritten.
- Transcription runs in a subprocess with a 180 second timeout. A whisper crash does not take the server down.
- Transcripts are cached by file hash under `~/.whatsapp-mcp/transcripts/`.
- The database stays read-only. Tests confirm the hash does not change.

## Out of scope

- Decrypting anything. Files on disk are already plain. `ZMEDIAKEY` is not used.
- Video transcoding. Videos expose metadata, a thumbnail, and an audio track for transcription, nothing more.
- Sending media. The write path remains text only, behind the confirmation gate.
- Files that do not exist locally. They are reported with `file_exists=false` and skipped.

## Releases

- 0.3.0: media reading (list_media, get_media, include_media)
- 0.4.0: local audio transcription with cache
- 0.5.0: export, thumbnails, backfill helper

## Open items

- Backfill: run `backfill_transcripts()` in batch over the 1,316 audio files once a real model is downloaded.
- Whisper model: none is installed yet. The brew tiny test model works for a quick check; `small` is better for Portuguese.
- Re-audit: a security pass over the media tools is scheduled before the next release.
