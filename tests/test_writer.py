"""Testes do driver de escrita (writer) — com mocks, sem tocar no app real."""

from unittest.mock import patch

from whatsapp_mcp import writer


def test_prepare_send_builds_correct_url(monkeypatch):
    opened = []

    def fake_run(cmd):
        opened.append(cmd)
        return ""

    monkeypatch.setattr(writer, "_run", fake_run)
    monkeypatch.setattr(writer, "_resolve_target", lambda **k: {"jid": "554991539437@s.whatsapp.net", "name": "Você"})

    res = writer.prepare_send(phone="+554991539437", text="olá mundo")
    assert res["status"] == "draft_prepared"
    assert res["confirmation_required"] is True
    assert "olá mundo" in res["text_preview"]
    # URL correta: phone sem @suffix, texto urlencoded
    url = opened[0][1]
    assert url.startswith("whatsapp://send?phone=554991539437&text=")
    assert "ol%C3%A1%20mundo" in url  # percent-encoding correto do urllib.quote


def test_prepare_send_does_not_press_enter(monkeypatch):
    calls = []

    def fake_run(cmd):
        calls.append(cmd)
        return ""

    monkeypatch.setattr(writer, "_run", fake_run)
    monkeypatch.setattr(writer, "_resolve_target", lambda **k: {"jid": "554991539437@s.whatsapp.net", "name": "Você"})

    writer.prepare_send(phone="+554991539437", text="texto")
    # Nenhum key code 36 (Return) pode ter sido disparado no prepare
    enter_calls = [c for c in calls if any("key code 36" in str(x) for x in c)]
    assert enter_calls == []


def test_confirm_send_presses_enter(monkeypatch):
    calls = []

    def fake_run(cmd):
        calls.append(cmd)
        return ""

    monkeypatch.setattr(writer, "_run", fake_run)
    # cria um draft válido antes de confirmar
    draft_id, _ = writer.create_draft("554991539437@s.whatsapp.net", "texto")
    res = writer.confirm_send(draft_id)
    assert res["status"] == "enter_sent"
    joined = " ".join(str(c) for c in calls)
    assert "key code 36" in joined


def test_confirm_send_rejects_without_draft(monkeypatch):
    """FIX #3: confirm_send sem draft_id ativo deve falhar (fail-closed)."""
    calls = []

    def fake_run(cmd):
        calls.append(cmd)
        return ""

    monkeypatch.setattr(writer, "_run", fake_run)
    res = writer.confirm_send("id-inexistente")
    assert "error" in res
    assert "draft_id" in res["error"]
    # Enter NÃO foi pressionado
    joined = " ".join(str(c) for c in calls)
    assert "key code 36" not in joined


def test_confirm_send_rejects_consumed_draft(monkeypatch):
    """FIX #3: draft consumido não pode ser reutilizado (um Enter por draft)."""
    calls = []

    def fake_run(cmd):
        calls.append(cmd)
        return ""

    monkeypatch.setattr(writer, "_run", fake_run)
    draft_id, _ = writer.create_draft("554991539437@s.whatsapp.net", "texto")
    assert writer.confirm_send(draft_id)["status"] == "enter_sent"
    # segunda confirmação com o mesmo draft_id falha (já consumido)
    res = writer.confirm_send(draft_id)
    assert "error" in res


def test_resolve_target_phone_formats_brazil(monkeypatch):
    monkeypatch.setattr(writer, "_connect", lambda: (_ for _ in ()).throw(AssertionError("não deve abrir banco")))
    target = writer._resolve_target(phone="+5549991539437")
    assert target["jid"] == "5549991539437@s.whatsapp.net"
    # sem + também funciona, e normaliza para 55
    target2 = writer._resolve_target(phone="49991539437")
    assert target2["jid"] == "5549991539437@s.whatsapp.net"


def test_clear_draft_uses_select_all_delete(monkeypatch):
    calls = []

    def fake_run(cmd):
        calls.append(cmd)
        return ""

    monkeypatch.setattr(writer, "_run", fake_run)
    monkeypatch.setattr(writer, "_resolve_target", lambda **k: {"jid": "554991539437@s.whatsapp.net", "name": "Você"})

    res = writer.clear_draft(phone="+554991539437")
    assert res["status"] == "draft_cleared"
    joined = " ".join(str(c) for c in calls)
    assert 'keystroke "a" using command down' in joined
    assert "key code 51" in joined  # Delete


def test_verify_sent_queries_db(monkeypatch):
    fake_row = {"text": "mensagem teste", "at": 800_000_000, "from_me": 1}

    class FakeConn:
        def execute(self, q, params):
            return self

        def fetchone(self):
            return fake_row

        def close(self):
            pass

    monkeypatch.setattr(writer, "_connect", lambda db_path=None: FakeConn())
    res = writer.verify_sent(text_substring="mensagem teste")
    assert res["sent"] is True
    assert res["at_unix"] == 800_000_000 + 978307200
