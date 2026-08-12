"""Driver de leitura do WhatsApp nativo (macOS).

Lê o ChatStorage.sqlite em modo READ-ONLY. Nenhuma escrita é feita aqui,
por design: gravar no banco não transmite mensagens e corrompe o Core Data
store do app (ver SPEC e análise de engenharia reversa).

Banco: ~/Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite
Timestamps: Core Data epoch (segundos desde 2001-01-01) -> +978307200 para unix.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CORE_DATA_EPOCH_OFFSET = 978307200  # segundos entre 1970-01-01 e 2001-01-01

DEFAULT_DB_PATH = Path.home() / "Library/Group Containers/group.net.whatsapp.WhatsApp.shared/ChatStorage.sqlite"
FTS_DB_PATH = Path.home() / "Library/Group Containers/group.net.whatsapp.WhatsApp.shared/fts/ChatSearchV5f.sqlite"

# Mídias em disco (texto claro, sem criptografia — verificado em F0):
# ZMEDIALOCALPATH no banco começa com "Media/<jid>/..."; o prefixo físico é
# "Message/" ANTES de "Media/". Logo a raiz física é .../Message/Media.
WHATSAPP_DATA_ROOT = Path.home() / "Library/Group Containers/group.net.whatsapp.WhatsApp.shared"
MEDIA_ROOT = WHATSAPP_DATA_ROOT / "Message" / "Media"

# Mapeamento ZMESSAGETYPE -> categoria, confirmado em F0 por extensão real de
# arquivo no disco: 1=image(jpg), 2/11=video(mp4), 3=audio(opus/m4a),
# 8=document(pdf), 15=sticker/webp(.was antigo), 42=image.
MEDIA_TYPE_MAP: dict[int, str] = {
    1: "image",
    42: "image",
    2: "video",
    11: "video",
    3: "audio",
    8: "document",
    15: "sticker",
}

# Extensões aceitas pelo transcribe_audio (whisper.cpp via ffmpeg do brew)
AUDIO_EXTS = {"opus", "m4a", "mp3", "was", "wav", "ogg"}
AUDIO_SIZE_CAP_BYTES = 50 * 1024 * 1024  # cap 50MB (SPEC F2)


def _ro_uri(db_path: Path) -> str:
    """URI de conexão SQLite somente-leitura (imutável e à prova de lock)."""
    return f"file:{db_path}?mode=ro&immutable=0"


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = Path(db_path) if db_path else DEFAULT_DB_PATH
    if not path.exists():
        raise FileNotFoundError(f"Banco do WhatsApp não encontrado em {path}")
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _dt(core_data_ts: int | None) -> str | None:
    if core_data_ts is None:
        return None
    # ZLASTMESSAGEDATE tem unidades inconsistentes no app (alguns chats guardam
    # nanosegundos/marcadores internos). Aceita apenas valores plausíveis:
    # segundos Core Data entre 2001 e 2101 (0 a ~3.15e9 segundos).
    try:
        ts = float(core_data_ts)
    except (TypeError, ValueError):
        return None
    if not (0 <= ts <= 3_160_000_000):  # ~2001-01-01 a ~2101-01-01
        return None
    return f"{int(ts + CORE_DATA_EPOCH_OFFSET)}"  # epoch unix; formato legível aplicado no cliente


def mask_jid(jid: str | None) -> str | None:
    """FIX #4: mascara JID real — mantém prefixo/sufixo, esconde o meio.

    ex: 55199991539437@s.whatsapp.net -> 5519********437@s.whatsapp.net
    """
    if not jid:
        return None
    local, sep, domain = jid.partition("@")
    if not sep:
        return jid
    if len(local) <= 6:
        masked_local = local[:2] + "*" * (len(local) - 2)
    else:
        masked_local = local[:4] + "*" * (len(local) - 7) + local[-3:]
    return f"{masked_local}@{domain}"


# ---------------------------------------------------------------------------
# Conversas
# ---------------------------------------------------------------------------

def _clean_preview(text: str | None, max_len: int = 120) -> str | None:
    """Só retorna preview se for texto legível (UTF-8), senão None."""
    if not text:
        return None
    try:
        decoded = text.encode("latin-1").decode("utf-8")
    except (UnicodeDecodeError, UnicodeEncodeError):
        return None
    # payloads binários do app (mídia/sistema) têm alta densidade de '/' e não-latin
    if decoded.count("/") > 3 or "////" in decoded:
        return None
    # blobs base64 longos (UUIDs/mídia) sem espaços
    if len(decoded) >= 40 and " " not in decoded and decoded.count("=") > 0:
        return None
    if len(decoded) >= 60 and " " not in decoded:
        return None
    # precisa ter proporção razoável de letras/espaços
    letters = sum(1 for c in decoded if c.isalpha() or c.isspace())
    if len(decoded) > 0 and letters / len(decoded) < 0.5:
        return None
    if any(ord(c) < 32 and c not in "\n\r\t" for c in decoded):
        return None
    if len(decoded) > max_len:
        decoded = decoded[:max_len] + "…"
    return decoded


@dataclass
class Chat:
    id: int
    name: str | None
    jid: str | None
    session_type: int | None
    unread: int | None
    last_message_at: str | None
    last_message_preview: str | None
    archived: int | None

    def to_dict(self, mask_jids: bool = True) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "jid": mask_jid(self.jid) if not mask_jids else None,
            "type": "group" if self.session_type == 1 else "direct",
            "unread": self.unread,
            "last_message_at": self.last_message_at,
            "last_message_preview": self.last_message_preview,
            "archived": bool(self.archived),
        }


def list_chats(limit: int = 50, unread_only: bool = False, include_archived: bool = False,
               db_path: Path | None = None) -> list[dict[str, Any]]:
    """Lista conversas. Por padrão exclui arquivadas e feeds de Status.

    include_archived=True: retorna também as arquivadas (flag `archived` indica).
    """
    conn = _connect(db_path)
    try:
        q = """
        SELECT Z_PK AS id, ZPARTNERNAME AS name, ZCONTACTJID AS jid,
               ZSESSIONTYPE AS session_type, ZUNREADCOUNT AS unread,
               ZLASTMESSAGEDATE AS last_at, ZLASTMESSAGETEXT AS last_text,
               ZARCHIVED AS archived
        FROM ZWACHATSESSION
        WHERE ZREMOVED = 0
          AND ZCONTACTJID NOT LIKE '%@broadcast'
          AND ZCONTACTJID NOT LIKE '%@status'
          AND ZCONTACTJID NOT LIKE '%@lid.status'
        """
        if not include_archived:
            q += " AND COALESCE(ZARCHIVED, 0) = 0"
        if unread_only:
            q += " AND ZUNREADCOUNT > 0"
        q += " ORDER BY COALESCE(ZLASTMESSAGEDATE, 0) DESC LIMIT ?"
        rows = conn.execute(q, (limit,)).fetchall()
        return [Chat(r["id"], r["name"], r["jid"], r["session_type"], r["unread"],
                     _dt(r["last_at"]), _clean_preview(r["last_text"]), r["archived"]).to_dict() for r in rows]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Mensagens
# ---------------------------------------------------------------------------

def _resolve_chat(conn: sqlite3.Connection, chat_id: int | None = None, jid: str | None = None) -> dict | None:
    if chat_id is not None:
        row = conn.execute(
            "SELECT Z_PK AS id, ZPARTNERNAME AS name, ZCONTACTJID AS jid FROM ZWACHATSESSION WHERE Z_PK = ?",
            (chat_id,),
        ).fetchone()
        return dict(row) if row else None
    if jid is not None:
        row = conn.execute(
            "SELECT Z_PK AS id, ZPARTNERNAME AS name, ZCONTACTJID AS jid FROM ZWACHATSESSION WHERE ZCONTACTJID = ?",
            (jid,),
        ).fetchone()
        return dict(row) if row else None
    return None


# ---------------------------------------------------------------------------
# Mídia
# ---------------------------------------------------------------------------

def media_category(mtype: int | None) -> str:
    """Categoria pública de um ZMESSAGETYPE. Desconhecidos não são mentidos:
    viram 'type<N>' em vez de uma categoria errada."""
    if mtype is None:
        return "unknown"
    return MEDIA_TYPE_MAP.get(mtype, f"type{mtype}")


def _media_types_for(category: str | None) -> list[int] | None:
    """Categoria pública -> lista de ZMESSAGETYPE aceitos (None = todas)."""
    if category is None:
        return None
    cat = category.strip().lower()
    accepted = sorted({mtype for mtype, c in MEDIA_TYPE_MAP.items() if c == cat})
    if not accepted:
        raise ValueError(
            f"media_type inválido: '{category}'. Use um de: "
            f"{sorted(set(MEDIA_TYPE_MAP.values()))}"
        )
    return accepted


def _resolve_media_path(rel: str | None) -> Path | None:
    """Resolve ZMEDIALOCALPATH/ZTHUMBNAILLOCALPATH para o path físico absoluto.

    Regras (SPEC F1/F0):
    - 'Media/<jid>/<a>/<b>/<hash>.<ext>' -> '<root>/Message/Media/<jid>/...'
    - 'Message/Media/...' já é aceito como está.
    - Caminho vazio, absoluto, ou que escape de MEDIA_ROOT (via '..' ou symlink)
      é REJEITADO: retorna None. O chamador decide se é erro ou file_exists=false.
    - Containment é verificado DEPOIS de resolve() (seguir symlink não escapa).
    """
    if not rel:
        return None
    rel = rel.replace("\\", "/").lstrip("/")
    if Path(rel).is_absolute():
        return None
    if rel.startswith("Media/"):
        rel = rel[len("Media/"):]
    elif rel.startswith("Message/Media/"):
        rel = rel[len("Message/Media/"):]
    # senão, rel é interpretado como relativo à raiz de mídia (defensivo p/ thumbs)
    candidate = (MEDIA_ROOT / rel).resolve()
    if not candidate.is_relative_to(MEDIA_ROOT):
        return None
    return candidate


def _ext_from_rel(rel: str | None) -> str | None:
    """Extensão real do arquivo (sem ponto, minúscula), direto do path do banco."""
    if not rel:
        return None
    name = rel.replace("\\", "/").rsplit("/", 1)[-1]
    if "." not in name:
        return None
    return name.rsplit(".", 1)[-1].lower() or None


def _media_item(media_id: int, message_id: int | None, mtype: int | None,
                from_me: bool | None, at_unix: str | None, local_path: str | None,
                size: int | None, duration: int | float | None,
                thumb_path: str | None, caption: str | None) -> dict[str, Any]:
    """Monta o item público de mídia. SEM paths e SEM JID por padrão."""
    path = _resolve_media_path(local_path)
    return {
        "media_id": media_id,
        "message_id": message_id,
        "category": media_category(mtype),
        "from_me": bool(from_me) if from_me is not None else None,
        "at_unix": at_unix,
        "file_exists": bool(path and path.exists()),
        "size": size,
        "ext": _ext_from_rel(local_path),
        "duration": duration,
        "has_thumb": bool(thumb_path),
        "caption": caption,
        "caption_untrusted": bool(caption),  # FIX #2: conteúdo de mídia é não confiável
    }


_MEDIA_SELECT = """
SELECT med.Z_PK AS media_id, med.ZFILESIZE AS size,
       med.ZMOVIEDURATION AS duration, med.ZMEDIALOCALPATH AS local_path,
       med.ZTHUMBNAILLOCALPATH AS thumb_path, med.ZTITLE AS caption,
       msg.Z_PK AS message_id, msg.ZISFROMME AS from_me,
       msg.ZMESSAGEDATE AS at, msg.ZMESSAGETYPE AS mtype
FROM ZWAMEDIAITEM med
JOIN ZWAMESSAGE msg ON msg.Z_PK = med.ZMESSAGE
"""


def list_media(chat_id: int, media_type: str | None = None, limit: int = 100,
               before: int | None = None, db_path: Path | None = None) -> dict[str, Any]:
    """Lista mídias de uma conversa (metadados; nunca expõe paths nem JIDs).

    media_type: image|video|audio|document|sticker. `before` é unix ts (paginação,
    mesma convenção de get_messages). file_exists=false = mídia sem arquivo local.
    """
    limit = min(max(1, limit), 500)  # SPEC: limit <= 500
    conn = _connect(db_path)
    try:
        chat = _resolve_chat(conn, chat_id=chat_id)
        if not chat:
            return {"error": f"Conversa {chat_id} não encontrada", "media": []}
        types = _media_types_for(media_type)
        q = _MEDIA_SELECT + "WHERE msg.ZCHATSESSION = ? AND med.ZMEDIALOCALPATH IS NOT NULL"
        params: list[Any] = [chat_id]
        if types is not None:
            q += f" AND msg.ZMESSAGETYPE IN ({','.join('?' * len(types))})"
            params.extend(types)
        if before is not None:
            q += " AND msg.ZMESSAGEDATE < ?"
            params.append(before)
        q += " ORDER BY msg.ZMESSAGEDATE DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(q, params).fetchall()
        media = [
            _media_item(r["media_id"], r["message_id"], r["mtype"], r["from_me"],
                        _dt(r["at"]), r["local_path"], r["size"], r["duration"],
                        r["thumb_path"], r["caption"])
            for r in rows
        ]
        return {
            "chat": {"id": chat["id"], "name": chat["name"]},
            "count": len(media),
            "media": media,
            "note": "paths e JIDs nunca são expostos aqui; use get_media(include_path=True) para o path físico.",
        }
    finally:
        conn.close()


def get_media(chat_id: int, media_id: int, include_path: bool = False,
              db_path: Path | None = None) -> dict[str, Any]:
    """Metadados de UMA mídia, com escopo de conversa (media_id é opaco).

    include_path=True resolve o path físico local (containment-checked). O path
    só sai daqui se pedido explicitamente — nunca por padrão.
    """
    conn = _connect(db_path)
    try:
        row = conn.execute(
            _MEDIA_SELECT
            + "WHERE med.Z_PK = ? AND msg.ZCHATSESSION = ?",
            (media_id, chat_id),
        ).fetchone()
        if not row:
            return {"error": f"Mídia {media_id} não encontrada na conversa {chat_id}"}
        item = _media_item(row["media_id"], row["message_id"], row["mtype"], row["from_me"],
                           _dt(row["at"]), row["local_path"], row["size"], row["duration"],
                           row["thumb_path"], row["caption"])
        item["untrusted"] = True  # caption/título podem conter instruções hostis
        if include_path:
            path = _resolve_media_path(row["local_path"])
            item["path"] = str(path) if path else None
        return item
    finally:
        conn.close()


def get_messages(chat_id: int, limit: int = 100, before: int | None = None,
                 max_text_len: int = 2000, include_media: bool = False,
                 db_path: Path | None = None) -> dict[str, Any]:
    conn = _connect(db_path)
    try:
        chat = _resolve_chat(conn, chat_id=chat_id)
        if not chat:
            return {"error": f"Conversa {chat_id} não encontrada", "messages": []}
        # include_media=True: inclui também mensagens de mídia (sem ZTEXT) via
        # LEFT JOIN — cada item ganha media_id + marcador "[media: <categoria>]"
        # (aplicado também a captions, para o agente saber que há anexo).
        q = """
        SELECT msg.ZISFROMME AS from_me, msg.ZMESSAGEDATE AS at, msg.ZTEXT AS text,
               msg.ZMESSAGETYPE AS mtype, msg.ZPUSHNAME AS pushname,
               med.Z_PK AS media_id, med.ZMEDIALOCALPATH AS local_path
        FROM ZWAMESSAGE msg
        LEFT JOIN ZWAMEDIAITEM med ON med.ZMESSAGE = msg.Z_PK
        WHERE msg.ZCHATSESSION = ?
        """
        params: list[Any] = [chat_id]
        if include_media:
            q += " AND ( (msg.ZTEXT IS NOT NULL AND length(msg.ZTEXT) > 0) OR med.ZMEDIALOCALPATH IS NOT NULL )"
        else:
            q += " AND msg.ZTEXT IS NOT NULL AND length(msg.ZTEXT) > 0"
        if before is not None:
            q += " AND msg.ZMESSAGEDATE < ?"
            params.append(before)
        q += " ORDER BY msg.ZMESSAGEDATE DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(q, params).fetchall()
        msgs = []
        for r in rows:
            text = r["text"]
            item: dict[str, Any] = {
                "from_me": bool(r["from_me"]),
                "at_unix": _dt(r["at"]),
                "type": r["mtype"],
                "sender_name": r["pushname"],
            }
            # FIX: mídia REAL = com arquivo local OU tipo de mídia conhecido.
            # (mensagens type 0 têm 706k media items de link preview SEM path —
            # marcar isso seria ruído; verificado no banco real em 2026-08-11)
            is_real_media = r["local_path"] is not None or r["mtype"] in MEDIA_TYPE_MAP
            if include_media and r["media_id"] is not None and is_real_media:
                marker = f"[media: {media_category(r['mtype'])}]"
                text = f"{text}\n{marker}" if text else marker
                item["media_id"] = r["media_id"]
            if max_text_len and text and len(text) > max_text_len:
                text = text[:max_text_len] + "…[truncado]"
            # FIX #2: marca conteúdo não confiável — mensagens podem conter instruções hostis
            item["text"] = text
            item["untrusted"] = True
            msgs.append(item)
        msgs.reverse()  # mais antiga -> mais recente
        return {"chat": {"id": chat["id"], "name": chat["name"]}, "messages": msgs}
    finally:
        conn.close()


def search_messages(query: str, chat_id: int | None = None, limit: int = 50,
                    max_text_len: int = 1000, db_path: Path | None = None) -> dict[str, Any]:
    """Busca textual com fallback: tenta o FTS do app, cai para LIKE."""
    conn = _connect(db_path)
    try:
        like = f"%{query}%"
        q = """
        SELECT m.Z_PK AS id, m.ZISFROMME AS from_me, m.ZMESSAGEDATE AS at,
               m.ZTEXT AS text, c.ZPARTNERNAME AS chat_name
        FROM ZWAMESSAGE m
        LEFT JOIN ZWACHATSESSION c ON c.Z_PK = m.ZCHATSESSION
        WHERE m.ZTEXT LIKE ? AND m.ZTEXT IS NOT NULL
        """
        params: list[Any] = [like]
        if chat_id is not None:
            q += " AND m.ZCHATSESSION = ?"
            params.append(chat_id)
        q += " ORDER BY m.ZMESSAGEDATE DESC LIMIT ?"
        params.append(limit)
        rows = conn.execute(q, params).fetchall()
        results = []
        for r in rows:
            text = r["text"]
            if max_text_len and text and len(text) > max_text_len:
                text = text[:max_text_len] + "…[truncado]"
            results.append({
                "chat_id": None,  # mantido simples; join de PK não exposto
                "chat_name": r["chat_name"],
                "from_me": bool(r["from_me"]),
                "at_unix": _dt(r["at"]),
                "text": text,
                "untrusted": True,  # FIX #2: conteúdo não confiável
            })
        return {"query": query, "count": len(results), "results": results}
    finally:
        conn.close()


def get_chat_info(chat_id: int, db_path: Path | None = None) -> dict[str, Any]:
    conn = _connect(db_path)
    try:
        chat = _resolve_chat(conn, chat_id=chat_id)
        if not chat:
            return {"error": f"Conversa {chat_id} não encontrada"}
        info = {"id": chat["id"], "name": chat["name"], "jid": mask_jid(chat["jid"])}  # FIX #4
        # grupo?
        g = conn.execute(
            "SELECT ZCREATORJID AS creator, ZSUBJECTTIMESTAMP AS subject_at FROM ZWAGROUPINFO WHERE Z_PK = (SELECT ZGROUPINFO FROM ZWACHATSESSION WHERE Z_PK = ?)",
            (chat_id,),
        ).fetchone()
        if g:
            info["group"] = {"creator_jid": mask_jid(g["creator"]), "subject_at": _dt(g["subject_at"])}  # FIX #4
        return info
    finally:
        conn.close()


def export_chat(chat_id: int, fmt: str = "json", max_text_len: int = 2000,
                db_path: Path | None = None) -> dict[str, Any]:
    data = get_messages(chat_id, limit=5000, max_text_len=max_text_len, db_path=db_path)
    if "error" in data:
        return data
    out_dir = Path.home() / "whatsapp-mcp-exports"
    out_dir.mkdir(parents=True, exist_ok=True)
    name = (data["chat"]["name"] or f"chat-{chat_id}").replace("/", "_")
    # FIX #9: O_EXCL — nunca sobrescreve arquivo existente (evita seguir symlink pré-plantado)
    if fmt == "md":
        lines = [f"# {data['chat']['name']}\n"]
        for m in data["messages"]:
            who = "Você" if m["from_me"] else (m["sender_name"] or "Eles")
            lines.append(f"**{who}** ({m['at_unix']}): {m['text']}")
        path = out_dir / f"{name}.md"
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
        except FileExistsError:
            return {"error": f"arquivo já existe (não sobrescrito): {path}"}
        return {"exported": str(path), "messages": len(data["messages"]), "format": "md"}
    path = out_dir / f"{name}.json"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False, indent=2))
    except FileExistsError:
        return {"error": f"arquivo já existe (não sobrescrito): {path}"}
    return {"exported": str(path), "messages": len(data["messages"]), "format": "json"}
