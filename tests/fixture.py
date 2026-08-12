"""Fixture SQLite mínima replicando o schema Core Data do WhatsApp."""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE ZWACHATSESSION (
    Z_PK INTEGER PRIMARY KEY,
    ZPARTNERNAME TEXT,
    ZCONTACTJID TEXT,
    ZSESSIONTYPE INTEGER,
    ZUNREADCOUNT INTEGER,
    ZLASTMESSAGEDATE INTEGER,
    ZLASTMESSAGETEXT TEXT,
    ZARCHIVED INTEGER,
    ZREMOVED INTEGER,
    ZGROUPINFO INTEGER,
    ZSAVEDINPUT TEXT
);
CREATE TABLE ZWAMESSAGE (
    Z_PK INTEGER PRIMARY KEY,
    ZCHATSESSION INTEGER,
    ZISFROMME INTEGER,
    ZMESSAGEDATE INTEGER,
    ZTEXT TEXT,
    ZMESSAGETYPE INTEGER,
    ZPUSHNAME TEXT
);
CREATE TABLE ZWAGROUPINFO (
    Z_PK INTEGER PRIMARY KEY,
    ZCREATORJID TEXT,
    ZSUBJECTTIMESTAMP INTEGER
);
CREATE TABLE ZWAMEDIAITEM (
    Z_PK INTEGER PRIMARY KEY,
    ZMESSAGE INTEGER,
    ZMEDIALOCALPATH TEXT,
    ZTHUMBNAILLOCALPATH TEXT,
    ZFILESIZE INTEGER,
    ZMOVIEDURATION INTEGER,
    ZTITLE TEXT,
    ZMEDIAKEY BLOB,
    ZVCARDSTRING TEXT
);
"""


def _make_media_files(root: Path) -> None:
    """Cria arquivos fake em <root>/Message/Media/<jid>/... (paths do fixture)."""
    jid1 = "554991539437@s.whatsapp.net"
    jid2 = "120363425942542830@g.us"
    files = {
        "abc123.jpg": b"\xff\xd8\xff" + b"\x00" * 128,          # magic jpg
        "abc123.thumb": b"\xff\xd8\xff" + b"\x00" * 64,         # thumb jpeg
        "audio01.ogg": b"OggS" + b"\x00" * 256,                 # magic ogg (nativo do whisper)
        "doc01.pdf": b"%PDF-1.4\n%test" + b"\x00" * 64,         # magic pdf
        "legenda.jpg": b"\xff\xd8\xff" + b"\x00" * 96,          # imagem com caption
        "legenda.thumb": b"\xff\xd8\xff" + b"\x00" * 32,
    }
    for rel, data in files.items():
        d = root / "Message" / "Media" / (jid1 if rel != "doc01.pdf" else jid2) / "0" / "1"
        d.mkdir(parents=True, exist_ok=True)
        (d / rel).write_bytes(data)


def build_fixture(path: Path, media_root: Path | None = None) -> None:
    """Cria o banco sintético. Se media_root for dado, cria também os arquivos
    de mídia fake sob <media_root>/Message/Media/..."""
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    now = 800_000_000  # ~2026, core data epoch
    conn.execute(
        "INSERT INTO ZWACHATSESSION (Z_PK, ZPARTNERNAME, ZCONTACTJID, ZSESSIONTYPE, ZUNREADCOUNT, ZLASTMESSAGEDATE, ZLASTMESSAGETEXT, ZARCHIVED, ZREMOVED) VALUES (1, 'Você', '554991539437@s.whatsapp.net', 0, 0, ?, 'ultima msg', 0, 0)",
        (now,),
    )
    conn.execute(
        "INSERT INTO ZWACHATSESSION (Z_PK, ZPARTNERNAME, ZCONTACTJID, ZSESSIONTYPE, ZUNREADCOUNT, ZLASTMESSAGEDATE, ZLASTMESSAGETEXT, ZARCHIVED, ZREMOVED) VALUES (2, 'Grupo Teste', '120363425942542830@g.us', 1, 3, ?, 'msg grupo', 0, 0)",
        (now,),
    )
    conn.execute(
        "INSERT INTO ZWACHATSESSION (Z_PK, ZPARTNERNAME, ZCONTACTJID, ZSESSIONTYPE, ZUNREADCOUNT, ZLASTMESSAGEDATE, ZLASTMESSAGETEXT, ZARCHIVED, ZREMOVED) VALUES (3, 'Arquivada', '554900000001@s.whatsapp.net', 0, 0, ?, 'msg', 1, 0)",
        (now - 1000,),
    )
    conn.execute(
        "INSERT INTO ZWACHATSESSION (Z_PK, ZPARTNERNAME, ZCONTACTJID, ZSESSIONTYPE, ZUNREADCOUNT, ZLASTMESSAGEDATE, ZLASTMESSAGETEXT, ZARCHIVED, ZREMOVED) VALUES (4, 'Status Jorge', '260142025183479@lid.status', 0, 39, ?, 'status payload', 0, 0)",
        (now,),
    )
    conn.execute(
        "INSERT INTO ZWACHATSESSION (Z_PK, ZPARTNERNAME, ZCONTACTJID, ZSESSIONTYPE, ZUNREADCOUNT, ZLASTMESSAGEDATE, ZLASTMESSAGETEXT, ZARCHIVED, ZREMOVED) VALUES (5, 'Status Kedma', '554999715418@status', 0, 25, ?, 'status payload', 0, 0)",
        (now,),
    )
    conn.execute(
        "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZPUSHNAME) VALUES (1, 1, 0, ?, 'olá do teste', 0, 'Vitor')",
        (now - 100,),
    )
    conn.execute(
        "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZPUSHNAME) VALUES (2, 1, 1, ?, 'resposta minha', 0, NULL)",
        (now - 50,),
    )
    conn.execute(
        "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZPUSHNAME) VALUES (3, 2, 0, ?, 'mensagem de grupo com keyword especial', 0, 'João')",
        (now,),
    )
    # --- mídia (mensagens SEM ZTEXT; ZMESSAGEDATE distinto para ordem determinística) ---
    conn.execute(
        "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZPUSHNAME) VALUES (10, 1, 0, ?, NULL, 1, NULL)",
        (now - 2000,),
    )
    conn.execute(
        "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZPUSHNAME) VALUES (11, 1, 1, ?, NULL, 3, NULL)",
        (now - 3000,),
    )
    conn.execute(
        "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZPUSHNAME) VALUES (12, 1, 0, ?, NULL, 1, NULL)",
        (now - 4000,),
    )
    conn.execute(
        "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZPUSHNAME) VALUES (13, 1, 1, ?, NULL, 1, NULL)",
        (now - 5000,),
    )
    conn.execute(
        "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZPUSHNAME) VALUES (14, 2, 0, ?, NULL, 8, NULL)",
        (now - 1500,),
    )
    # imagem COM caption (ZTEXT preenchido) — para include_media com marcador
    conn.execute(
        "INSERT INTO ZWAMESSAGE (Z_PK, ZCHATSESSION, ZISFROMME, ZMESSAGEDATE, ZTEXT, ZMESSAGETYPE, ZPUSHNAME) VALUES (15, 2, 1, ?, 'foto com legenda', 1, NULL)",
        (now - 1200,),
    )
    # --- ZWAMEDIAITEM ---
    jid1 = "554991539437@s.whatsapp.net"
    jid2 = "120363425942542830@g.us"
    media_rows = [
        # (Z_PK, ZMESSAGE, localpath, thumbpath, size, duration, title)
        (1, 10, f"Media/{jid1}/0/1/abc123.jpg", f"Media/{jid1}/0/1/abc123.thumb", 132, None, "foto do teste"),
        (2, 11, f"Media/{jid1}/0/1/audio01.ogg", None, 260, 5000, "audio nota"),
        (3, 12, f"Media/{jid1}/0/1/ghost.jpg", None, 999, None, "foto fantasma"),   # arquivo NÃO existe
        (4, 13, "Media/../etc/passwd", None, 1, None, "traversal"),                 # path rejeitado
        (5, 14, f"Media/{jid2}/0/1/doc01.pdf", None, 100, None, "documento grupo"),
        (6, 15, f"Media/{jid1}/0/1/legenda.jpg", f"Media/{jid1}/0/1/legenda.thumb", 100, None, "legenda img"),
    ]
    for z_pk, z_msg, lp, tp, size, dur, title in media_rows:
        conn.execute(
            "INSERT INTO ZWAMEDIAITEM (Z_PK, ZMESSAGE, ZMEDIALOCALPATH, ZTHUMBNAILLOCALPATH, ZFILESIZE, ZMOVIEDURATION, ZTITLE, ZMEDIAKEY, ZVCARDSTRING) VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
            (z_pk, z_msg, lp, tp, size, dur, title),
        )
    conn.commit()
    conn.close()
    if media_root is not None:
        _make_media_files(media_root)
