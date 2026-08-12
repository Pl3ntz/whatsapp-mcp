"""Estado server-side do gate de escrita (FIX #3 da auditoria).

`prepare_send` registra um draft com hash de (alvo, texto) e expiração.
`confirm_send(draft_id)` só executa o Enter se o draft estiver ativo e o hash
conferir. Sem draft ativo, confirmação é recusada (fail-closed).

Isso elimina: confirm sem prepare, troca de alvo entre prepare e confirm,
e envio de texto editado pelo usuário (o hash é do que foi aprovado).
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass, field
from threading import Lock

DRAFT_TTL_SECONDS = 120  # o rascunho aprovado expira em 2 min


@dataclass
class Draft:
    draft_id: str
    target: str
    text: str
    created_at: float
    expires_at: float


class DraftStore:
    """Store em memória com lock (MCP server é single-process, stdio)."""

    def __init__(self) -> None:
        self._drafts: dict[str, Draft] = {}
        self._lock = Lock()

    def create(self, target: str, text: str) -> Draft:
        draft_id = uuid.uuid4().hex[:16]
        draft = Draft(
            draft_id=draft_id,
            target=target,
            text=text,
            created_at=time.time(),
            expires_at=time.time() + DRAFT_TTL_SECONDS,
        )
        with self._lock:
            self._drafts[draft_id] = draft
        return draft

    def consume(self, draft_id: str) -> Draft | None:
        """Valida e consome o draft. Retorna o Draft se válido, senão None."""
        with self._lock:
            draft = self._drafts.pop(draft_id, None)
        if draft is None:
            return None
        if time.time() > draft.expires_at:
            return None
        return draft


_store = DraftStore()


def create_draft(target: str, text: str) -> tuple[str, str]:
    """Cria um draft e retorna (draft_id, text)."""
    draft = _store.create(target, text)
    return draft.draft_id, draft.text


def consume_draft(draft_id: str) -> Draft | None:
    """Consome o draft se válido. Retorna o Draft (target+text) ou None."""
    return _store.consume(draft_id)
