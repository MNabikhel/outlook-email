"""The workspace page at /app: it loads at every address the workspace uses, its scripts come from this
computer with a version in their address, the classic pages link to it, and its scripts never put text
from the server into the page as HTML."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from controller_inbox import web
from controller_inbox.pipeline import ingest_demo

UI = Path(web.__file__).parent / "static" / "ui"


@pytest.fixture
def client(store, settings, as_of_now):
    ingest_demo(store, settings, now=as_of_now)
    return TestClient(web.create_app(settings, store))


def test_the_workspace_page_loads_at_every_address_with_versioned_scripts(client):
    for path in ("/app", "/app/folder/important", "/app/mail/demo-payroll", "/app/mail/demo-payroll/file/1?tab=tables", "/app/digest"):
        page = client.get(path)
        assert page.status_code == 200, path
        assert '<script type="importmap">' in page.text and "/static/ui/main.js?v=" in page.text
        assert page.headers["cache-control"] == "no-store"
    page = client.get("/app").text
    assert '"/static/ui/api.js": "/static/ui/api.js?v=' in page.replace("\\u002f", "/")
    for name in ("main.js", "api.js", "dom.js", "lists.js", "reader.js", "chat.js", "format.js", "pages.js", "palette.js", "theme.js", "app.css"):
        assert client.get(f"/static/ui/{name}").status_code == 200, name
    # Nothing is fetched from outside this computer.
    assert not re.search(r"""(src|href)=["']?(https?:)?//""", page)


def test_the_classic_pages_link_to_the_workspace_and_back(client):
    assert 'href="/app"' in client.get("/").text
    assert 'href="/app"' in client.get("/inbox").text
    script = client.get("/static/ui/main.js").text
    assert 'href: "/"' in script and "Classic view" in script


def test_scripts_never_put_server_text_into_html_unescaped():
    folder = UI
    for path in folder.glob("*.js"):
        text = path.read_text(encoding="utf-8")
        uses = text.count(".innerHTML")
        if path.name == "chat.js":
            # The one use: an answer, escaped first by formatAnswer (see format.js).
            assert uses == 1 and "innerHTML = formatAnswer(" in text
        else:
            assert uses == 0, path.name
    assert "escapeHtml(sentence)" in (folder / "format.js").read_text(encoding="utf-8")


def test_a_file_name_with_a_bracketed_number_stays_one_link(tmp_path):
    import json
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js isn't installed")
    sources = [{"n": 1, "id": "e1", "subject": "Remittance", "files": [{"n": 1, "name": "Invoice [1].pdf", "text": True}]}]
    script = tmp_path / "answer.mjs"
    script.write_text(
        f"import {{ formatAnswer }} from {json.dumps((UI / 'format.js').as_uri())};\n"
        f"console.log(formatAnswer('See Invoice [1].pdf for the total [1].', {json.dumps(sources)}));\n"
    )
    html = subprocess.run([node, str(script)], capture_output=True, text=True, check=True).stdout
    assert html.count("<a ") == 2 and "<a" not in html.split("Invoice [1].pdf</a>")[0].split("<a ", 2)[-1].split(">", 1)[1]
    assert 'title="Open Invoice [1].pdf">Invoice [1].pdf</a> for the total <a class="cite"' in html
