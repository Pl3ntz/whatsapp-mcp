"""Operações sobre arquivos de mídia: transcrição local, export e thumbnails.

Depende de reader para: conexão read-only ao banco, resolução de paths com
containment (MEDIA_ROOT) e metadados da mídia. Nada aqui escreve no banco.

Transcrição (F2): roda `whisper-cli` (whisper.cpp) em SUBPROCESS com timeout —
um crash do whisper NUNCA derruba o MCP. Cache local em
~/.whatsapp-mcp/transcripts/<sha256(arquivo)>.json.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from . import reader
from .reader import AUDIO_EXTS, AUDIO_SIZE_CAP_BYTES, MEDIA_ROOT

WHISPER_CLI = "whisper-cli"
FFMPEG_CLI = "ffmpeg"
WHISPER_TIMEOUT_SECONDS = 180  # SPEC F2

# whisper.cpp (brew 0.18.x) decodifica nativamente: flac, mp3, ogg, wav
# (verificado em 2026-08-11 via `whisper-cli --help` E execução real: o
# whisper.cpp do brew 1.9.1 FALHA ao ler opus: "failed to read audio file".
# opus/m4a/was precisam de conversão ffmpeg -> wav 16k mono (SPEC F2).
WHISPER_NATIVE_EXTS = {"wav", "flac", "mp3", "ogg"}
FFMPEG_CONVERT_EXTS = {"m4a", "was", "opus"}
CACHE_DIR = Path.home() / ".whatsapp-mcp" / "transcripts"
THUMB_MAX_BASE64_BYTES = 500 * 1024  # SPEC: base64 ≤ 500KB
DEFAULT_EXPORT_DIR = Path.home() / "whatsapp-mcp-exports"


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------

def _cache_path_for(file_hash: str) -> Path:
    return CACHE_DIR / f"{file_hash}.json"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_whisper_json(data: dict) -> dict:
    """whisper.cpp -oj gera {"transcription":[{offsets,text}...], "result":{...}}."""
    segments = []
    for seg in data.get("transcription", []):
        off = seg.get("offsets", {}) or {}
        segments.append({
            "from_ms": off.get("from"),
            "to_ms": off.get("to"),
            "text": (seg.get("text") or "").strip(),
        })
    text = " ".join(s["text"] for s in segments).strip()
    duration = None
    if segments and segments[-1].get("to_ms") is not None:
        duration = segments[-1]["to_ms"] / 1000.0
    return {
        "text": text,
        "segments": segments,
        "duration": duration,
        "language": (data.get("result") or {}).get("language"),
    }


def _get_media_row(conn, media_id: int) -> dict | None:
    row = conn.execute(
        "SELECT med.Z_PK AS media_id, med.ZMEDIALOCALPATH AS local_path, "
        "med.ZTHUMBNAILLOCALPATH AS thumb_path, med.ZTITLE AS caption, "
        "msg.ZMESSAGETYPE AS mtype, msg.Z_PK AS message_id "
        "FROM ZWAMEDIAITEM med "
        "JOIN ZWAMESSAGE msg ON msg.Z_PK = med.ZMESSAGE "
        "WHERE med.Z_PK = ?",
        (media_id,),
    ).fetchone()
    return dict(row) if row else None


def _resolve_or_error(local_path: str | None) -> tuple[Path | None, str | None]:
    """Resolve path da mídia com containment. Retorna (path, erro)."""
    path = reader._resolve_media_path(local_path)
    if path is None:
        return None, "path da mídia não resolvível (contido em Message/Media? banco corrompido?)"
    if not path.exists():
        return None, f"arquivo não existe em disco: {path}"
    return path, None


def _ensure_audio_for_whisper(path: Path, tmp_dir: Path) -> tuple[Path, bool, str | None]:
    """Retorna (caminho aceito pelo whisper, precisa_limpar, erro).

    Formatos nativos do whisper vão direto; m4a/was são convertidos com ffmpeg
    para wav 16k mono em subprocess com timeout (crash do ffmpeg não derruba o MCP).
    """
    ext = path.suffix.lstrip(".").lower()
    if ext in WHISPER_NATIVE_EXTS:
        return path, False, None
    if ext in FFMPEG_CONVERT_EXTS:
        ffmpeg = shutil.which(FFMPEG_CLI)
        if not ffmpeg:
            return path, False, (
                f"{FFMPEG_CLI} não encontrado no PATH. Instale com: brew install ffmpeg "
                f"(necessário para converter {ext} -> wav antes do whisper)"
            )
        wav = tmp_dir / f"wpp-ffmpeg-{uuid.uuid4().hex[:12]}.wav"
        cmd = [ffmpeg, "-y", "-i", str(path), "-ar", "16000", "-ac", "1", "-f", "wav", str(wav)]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=WHISPER_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            return path, False, f"timeout após {WHISPER_TIMEOUT_SECONDS}s convertendo com ffmpeg"
        except OSError as e:
            return path, False, f"falha ao executar {FFMPEG_CLI}: {e}"
        if res.returncode != 0 or not wav.exists():
            return path, False, f"ffmpeg falhou ao converter {ext} (exit {res.returncode}): {(res.stderr or '')[-400:]}"
        return wav, True, None
    return path, False, f"extensão '{ext}' não suportada para transcrição (use uma de: {sorted(AUDIO_EXTS)})"


# ---------------------------------------------------------------------------
# F2 — transcrição local
# ---------------------------------------------------------------------------

def _resolve_whisper_model(model: str, cli: str) -> str | None:
    """Resolve o modelo whisper: caminho completo, ou nome simples procurado
    nos locais comuns (brew share, ~/.cache/whisper, dir do cli)."""
    p = Path(model)
    if p.exists() and p.is_file():
        return str(p)
    # nome simples: ggml-<nome>.bin
    candidates: list[Path] = []
    try:
        brew_prefix = subprocess.run(
            ["brew", "--prefix", "whisper-cpp"], capture_output=True, text=True, timeout=10
        ).stdout.strip()
        if brew_prefix:
            candidates.append(Path(brew_prefix) / "share" / "whisper-cpp")
    except Exception:
        pass
    candidates += [Path.home() / ".cache" / "whisper", Path(cli).resolve().parent / "models"]
    for base in candidates:
        for f in (base / f"ggml-{model}.bin", base / f"for-tests-ggml-{model}.bin"):
            if f.exists():
                return str(f)
    return None


def transcribe_audio(media_id: int, model: str = "small", language: str | None = None,
                     db_path: Path | None = None) -> dict[str, Any]:
    """Transcreve um áudio localmente via whisper.cpp (sem nuvem).

    - Path resolvido server-side (containment); extensão validada (opus/m4a/mp3/was).
    - Cap 50MB. Cache em ~/.whatsapp-mcp/transcripts/<sha256(arquivo)>.json.
    - Roda whisper-cli em subprocess com timeout de 180s — crash não derruba o MCP.
    - Se whisper-cli não estiver instalado: erro claro com instrução de brew.

    Requisito: `brew install whisper-cpp ffmpeg`. Modelo precisa existir (ex.: o
    download-ggml-model.sh do whisper.cpp, ou caminho completo para ggml-*.bin).
    """
    if not model or not str(model).strip():
        return {"error": "model não pode ser vazio (ex.: small, base, ou path para ggml-*.bin)"}
    conn = reader._connect(db_path)
    try:
        row = _get_media_row(conn, media_id)
    finally:
        conn.close()
    if not row:
        return {"error": f"Mídia {media_id} não encontrada"}

    path, err = _resolve_or_error(row["local_path"])
    if err:
        return {"error": err}

    tmp_dir = Path(os.environ.get("TMPDIR", "/tmp"))
    audio_for_whisper, cleanup_wav, err = _ensure_audio_for_whisper(path, tmp_dir)
    if err:
        return {"error": err}

    size = path.stat().st_size
    if size > AUDIO_SIZE_CAP_BYTES:
        return {"error": f"áudio de {size / 1024 / 1024:.1f}MB excede o cap de 50MB"}

    file_hash = _sha256_file(path)
    cache = _cache_path_for(file_hash)
    if cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            data.update({"cache_hit": True, "media_id": media_id, "engine": data.get("engine", "whisper.cpp"),
                         "untrusted": True})
            return data
        except (json.JSONDecodeError, OSError):
            pass  # cache corrompido: re-transcreve e sobrescreve

    cli = shutil.which(WHISPER_CLI)
    if not cli:
        return {
            "error": (
                f"{WHISPER_CLI} não encontrado no PATH. Instale com: "
                "brew install whisper-cpp ffmpeg  (o whisper-cli do brew já lida com opus/m4a). "
                "Depois baixe o modelo: sh <(brew --prefix whisper-cpp)/share/whisper.cpp/models/download-ggml-model.sh small"
            ),
        }

    # Resolve o modelo: nome simples ("tiny") → procura no prefix do brew;
    # caminho completo → usa direto.
    model_resolved = _resolve_whisper_model(str(model), cli)
    if model_resolved is None:
        return {
            "error": (
                f"modelo whisper '{model}' não encontrado. Opções: "
                "1) passe o caminho completo de um ggml-*.bin, ou "
                "2) valide rápido com o tiny do brew: "
                f"whisper-cli -m $(brew --prefix whisper-cpp)/share/whisper-cpp/for-tests-ggml-tiny.bin -f <audio>"
            ),
        }

    cmd = [cli, "-m", model_resolved, "-f", str(audio_for_whisper)]
    if language:
        cmd += ["-l", str(language)]
    tmp_prefix = tmp_dir / f"wpp-whisper-{uuid.uuid4().hex[:12]}"
    cmd += ["-oj", "-of", str(tmp_prefix)]

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=WHISPER_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return {"error": f"timeout após {WHISPER_TIMEOUT_SECONDS}s de transcrição (áudio longo demais para o modelo {model}?)"}
    except OSError as e:
        return {"error": f"falha ao executar {WHISPER_CLI}: {e}"}

    if res.returncode != 0:
        if cleanup_wav:
            _cleanup_tmp(tmp_prefix, Path(f"{tmp_prefix}.json"))
            try:
                audio_for_whisper.unlink()
            except OSError:
                pass
        stderr = (res.stderr or "")[-800:]
        return {
            "error": (
                f"{WHISPER_CLI} falhou (exit {res.returncode}). "
                f"Detalhes: {stderr}. "
                "Modelo não encontrado? Passe um nome (small/base) ou caminho para ggml-*.bin. "
                "Validação rápida com o modelo tiny do brew: "
                "whisper-cli -m $(brew --prefix whisper-cpp)/share/whisper-cpp/for-tests-ggml-tiny.bin -f <audio> -oj -of /tmp/x"
            ),
        }

    out_json = Path(f"{tmp_prefix}.json")
    if not out_json.exists():
        return {"error": f"{WHISPER_CLI} não gerou {out_json.name} (esperado -oj -of). stderr: {(res.stderr or '')[-400:]}"}
    try:
        parsed = _parse_whisper_json(json.loads(out_json.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, OSError) as e:
        return {"error": f"falha ao ler saída JSON do whisper: {e}"}
    finally:
        _cleanup_tmp(tmp_prefix, out_json)
        if cleanup_wav:
            try:
                audio_for_whisper.unlink()
            except OSError:
                pass

    result = {
        "media_id": media_id,
        "text": parsed["text"],
        "segments": parsed["segments"],
        "duration": parsed["duration"],
        "language": parsed["language"],
        "cache_hit": False,
        "engine": "whisper.cpp",
        "model": model,
        "created_at": int(time.time()),
        "untrusted": True,  # texto transcrito é derivado de conteúdo do usuário
    }
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass  # cache é otimização; falha não deve matar o resultado
    return result


def _cleanup_tmp(tmp_prefix: Path, out_json: Path) -> None:
    for p in (Path(f"{tmp_prefix}.json"), out_json, tmp_prefix):
        try:
            if p.exists():
                p.unlink()
        except OSError:
            pass


# ---------------------------------------------------------------------------
# F3 — export, thumbnail e backfill
# ---------------------------------------------------------------------------

def _sanitize_name(name: str | None, media_id: int) -> str:
    """Nome seguro para arquivo exportado: sem separadores de path, sem pontos
    iniciais, SEM JID (usa o nome de exibição da conversa, nunca o JID)."""
    safe = re.sub(r"[^\w.-]+", "_", name or "").strip("._")  # \w unicode: mantém acentos
    if not safe:
        safe = f"media-{media_id}"
    return safe


def _copy_to_excl(src: Path, dest: Path, overwrite: bool) -> None:
    """Copia src -> dest. overwrite=False: O_EXCL (nunca segue symlink pré-plantado).
    overwrite=True: grava em temp + os.replace (rename atômico substitui o symlink
    em si, não o alvo)."""
    if overwrite:
        tmp = dest.parent / f".tmp-{uuid.uuid4().hex[:12]}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with open(src, "rb") as f_in, os.fdopen(fd, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out, 1 << 20)
        os.replace(tmp, dest)
    else:
        try:
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            raise FileExistsError(f"arquivo já existe (não sobrescrito): {dest}")
        with open(src, "rb") as f_in, os.fdopen(fd, "wb") as f_out:
            shutil.copyfileobj(f_in, f_out, 1 << 20)


def export_media(media_id: int, dest_dir: str | None = None, overwrite: bool = False,
                 db_path: Path | None = None) -> dict[str, Any]:
    """Copia a mídia para um diretório local com nome sanitizado SEM JID.

    Formato do nome: <conversa_sanitizada>__<hash>.<ext> (hash = parte do nome
    físico; nunca expõe o JID). O_EXCL por padrão (não sobrescreve); use
    overwrite=True para substituir (via rename atômico). Default: ~/whatsapp-mcp-exports.
    """
    conn = reader._connect(db_path)
    try:
        row = _get_media_row(conn, media_id)
        if not row:
            return {"error": f"Mídia {media_id} não encontrada"}
        chat = conn.execute(
            "SELECT ZPARTNERNAME AS name FROM ZWACHATSESSION WHERE Z_PK = "
            "(SELECT ZCHATSESSION FROM ZWAMESSAGE WHERE Z_PK = ?)",
            (row["message_id"],),
        ).fetchone()
    finally:
        conn.close()

    path, err = _resolve_or_error(row["local_path"])
    if err:
        return {"error": err}
    out_dir = Path(dest_dir) if dest_dir else DEFAULT_EXPORT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    ext = _safe_ext(path)
    local = row["local_path"].replace("\\", "/").rsplit("/", 1)[-1]
    stem = local.rsplit(".", 1)[0] if "." in local else local
    name = _sanitize_name(chat["name"] if chat else None, media_id)
    dest = out_dir / f"{name}__{stem}.{ext}"
    try:
        _copy_to_excl(path, dest, overwrite)
    except FileExistsError as e:
        return {"error": str(e)}
    except OSError as e:
        return {"error": f"falha ao copiar: {e}"}
    return {"exported": str(dest), "size": path.stat().st_size, "media_id": media_id}


def _safe_ext(path: Path) -> str:
    ext = path.suffix.lstrip(".").lower()
    return ext if ext else "bin"


def get_media_thumb(media_id: int, as_base64: bool = False, include_path: bool = False,
                    db_path: Path | None = None) -> dict[str, Any]:
    """Thumbnail: path local (só com include_path=True, pois o path contém o
    JID bruto no diretório), ou base64 ≤500KB."""
    conn = reader._connect(db_path)
    try:
        row = _get_media_row(conn, media_id)
    finally:
        conn.close()
    if not row:
        return {"error": f"Mídia {media_id} não encontrada"}

    thumb = reader._resolve_media_path(row["thumb_path"])
    if thumb is None:
        return {"error": "thumbnail sem path resolvível (ZTHUMBNAILLOCALPATH ausente ou corrompido)"}
    if not thumb.exists():
        return {"error": "arquivo de thumbnail não existe em disco"}

    if as_base64:
        size = thumb.stat().st_size
        if size > THUMB_MAX_BASE64_BYTES:
            return {"error": f"thumbnail de {size} bytes excede o cap de 500KB para base64"}
        try:
            b64 = base64.b64encode(thumb.read_bytes()).decode("ascii")
        except OSError as e:
            return {"error": f"falha ao ler thumbnail: {e}"}
        return {"media_id": media_id, "thumb_base64": b64, "size": size,
                "mime": "image/jpeg", "untrusted": True}
    result: dict[str, Any] = {"media_id": media_id, "exists": True, "size": thumb.stat().st_size}
    if include_path:
        result["thumb_path"] = str(thumb)
    return result


def backfill_transcripts(limit: int | None = None, db_path: Path | None = None) -> dict[str, Any]:
    """Helper (não é MCP tool): itera áudios com arquivo local e transcreve os
    que ainda não têm cache. Uso: rodar em lote quando whisper-cli estiver
    instalado. Retorna resumo; nunca falha inteiro por um áudio problemático.
    """
    conn = reader._connect(db_path)
    try:
        q = """
        SELECT med.Z_PK AS media_id, med.ZMEDIALOCALPATH AS local_path
        FROM ZWAMEDIAITEM med
        JOIN ZWAMESSAGE msg ON msg.Z_PK = med.ZMESSAGE
        WHERE msg.ZMESSAGETYPE = 3 AND med.ZMEDIALOCALPATH IS NOT NULL
        ORDER BY msg.ZMESSAGEDATE DESC
        """
        rows = conn.execute(q).fetchall()
        if limit is not None:
            rows = rows[:limit]
    finally:
        conn.close()

    out = {"processed": 0, "transcribed": 0, "cache_hit": 0, "errors": []}
    for r in rows:
        media_id = r["media_id"]
        path = reader._resolve_media_path(r["local_path"])
        if path is None or not path.exists():
            out["errors"].append({"media_id": media_id, "error": "sem arquivo local"})
            out["processed"] += 1
            continue
        res = transcribe_audio(media_id, db_path=db_path)
        out["processed"] += 1
        if "error" in res:
            out["errors"].append({"media_id": media_id, "error": res["error"]})
        elif res.get("cache_hit"):
            out["cache_hit"] += 1
        else:
            out["transcribed"] += 1
    return out
