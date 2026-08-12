"""Testes do driver web (web.whatsapp.com) — com mocks, sem Chrome real."""

import pytest

from whatsapp_mcp import web_driver


class FakeElement:
    def __init__(self, text="", attrs=None):
        self._text = text
        self._attrs = attrs or {}
        self._pressed = None

    def inner_text(self):
        return self._text

    def get_attribute(self, name):
        return self._attrs.get(name)

    def click(self):
        pass

    def fill(self, value):
        self._text = value

    def press(self, key):
        self._pressed = key

    def query_selector(self, sel):
        return None


class FakeLocator:
    """Simula o locator do playwright (get_by_text -> .first)."""

    def __init__(self, item=None):
        self._item = item

    @property
    def first(self):
        return self

    def wait_for(self, state=None, timeout=None):
        if self._item is None:
            raise web_driver.PWError("not found")
        return self._item

    def click(self):
        if self._item is not None:
            self._item.click()


class FakePageBase:
    """Base para Pages de teste: implementa get_by_text para o FIX #8."""

    def __init__(self):
        self._search_result = FakeElement(text="Minha Claro")

    def get_by_text(self, text, exact=False):
        return FakeLocator(self._search_result)


def _fake_connect(monkeypatch, page):
    class FakeCtx:
        pages = [page]

    class FakeBrowser:
        contexts = [FakeCtx()]

        def new_context(self):
            return FakeCtx()

    class FakePW:
        def chromium(self):
            return self

        def connect_over_cdp(self, url):
            return FakeBrowser()

        def stop(self):
            pass

    monkeypatch.setattr(web_driver, "_connect", lambda: (FakePW(), FakeBrowser()))
    monkeypatch.setattr(web_driver, "_ensure_chrome_running", lambda: True)
    monkeypatch.setattr(web_driver, "_get_wa_page", lambda b: page)
    monkeypatch.setattr(web_driver, "_wait_loaded", lambda p, timeout_ms=20000: None)


def test_list_chats_parses_names(monkeypatch):
    class Page:
        def query_selector_all(self, sel):
            return [FakeElement(text="Minha Claro"), FakeElement(text="Grupo Teste")]

        def query_selector(self, sel):
            return None

    _fake_connect(monkeypatch, Page())
    res = web_driver.list_chats()
    assert res["driver"] == "web"
    assert len(res["chats"]) == 2


def test_get_messages_opens_chat_and_reads(monkeypatch):
    msg = FakeElement(text="olá do web")
    search_box = FakeElement(text="")
    item = FakeElement(text="Minha Claro")

    class Page(FakePageBase):
        def query_selector(self, sel):
            if "compose" in sel:
                return None
            if "row" in sel or "list-item" in sel:
                return item
            if 'tab="3"' in sel:
                return search_box
            return None

        def query_selector_all(self, sel):
            return [msg]

        def wait_for_timeout(self, ms):
            pass

    _fake_connect(monkeypatch, Page())
    res = web_driver.get_messages("Minha Claro", limit=5)
    assert res["driver"] == "web"
    # FIX #2: conteúdo marcado como não confiável
    assert web_driver.UNTRUSTED_PREFIX in res["messages"][0]
    assert res["messages"][0][web_driver.UNTRUSTED_PREFIX] == "olá do web"


def test_prepare_send_prefills_without_sending(monkeypatch):
    input_box = FakeElement(text="")
    item = FakeElement(text="Minha Claro")
    search_box = FakeElement(text="")

    class Page(FakePageBase):
        def query_selector(self, sel):
            if "compose" in sel:
                return input_box
            if "row" in sel:
                return item
            if 'tab="3"' in sel:
                return search_box
            return None

        def query_selector_all(self, sel):
            return []

        def wait_for_timeout(self, ms):
            pass

    _fake_connect(monkeypatch, Page())
    res = web_driver.prepare_send("Minha Claro", "olá teste")
    assert res["status"] == "draft_prepared"
    assert res["confirmation_required"] is True
    assert res["text_preview"] == "olá teste"
    assert "draft_id" in res  # FIX #3
    assert input_box._pressed is None  # Enter NÃO foi pressionado


def test_confirm_send_presses_enter(monkeypatch):
    input_box = FakeElement(text="olá teste")

    class Page(FakePageBase):
        def query_selector(self, sel):
            if "compose" in sel:
                return input_box
            if 'tab="3"' in sel:
                return FakeElement(text="")
            return None

        def query_selector_all(self, sel):
            return []

        def wait_for_timeout(self, ms):
            pass

    _fake_connect(monkeypatch, Page())
    draft_id, _ = web_driver.create_draft("web:Minha Claro", "olá teste")
    res = web_driver.confirm_send(draft_id)
    assert res["status"] == "enter_sent"
    assert input_box._pressed == "Enter"


def test_confirm_send_rejects_without_draft(monkeypatch):
    """FIX #3: confirm sem draft ativo falha antes de tocar no Chrome."""
    monkeypatch.setattr(web_driver, "_ensure_chrome_running", lambda: True)
    res = web_driver.confirm_send("id-inexistente")
    assert "error" in res
    assert "draft_id" in res["error"]


def test_chat_not_found_returns_error(monkeypatch):
    class Page:
        def query_selector(self, sel):
            if "row" in sel or 'tab="3"' in sel:
                return None
            return None

        def query_selector_all(self, sel):
            return []

        def wait_for_timeout(self, ms):
            pass

    _fake_connect(monkeypatch, Page())
    res = web_driver.prepare_send("Inexistente", "x")
    assert "error" in res
