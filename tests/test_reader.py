"""Testes do driver de leitura (reader)."""

import sqlite3
from pathlib import Path

import pytest

from whatsapp_mcp import reader
from tests.fixture import build_fixture


@pytest.fixture()
def db(tmp_path: Path):
    p = tmp_path / "ChatStorage.sqlite"
    build_fixture(p)
    return p


def test_list_chats(db):
    chats = reader.list_chats(db_path=db)
    assert len(chats) >= 2
    # conversa mais recente primeiro
    assert chats[0]["name"] in ("Você", "Grupo Teste")
    assert chats[0]["jid"] is None  # mascarado por padrão
    # tipo correto
    by_name = {c["name"]: c for c in chats}
    assert by_name["Grupo Teste"]["type"] == "group"
    assert by_name["Você"]["type"] == "direct"
    assert by_name["Grupo Teste"]["unread"] == 3


def test_list_chats_archived_excluded_by_default(db):
    chats = reader.list_chats(db_path=db)
    names = [c["name"] for c in chats]
    assert "Arquivada" not in names


def test_list_chats_include_archived(db):
    """include_archived=True traz as arquivadas com flag archived."""
    chats = reader.list_chats(include_archived=True, db_path=db)
    by_name = {c["name"]: c for c in chats}
    assert "Arquivada" in by_name
    assert by_name["Arquivada"]["archived"] is True
    assert by_name["Você"]["archived"] is False


def test_list_chats_excludes_status_stories(db):
    """Chats de Status/Stories (@status, @lid.status) não são conversas reais."""
    chats = reader.list_chats(db_path=db)
    names = [c["name"] for c in chats]
    assert "Status Jorge" not in names
    assert "Status Kedma" not in names
    # chats reais continuam presentes
    assert "Você" in names


def test_list_chats_unread_only(db):
    chats = reader.list_chats(unread_only=True, db_path=db)
    names = [c["name"] for c in chats]
    assert "Grupo Teste" in names
    assert "Você" not in names


def test_get_messages(db):
    res = reader.get_messages(1, db_path=db)
    assert "error" not in res
    msgs = res["messages"]
    assert len(msgs) == 2
    assert msgs[0]["text"] == "olá do teste"
    assert msgs[0]["from_me"] is False
    assert msgs[1]["text"] == "resposta minha"
    assert msgs[1]["from_me"] is True


def test_get_messages_limit(db):
    res = reader.get_messages(1, limit=1, db_path=db)
    assert len(res["messages"]) == 1


def test_get_messages_unknown_chat(db):
    res = reader.get_messages(999, db_path=db)
    assert "error" in res


def test_search_messages(db):
    res = reader.search_messages("keyword especial", db_path=db)
    assert res["count"] == 1
    assert res["results"][0]["chat_name"] == "Grupo Teste"


def test_search_messages_scoped(db):
    res = reader.search_messages("keyword especial", chat_id=1, db_path=db)
    assert res["count"] == 0  # está no chat 2, não no 1


def test_export_chat_json(db, tmp_path, monkeypatch):
    monkeypatch.setattr(reader.Path, "home", lambda: tmp_path)
    res = reader.export_chat(1, fmt="json", db_path=db)
    assert res["format"] == "json"
    assert Path(res["exported"]).exists()


def test_export_chat_md(db, tmp_path, monkeypatch):
    monkeypatch.setattr(reader.Path, "home", lambda: tmp_path)
    res = reader.export_chat(1, fmt="md", db_path=db)
    assert res["format"] == "md"
    content = Path(res["exported"]).read_text(encoding="utf-8")
    assert "olá do teste" in content


def test_db_read_only_never_writes(db):
    """Garante que as chamadas de leitura não alteram o arquivo."""
    import hashlib

    def sha(p):
        return hashlib.sha256(p.read_bytes()).hexdigest()

    before = sha(db)
    reader.list_chats(db_path=db)
    reader.get_messages(1, db_path=db)
    reader.search_messages("keyword", db_path=db)
    after = sha(db)
    assert before == after


def test_connect_missing_db(tmp_path):
    with pytest.raises(FileNotFoundError):
        reader.list_chats(db_path=tmp_path / "nope.sqlite")
