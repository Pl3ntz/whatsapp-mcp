"""Driver de escrita do WhatsApp nativo (macOS).

Estratégia validada na máquina do proprietário (2026-08-11):
1. `whatsapp://send?phone=<jid>&text=<texto>` abre a conversa com o texto
   PRE-PREENCHIDO no campo, SEM enviar (comportamento do próprio app,
   verificado via ZWACHATSESSION.ZSAVEDINPUT).
2. O envio só ocorre com Enter — que este driver só emite APÓS confirmação
   explícita do proprietário (gate). Nunca enviamos sem aprovação.

Requisitos: app WhatsApp instalado; permissão de Acessibilidade para o
processo que executa (System Events keystroke).
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any

from .reader import DEFAULT_DB_PATH, _connect, _resolve_chat, mask_jid
from .state import consume_draft, create_draft

APP_BUNDLE_ID = "net.whatsapp.WhatsApp"


def _run(cmd: list[str]) -> str:
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    return (res.stdout or "") + (res.stderr or "")


def _resolve_target(chat_id: int | None = None, jid: str | None = None,
                    phone: str | None = None) -> dict:
    """Resolve a conversa alvo a partir de chat_id, jid ou número de telefone."""
    if phone:
        digits = "".join(c for c in phone if c.isdigit())
        if not digits.startswith("55"):
            digits = "55" + digits
        return {"jid": f"{digits}@s.whatsapp.net", "name": f"+{digits}"}
    conn = _connect()
    try:
        chat = _resolve_chat(conn, chat_id=chat_id, jid=jid)
        if not chat:
            raise ValueError(f"Conversa não encontrada (chat_id={chat_id}, jid={jid})")
        return chat
    finally:
        conn.close()


def prepare_send(chat_id: int | None = None, jid: str | None = None,
                 phone: str | None = None, text: str = "") -> dict[str, Any]:
    """Abre o app na conversa com o texto pre-preenchido. NÃO envia nada.

    Retorna o que será enviado para o gate de confirmação (FIX #3: draft com
    expiração no estado server-side; FIX #4: jid mascarado).
    """
    target = _resolve_target(chat_id=chat_id, jid=jid, phone=phone)
    import urllib.parse
    url = f"whatsapp://send?phone={target['jid'].split('@')[0]}&text={urllib.parse.quote(text)}"
    out = _run(["open", url])
    time.sleep(1.5)
    _run(["open", "-a", "WhatsApp"])  # garante frontmost para o gate visual
    # FIX #3: registra o draft no estado server-side
    draft_id, text_hash = create_draft(target["jid"], text)
    return {
        "status": "draft_prepared",
        "draft_id": draft_id,
        "target": {"name": target.get("name"), "jid_masked": mask_jid(target.get("jid"))},
        "text_preview": text[:200] + ("…" if len(text) > 200 else ""),
        "confirmation_required": True,
        "note": "O texto está no campo do app. Nada foi enviado. Confirme com confirm_send(draft_id). Expira em 120s.",
    }


def confirm_send(draft_id: str) -> dict[str, Any]:
    """Pressiona Enter no app. Só após validação do draft (FIX #3).

    Antes do Enter, RE-ABRE a conversa alvo com o texto aprovado (do draft):
    elimina envio para conversa errada (troca de alvo entre prepare e confirm)
    e envio de texto editado pelo usuário.
    """
    draft = consume_draft(draft_id)
    if not draft:
        return {"error": "draft_id inválido ou expirado. Chame send_message antes e confirme o draft_id retornado."}
    # re-abre a conversa alvo com o texto aprovado (garante alvo + texto corretos)
    import urllib.parse
    target_jid = draft.target
    url = f"whatsapp://send?phone={target_jid.split('@')[0]}&text={urllib.parse.quote(draft.text)}"
    _run(["open", url])
    time.sleep(1.2)
    _run(["osascript", "-e", 'tell application "WhatsApp" to activate'])
    time.sleep(0.8)
    _run(["osascript", "-e", 'tell application "System Events" to key code 36'])  # Return
    time.sleep(1.5)
    return {
        "status": "enter_sent",
        "draft_id": draft_id,
        "note": "Enter pressionado na conversa do draft. Confira no app e use verify_sent para confirmar a entrega.",
    }


def clear_draft(chat_id: int | None = None, jid: str | None = None,
                phone: str | None = None) -> dict[str, Any]:
    """Abre a conversa e limpa o rascunho (⌘A + Delete). Sem envio."""
    prepare_send(chat_id=chat_id, jid=jid, phone=phone, text="")
    time.sleep(1)
    _run(["osascript", "-e", 'tell application "WhatsApp" to activate'])
    time.sleep(0.5)
    _run(["osascript", "-e", 'tell application "System Events" to keystroke "a" using command down'])
    time.sleep(0.3)
    _run(["osascript", "-e", 'tell application "System Events" to key code 51'])  # Delete
    time.sleep(1)
    return {"status": "draft_cleared", "note": "Rascunho limpo (verificado no banco pelo chamador se desejado)."}


def verify_sent(chat_id: int | None = None, jid: str | None = None,
                phone: str | None = None, text_substring: str = "",
                db_path: Path | None = None) -> dict[str, Any]:
    """Verifica no banco (read-only) se a mensagem foi registrada como enviada (from_me=1).

    FIX #7: exige text_substring (evita devolver texto completo de qualquer
    última mensagem enviada sem contexto).
    """
    if not text_substring:
        return {"error": "text_substring é obrigatório (evita vazar texto de mensagens sem contexto)."}
    conn = _connect(db_path)
    try:
        q = """
        SELECT m.ZTEXT AS text, m.ZMESSAGEDATE AS at, m.ZISFROMME AS from_me
        FROM ZWAMESSAGE m
        WHERE m.ZISFROMME = 1 AND m.ZTEXT IS NOT NULL AND m.ZTEXT LIKE ?
        """
        params: list[Any] = [f"%{text_substring}%"]
        q += " ORDER BY m.ZMESSAGEDATE DESC LIMIT 1"
        row = conn.execute(q, params).fetchone()
        if not row:
            return {"sent": False, "note": "Nenhuma mensagem enviada correspondente encontrada."}
        return {
            "sent": True,
            "text": row["text"][:500] + ("…" if len(row["text"]) > 500 else ""),
            "at_unix": row["at"] + 978307200,
        }
    finally:
        conn.close()
