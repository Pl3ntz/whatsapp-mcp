"""Testes de mídia (F1): list_media, get_media, get_messages(include_media)."""

import hashlib
from types import SimpleNamespace

import pytest

from whatsapp_mcp import reader
from tests.fixture import build_fixture


@pytest.fixture()
def ctx(tmp_path, monkeypatch):
    """Banco sintético + arquivos de mídia fake + MEDIA_ROOT apontando pro tmp."""
    db = tmp_path / "ChatStorage.sqlite"
    build_fixture(db, media_root=tmp_path)
    monkeypatch.setattr(reader, "MEDIA_ROOT", tmp_path / "Message" / "Media")
    return SimpleNamespace(db=db, tmp=tmp_path, media_root=tmp_path / "Message" / "Media")


def test_list_media_lists_items_without_paths_or_jids(ctx):
    res = reader.list_media(1, db_path=ctx.db)
    assert "error" not in res
    assert res["chat"]["name"] == "Você"
    assert res["count"] == 4
    # nenhum path e nenhum JID nos outputs
    blob = str(res)
    assert "s.whatsapp.net" not in blob
    assert "Message/Media" not in blob
    assert "Media/" not in blob
    cats = {m["category"] for m in res["media"]}
    assert cats == {"image", "audio"}


def test_list_media_unknown_chat(ctx):
    res = reader.list_media(999, db_path=ctx.db)
    assert "error" in res


def test_list_media_type_filter(ctx):
    res = reader.list_media(1, media_type="image", db_path=ctx.db)
    assert res["count"] == 3
    assert all(m["category"] == "image" for m in res["media"])
    res2 = reader.list_media(1, media_type="audio", db_path=ctx.db)
    assert res2["count"] == 1
    assert res2["media"][0]["ext"] == "ogg"


def test_list_media_invalid_type(ctx):
    with pytest.raises(ValueError):
        reader.list_media(1, media_type="gif", db_path=ctx.db)


def test_list_media_file_exists_flag(ctx):
    res = reader.list_media(1, db_path=ctx.db)
    by_id = {m["media_id"]: m for m in res["media"]}
    assert by_id[1]["file_exists"] is True   # abc123.jpg existe no tmp
    assert by_id[3]["file_exists"] is False  # ghost.jpg não existe (fantasma)
    assert by_id[3]["size"] == 999


def test_list_media_traversal_path_rejected(ctx):
    """ZMEDIALOCALPATH com ../ não vaza: file_exists=false e nunca vira path."""
    res = reader.list_media(1, db_path=ctx.db)
    by_id = {m["media_id"]: m for m in res["media"]}
    assert by_id[4]["file_exists"] is False  # Media/../etc/passwd rejeitado


def test_list_media_limit_clamp_and_before(ctx):
    res = reader.list_media(1, limit=2, db_path=ctx.db)
    assert res["count"] == 2
    # mais recentes primeiro: media 1 (msg10), depois 2 (msg11)
    ids = [m["media_id"] for m in res["media"]]
    assert ids[0] == 1
    # before paginação: mesmo contrato de get_messages (compara com ZMESSAGEDATE cru)
    before_ts = 800_000_000 - 2000  # core data ts da msg10
    res2 = reader.list_media(1, before=before_ts, db_path=ctx.db)
    assert res2["count"] == 3  # msg11, msg12, msg13 (todas mais antigas)


def test_get_media_no_path_by_default(ctx):
    res = reader.get_media(1, 1, db_path=ctx.db)
    assert "error" not in res
    assert "path" not in res  # include_path=False NÃO vaza path
    assert res["media_id"] == 1
    assert res["category"] == "image"
    assert res["caption"] == "foto do teste"
    assert res["caption_untrusted"] is True
    assert res["untrusted"] is True
    assert "s.whatsapp.net" not in str(res)


def test_get_media_with_path(ctx):
    res = reader.get_media(1, 1, include_path=True, db_path=ctx.db)
    expected = (ctx.media_root / "554991539437@s.whatsapp.net" / "0" / "1" / "abc123.jpg").resolve()
    assert res["path"] == str(expected)
    assert res["file_exists"] is True


def test_get_media_cross_chat_blocked(ctx):
    """media_id é opaco + escopo de conversa: mídia do chat 2 não é visível via chat 1."""
    res = reader.get_media(1, 5, db_path=ctx.db)
    assert "error" in res
    res_ok = reader.get_media(2, 5, db_path=ctx.db)
    assert "error" not in res_ok


def test_get_media_traversal_path(ctx):
    res = reader.get_media(1, 4, include_path=True, db_path=ctx.db)
    assert res["path"] is None  # rejeitado (containment)
    assert res["file_exists"] is False


def test_get_messages_include_media(ctx):
    res = reader.get_messages(1, include_media=True, db_path=ctx.db)
    msgs = res["messages"]
    assert len(msgs) == 6  # 2 texto + 4 mídia
    # mais antiga primeiro; mídia entra com marcador e media_id
    assert msgs[0]["text"] == "[media: image]"       # msg13
    assert msgs[0]["media_id"] == 4
    assert msgs[2]["text"] == "[media: audio]"        # msg11
    assert msgs[2]["media_id"] == 2
    assert msgs[4]["text"] == "olá do teste"          # msg1 (texto puro, sem media_id)
    assert "media_id" not in msgs[4]


def test_get_messages_include_media_caption_marker(ctx):
    """Imagem com caption: texto continua + marcador de mídia anexado."""
    res = reader.get_messages(2, include_media=True, db_path=ctx.db)
    msgs = res["messages"]
    legenda = [m for m in msgs if m.get("media_id") == 6]
    assert legenda and legenda[0]["text"] == "foto com legenda\n[media: image]"


def test_get_messages_default_unchanged(ctx):
    """include_media=False mantém comportamento antigo (só texto)."""
    res = reader.get_messages(1, db_path=ctx.db)
    assert len(res["messages"]) == 2
    assert all("media_id" not in m for m in res["messages"])


def test_media_db_read_only_never_writes(ctx):
    def sha(p):
        return hashlib.sha256(p.read_bytes()).hexdigest()

    before = sha(ctx.db)
    reader.list_media(1, db_path=ctx.db)
    reader.list_media(1, media_type="audio", db_path=ctx.db)
    reader.get_media(1, 1, include_path=True, db_path=ctx.db)
    reader.get_messages(1, include_media=True, db_path=ctx.db)
    assert sha(ctx.db) == before
