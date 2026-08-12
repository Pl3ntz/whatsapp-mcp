"""Testes de mídia (F2/F3): transcribe_audio (mock do subprocess), export_media,
get_media_thumb, backfill_transcripts."""

import base64
import json
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from whatsapp_mcp import media, reader
from tests.fixture import build_fixture


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    db = tmp_path / "ChatStorage.sqlite"
    build_fixture(db, media_root=tmp_path)
    monkeypatch.setattr(reader, "MEDIA_ROOT", tmp_path / "Message" / "Media")
    monkeypatch.setattr(media, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(media, "DEFAULT_EXPORT_DIR", tmp_path / "exports")
    monkeypatch.setenv("TMPDIR", str(tmp_path / "tmp"))
    (tmp_path / "tmp").mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(db=db, tmp=tmp_path, media_root=tmp_path / "Message" / "Media")


@pytest.fixture()
def whisper_cli(monkeypatch, ctx):
    """Fake whisper-cli no PATH + fake subprocess.run que grava o JSON esperado."""
    monkeypatch.setattr(media.shutil, "which", lambda name: "/usr/bin/whisper-cli" if name == "whisper-cli" else None)
    # FIX: _resolve_whisper_model mockado para não chamar brew --prefix nos testes
    monkeypatch.setattr(media, "_resolve_whisper_model", lambda model, cli: "/fake/models/ggml-small.bin")
    calls = []

    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        calls.append(cmd)
        prefix = cmd[-1]
        out = {
            "transcription": [
                {"timestamps": {"from": "00:00:00,000", "to": "00:00:03,000"},
                 "offsets": {"from": 0, "to": 3000},
                 "text": "olá mundo teste"},
                {"timestamps": {"from": "00:00:03,000", "to": "00:00:05,000"},
                 "offsets": {"from": 3000, "to": 5000},
                 "text": "segunda parte"},
            ],
            "result": {"language": "pt"},
        }
        Path(f"{prefix}.json").write_text(json.dumps(out), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(media.subprocess, "run", fake_run)
    return calls


def test_transcribe_miss_runs_whisper_and_caches(ctx, whisper_cli):
    res = media.transcribe_audio(2, db_path=ctx.db)
    assert "error" not in res
    assert res["cache_hit"] is False
    assert res["text"] == "olá mundo teste segunda parte"
    assert res["duration"] == 5.0
    assert res["segments"][0]["from_ms"] == 0
    assert res["language"] == "pt"
    assert res["engine"] == "whisper.cpp"
    assert res["untrusted"] is True
    assert len(whisper_cli) == 1
    cmd = whisper_cli[0]
    assert cmd[:2] == ["/usr/bin/whisper-cli", "-m"]
    assert "-oj" in cmd and "-of" in cmd
    # cache gravado e 2ª chamada não roda subprocess
    res2 = media.transcribe_audio(2, db_path=ctx.db)
    assert res2["cache_hit"] is True
    assert len(whisper_cli) == 1


def test_transcribe_cache_hit_without_binary(ctx, monkeypatch):
    """Com cache presente, nem precisa de whisper-cli no PATH."""
    monkeypatch.setattr(media.shutil, "which", lambda name: None)
    # calcula hash do arquivo e planta o cache
    p = ctx.media_root / "554991539437@s.whatsapp.net" / "0" / "1" / "audio01.ogg"
    file_hash = media._sha256_file(p)
    cache = media.CACHE_DIR / f"{file_hash}.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps({"text": "transcrito antes", "segments": [], "duration": 1.0,
                                 "engine": "whisper.cpp", "model": "small", "created_at": 1}))
    res = media.transcribe_audio(2, db_path=ctx.db)
    assert res["cache_hit"] is True
    assert res["text"] == "transcrito antes"


def test_transcribe_cli_missing(ctx, monkeypatch):
    monkeypatch.setattr(media.shutil, "which", lambda name: None)
    res = media.transcribe_audio(2, db_path=ctx.db)
    assert "error" in res
    assert "brew install whisper-cpp" in res["error"]


def test_transcribe_language_flag(ctx, whisper_cli):
    media.transcribe_audio(2, language="pt", db_path=ctx.db)
    assert "-l" in whisper_cli[0]
    assert "pt" in whisper_cli[0]


def test_transcribe_media_not_found(ctx, whisper_cli):
    res = media.transcribe_audio(999, db_path=ctx.db)
    assert "error" in res
    assert whisper_cli == []


def test_transcribe_missing_file(ctx, whisper_cli):
    # media 3 = ghost.jpg (não existe em disco)
    res = media.transcribe_audio(3, db_path=ctx.db)
    assert "error" in res
    assert "não existe" in res["error"]
    assert whisper_cli == []


def test_transcribe_rejects_non_audio_extension(ctx, whisper_cli):
    # insere mídia com extensão não suportada (.txt) apontando para arquivo real
    p = ctx.media_root / "554991539437@s.whatsapp.net" / "0" / "1" / "nota.txt"
    p.write_bytes(b"texto qualquer")
    conn = sqlite3.connect(ctx.db)
    conn.execute(
        "INSERT INTO ZWAMEDIAITEM (Z_PK, ZMESSAGE, ZMEDIALOCALPATH, ZFILESIZE, ZTITLE) VALUES (?, ?, ?, ?, NULL)",
        (50, 10, "Media/554991539437@s.whatsapp.net/0/1/nota.txt", 100),
    )
    conn.commit()
    conn.close()
    res = media.transcribe_audio(50, db_path=ctx.db)
    assert "error" in res
    assert "não suportada" in res["error"]
    assert whisper_cli == []


def test_transcribe_timeout(ctx, monkeypatch):
    monkeypatch.setattr(media.shutil, "which", lambda name: "/usr/bin/whisper-cli")
    monkeypatch.setattr(media, "_resolve_whisper_model", lambda model, cli: "/fake/models/ggml-small.bin")
    def boom(cmd, capture_output=True, text=True, timeout=None):
        raise subprocess.TimeoutExpired(cmd, timeout)
    monkeypatch.setattr(media.subprocess, "run", boom)
    res = media.transcribe_audio(2, db_path=ctx.db)
    assert "error" in res
    assert "timeout" in res["error"]


def test_transcribe_whisper_error_stderr(ctx, monkeypatch):
    monkeypatch.setattr(media.shutil, "which", lambda name: "/usr/bin/whisper-cli")
    monkeypatch.setattr(media, "_resolve_whisper_model", lambda model, cli: "/fake/models/ggml-small.bin")
    def fail(cmd, capture_output=True, text=True, timeout=None):
        return SimpleNamespace(returncode=1, stdout="", stderr="error: failed to read model file 'small'")
    monkeypatch.setattr(media.subprocess, "run", fail)
    res = media.transcribe_audio(2, db_path=ctx.db)
    assert "error" in res
    assert "failed to read model" in res["error"]


def test_transcribe_no_json_output(ctx, monkeypatch):
    monkeypatch.setattr(media.shutil, "which", lambda name: "/usr/bin/whisper-cli")
    monkeypatch.setattr(media, "_resolve_whisper_model", lambda model, cli: "/fake/models/ggml-small.bin")
    def nojson(cmd, capture_output=True, text=True, timeout=None):
        return SimpleNamespace(returncode=0, stdout="", stderr="")
    monkeypatch.setattr(media.subprocess, "run", nojson)
    res = media.transcribe_audio(2, db_path=ctx.db)
    assert "error" in res
    assert "não gerou" in res["error"]


# ---------------------------------------------------------------------------
# F3 — export_media / get_media_thumb / backfill
# ---------------------------------------------------------------------------

def test_export_media_creates_sanitized_file(ctx):
    res = media.export_media(1, db_path=ctx.db)  # media 1 = abc123.jpg (chat 1 "Você")
    assert "error" not in res
    out = Path(res["exported"])
    assert out.exists()
    assert out.read_bytes().startswith(b"\xff\xd8\xff")
    assert res["size"] > 0
    # nome sanitizado SEM JID: <chat>__<hash>.jpg
    assert out.name == "Você__abc123.jpg"
    assert "@" not in out.name


def test_export_media_o_excl_does_not_overwrite(ctx):
    r1 = media.export_media(1, db_path=ctx.db)
    out = Path(r1["exported"])
    out.write_bytes(b"CONTEUDO ALTERADO")  # simula arquivo existente
    r2 = media.export_media(1, db_path=ctx.db)
    assert "error" in r2
    assert "não sobrescrito" in r2["error"]
    assert out.read_bytes() == b"CONTEUDO ALTERADO"  # intocado


def test_export_media_overwrite_true(ctx):
    r1 = media.export_media(1, db_path=ctx.db)
    out = Path(r1["exported"])
    out.write_bytes(b"CONTEUDO ALTERADO")
    r2 = media.export_media(1, overwrite=True, db_path=ctx.db)
    assert "error" not in r2
    assert out.read_bytes().startswith(b"\xff\xd8\xff")  # reescrito com o original


def test_export_media_custom_dest_dir(ctx):
    dest = ctx.tmp / "destino"
    res = media.export_media(1, dest_dir=str(dest), db_path=ctx.db)
    assert "error" not in res
    assert Path(res["exported"]).parent == dest


def test_export_media_missing_file(ctx):
    res = media.export_media(3, db_path=ctx.db)  # ghost.jpg
    assert "error" in res


def test_export_media_unknown(ctx):
    res = media.export_media(999, db_path=ctx.db)
    assert "error" in res


def test_get_media_thumb_path(ctx):
    # FIX: por padrão NÃO devolve path (contém JID); exige include_path=True
    res = media.get_media_thumb(1, db_path=ctx.db)  # media 1 tem thumb abc123.thumb
    assert "thumb_path" not in res  # path não vaza por padrão
    assert res["exists"] is True
    res2 = media.get_media_thumb(1, include_path=True, db_path=ctx.db)
    assert res2["thumb_path"] == str((ctx.media_root / "554991539437@s.whatsapp.net" / "0" / "1" / "abc123.thumb").resolve())


def test_get_media_thumb_base64(ctx):
    res = media.get_media_thumb(1, as_base64=True, db_path=ctx.db)
    assert "error" not in res
    assert res["mime"] == "image/jpeg"
    raw = base64.b64decode(res["thumb_base64"])
    assert raw.startswith(b"\xff\xd8\xff")
    assert res["size"] == len(raw)
    assert res["untrusted"] is True


def test_get_media_thumb_missing(ctx):
    res = media.get_media_thumb(2, db_path=ctx.db)  # audio sem thumb
    assert "error" in res


def test_get_media_thumb_unknown(ctx):
    res = media.get_media_thumb(999, db_path=ctx.db)
    assert "error" in res


def test_backfill_transcripts(ctx, whisper_cli):
    res = media.backfill_transcripts(db_path=ctx.db)
    assert res["processed"] == 1  # só audio01.ogg (mtype 3) tem arquivo
    assert res["transcribed"] == 1
    assert res["cache_hit"] == 0
    assert res["errors"] == []
    # segunda rodada: tudo cache hit
    res2 = media.backfill_transcripts(db_path=ctx.db)
    assert res2["cache_hit"] == 1
    assert res2["transcribed"] == 0


def test_backfill_transcripts_limit(ctx, whisper_cli):
    res = media.backfill_transcripts(limit=1, db_path=ctx.db)
    assert res["processed"] <= 1


# ---------------------------------------------------------------------------
# F2 (refinamento) — conversão ffmpeg p/ formatos que whisper não decodifica
# ---------------------------------------------------------------------------

def _insert_m4a(ctx) -> int:
    """Cria nota.m4a no disco e registra ZWAMEDIAITEM. Retorna media_id."""
    p = ctx.media_root / "554991539437@s.whatsapp.net" / "0" / "1" / "nota.m4a"
    p.write_bytes(b"ftypM4A" + b"\x00" * 128)
    conn = sqlite3.connect(ctx.db)
    conn.execute(
        "INSERT INTO ZWAMEDIAITEM (Z_PK, ZMESSAGE, ZMEDIALOCALPATH, ZFILESIZE, ZTITLE) VALUES (?, ?, ?, ?, NULL)",
        (60, 11, "Media/554991539437@s.whatsapp.net/0/1/nota.m4a", 136),
    )
    conn.commit()
    conn.close()
    return 60


def test_transcribe_m4a_converts_with_ffmpeg(ctx, monkeypatch):
    media_id = _insert_m4a(ctx)
    monkeypatch.setattr(media.shutil, "which",
                        lambda name: "/usr/bin/whisper-cli" if name == "whisper-cli" else "/usr/bin/ffmpeg")
    monkeypatch.setattr(media, "_resolve_whisper_model", lambda model, cli: "/fake/models/ggml-small.bin")
    calls = []

    def fake_ffmpeg(cmd, capture_output=True, text=True, timeout=None):
        calls.append(("ffmpeg", cmd))
        out = cmd[-1]  # wav path
        Path(out).write_bytes(b"RIFFwavfake")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_whisper(cmd, capture_output=True, text=True, timeout=None):
        calls.append(("whisper", cmd))
        prefix = cmd[-1]
        Path(f"{prefix}.json").write_text(json.dumps(
            {"transcription": [{"offsets": {"from": 0, "to": 1000}, "text": "convertido"}],
             "result": {"language": "pt"}}))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        if cmd[0] == "/usr/bin/ffmpeg":
            return fake_ffmpeg(cmd)
        return fake_whisper(cmd)

    monkeypatch.setattr(media.subprocess, "run", fake_run)
    res = media.transcribe_audio(media_id, db_path=ctx.db)
    assert "error" not in res
    assert res["text"] == "convertido"
    kinds = [k for k, _ in calls]
    assert kinds == ["ffmpeg", "whisper"]
    ff_cmd = calls[0][1]
    assert "-ar" in ff_cmd and "16000" in ff_cmd and "-ac" in ff_cmd
    # whisper recebeu o wav convertido (não o m4a)
    whisper_cmd = calls[1][1]
    assert whisper_cmd[whisper_cmd.index("-f") + 1].endswith(".wav")
    # cache estável: hash do ORIGINAL (m4a), não do wav
    original = ctx.media_root / "554991539437@s.whatsapp.net" / "0" / "1" / "nota.m4a"
    assert (media.CACHE_DIR / f"{media._sha256_file(original)}.json").exists()
    # wav temporário foi limpo
    assert not any(p.name.startswith("wpp-ffmpeg-") for p in (ctx.tmp / "tmp").iterdir())


def test_transcribe_m4a_without_ffmpeg(ctx, monkeypatch):
    media_id = _insert_m4a(ctx)
    monkeypatch.setattr(media.shutil, "which",
                        lambda name: "/usr/bin/whisper-cli" if name == "whisper-cli" else None)
    res = media.transcribe_audio(media_id, db_path=ctx.db)
    assert "error" in res
    assert "brew install ffmpeg" in res["error"]


def test_transcribe_opus_converts_with_ffmpeg(ctx, monkeypatch):
    """opus NÃO é nativo do whisper.cpp 1.9.1 (verificado: failed to read audio file).
    Deve converter com ffmpeg antes de chamar o whisper."""
    from types import SimpleNamespace as SN
    import sqlite3
    media_id = _insert_m4a(ctx)  # planta nota.m4a (media_id 60)
    # troca o arquivo e o path do banco para .opus
    base = ctx.media_root / "554991539437@s.whatsapp.net" / "0" / "1"
    (base / "nota.m4a").rename(base / "nota.opus")
    conn = sqlite3.connect(ctx.db)
    conn.execute("UPDATE ZWAMEDIAITEM SET ZMEDIALOCALPATH = ? WHERE Z_PK = ?",
                 ("Media/554991539437@s.whatsapp.net/0/1/nota.opus", media_id))
    conn.commit()
    conn.close()

    monkeypatch.setattr(media.shutil, "which",
                        lambda name: "/usr/bin/whisper-cli" if name == "whisper-cli" else "/usr/bin/ffmpeg")
    monkeypatch.setattr(media, "_resolve_whisper_model", lambda model, cli: "/fake/models/ggml-small.bin")
    calls = []

    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        calls.append(cmd)
        if cmd[0] == "/usr/bin/ffmpeg":
            wav = cmd[-1]
            Path(wav).write_bytes(b"RIFF" + b"\x00" * 64)
            return SN(returncode=0, stdout="", stderr="")
        prefix = cmd[-1]
        Path(f"{prefix}.json").write_text(json.dumps({
            "transcription": [{"timestamps": {"from": "00:00:00,000", "to": "00:00:01,000"},
                               "offsets": {"from": 0, "to": 1000}, "text": "transcrito de opus"}],
            "result": {"language": "pt"}}), encoding="utf-8")
        return SN(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(media.subprocess, "run", fake_run)
    res = media.transcribe_audio(media_id, db_path=ctx.db)
    assert "error" not in res, res
    assert res["text"] == "transcrito de opus"
    assert any(c[0] == "/usr/bin/ffmpeg" for c in calls)  # opus -> wav antes do whisper
