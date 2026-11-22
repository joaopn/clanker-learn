"""The Editor: code-server, in a project that was given it, behind its lazy-start stub, reached through the real web
server's origin-port proxy in a real outer container in privileged mode, as test_web_apps.py
reaches a web app. Requests come from the neighbour container (`peer`).
"""
import time

import pytest
from conftest import Outer
from test_broker import set_password
from test_web import PASSWORD, Page, act, proof, said, web_on
from test_web_apps import on_port, services, session_for

pytestmark = pytest.mark.integration

EDITOR_PORT = 8443


def test_the_editor_of_a_new_project_opens_through_its_origin_port(image, peer, preload):
    one = Outer()
    try:
        web_on(one, preload)
        set_password(one, PASSWORD)
        page = Page(one)
        assert page.post("/broker/login", {"proof": proof()})["status"] == 200
        deadline = time.monotonic() + 120                             # the keeper takes the editor's distribution
        while not str(one.state().get("editor", "")).startswith("ready"):
            assert time.monotonic() < deadline, one.state()
            time.sleep(0.5)
        act(page, "/broker/project", {"name": "editing", "editor": True})
        assert list(services(page, "editing"))[:2] == ["editor", "files"]   # the page's tabs, in this order
        opened = page.post("/broker/project/editing/origin", {"port": EDITOR_PORT})   # listed always, never detected
        assert opened["status"] == 200 and said(opened)["ok"], opened
        origin = said(opened)["origin_port"]
        session_for(page, "editing")
        editor = on_port(one, page, origin)
        deadline = time.monotonic() + 60                              # once code-server listens
        while (answer := editor.get("/"))["status"] != 200:
            assert time.monotonic() < deadline, answer
            time.sleep(0.5)
        body = answer["body"]   # VS Code's workbench, as code-server names itself in its product settings
        assert "vscode-workbench-web-configuration" in body and "nameLong&quot;:&quot;code-server" in body, body[:2000]
    finally:
        one.remove()
