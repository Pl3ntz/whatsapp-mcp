"""Testes do gate de estado server-side (FIX #3 da auditoria)."""

from whatsapp_mcp import state


def test_create_and_consume_ok():
    draft_id, text = state.create_draft("554991539437@s.whatsapp.net", "olá")
    draft = state.consume_draft(draft_id)
    assert draft is not None
    assert draft.target == "554991539437@s.whatsapp.net"
    assert draft.text == "olá"


def test_consume_is_one_shot():
    """Um draft serve uma única confirmação."""
    draft_id, _ = state.create_draft("x", "texto")
    assert state.consume_draft(draft_id) is not None
    assert state.consume_draft(draft_id) is None


def test_consume_rejects_unknown():
    assert state.consume_draft("nao-existe") is None


def test_draft_expires(monkeypatch):
    """Draft expira após o TTL (120s)."""
    fake_now = 1000.0
    monkeypatch.setattr(state.time, "time", lambda: fake_now)
    draft_id, _ = state.create_draft("x", "texto")
    # avança o relógio além do TTL
    fake_now = 1000.0 + state.DRAFT_TTL_SECONDS + 1
    assert state.consume_draft(draft_id) is None


def test_draft_keeps_approved_text():
    """O draft guarda o texto aprovado (usado no confirm para re-aplicar)."""
    draft_id, text = state.create_draft("chat:alvo", "texto aprovado")
    assert text == "texto aprovado"
    draft = state.consume_draft(draft_id)
    assert draft.text == "texto aprovado"
