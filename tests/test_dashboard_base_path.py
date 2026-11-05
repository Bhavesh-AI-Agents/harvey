"""HARVEY_BASE_PATH and HARVEY_PUBLIC_ORIGIN: serving the dashboard under a path of a shared HTTPS host behind a proxy.

Both settings are off by default and must change nothing then; when set, only the one configured public host and
origin are let through, and the bearer token is still required for every API call."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import harvey.dashboard as dash

PUBLIC = "https://admin.example.test"


@pytest.fixture
def env(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(dash, "ENV_FILE", tmp_path / ".env")
    monkeypatch.setattr(dash, "DB_PATH", tmp_path / "data" / "harvey.db")
    dash.token_store.configure(tmp_path / "data" / "dashboard-token")
    return {"token": dash.token_store.get()}


def test_public_host_is_refused_unless_configured(env, monkeypatch):
    monkeypatch.delenv("HARVEY_PUBLIC_ORIGIN", raising=False)
    with TestClient(dash.app, base_url=PUBLIC) as c:
        assert c.get("/").status_code == 400


def test_configured_public_origin_is_accepted_but_still_needs_the_token(env, monkeypatch):
    monkeypatch.setenv("HARVEY_PUBLIC_ORIGIN", PUBLIC)
    with TestClient(dash.app, base_url=PUBLIC) as c:
        assert c.get("/").status_code == 200
        assert c.get("/api/stats", headers={"Origin": PUBLIC}).status_code == 401
        ok = c.get("/api/stats", headers={"Origin": PUBLIC, "Authorization": f"Bearer {env['token']}"})
        assert ok.status_code != 401 and ok.status_code != 403


def test_other_hosts_and_origins_stay_refused_with_a_public_origin_set(env, monkeypatch):
    monkeypatch.setenv("HARVEY_PUBLIC_ORIGIN", PUBLIC)
    auth = {"Authorization": f"Bearer {env['token']}"}
    with TestClient(dash.app, base_url="https://evil.example.test") as c:
        assert c.get("/", headers=auth).status_code == 400
    with TestClient(dash.app, base_url=PUBLIC) as c:
        assert c.get("/api/stats", headers={**auth, "Origin": "https://evil.example.test"}).status_code == 403
        assert c.get("/api/stats", headers={**auth, "Origin": "http://admin.example.test"}).status_code == 403


@pytest.mark.parametrize("value", ["http://admin.example.test", "https://admin.example.test/path", "admin.example.test", ""])
def test_malformed_public_origins_are_ignored(env, monkeypatch, value):
    monkeypatch.setenv("HARVEY_PUBLIC_ORIGIN", value)
    with TestClient(dash.app, base_url="https://admin.example.test") as c:
        assert c.get("/").status_code == 400


def test_base_path_prefixes_the_page_and_stylesheet(env, monkeypatch):
    monkeypatch.setattr(dash, "BASE_PATH", "/harvey")
    with TestClient(dash.app, base_url="http://127.0.0.1:5555") as c:
        html = c.get("/").text
        css = c.get("/static/app.css").text
    assert '<meta name="harvey-base" content="/harvey">' in html
    assert '"/harvey/static/auth.js"' in html and '"/static/' not in html
    assert "url('/harvey/static/fonts/" in css and "url('/static/" not in css


def test_no_base_path_changes_nothing(env, monkeypatch):
    monkeypatch.setattr(dash, "BASE_PATH", "")
    with TestClient(dash.app, base_url="http://127.0.0.1:5555") as c:
        html = c.get("/").text
    assert html == (dash.WEB_DIR / "index.html").read_text(encoding="utf-8")


def test_auth_js_prefixes_api_paths_with_the_base():
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    script = r"""
globalThis.window = {location: {origin: 'https://admin.example.test', pathname: '/harvey/', search: '', hash: ''}};
globalThis.location = window.location;
globalThis.history = {replaceState: () => {}};
globalThis.sessionStorage = {getItem: () => null, setItem: () => {}, removeItem: () => {}};
globalThis.document = {querySelector: s => s.includes('harvey-base') ? {content: '/harvey'} : null};
eval(require('fs').readFileSync(process.argv[1], 'utf8') + ';globalThis.T = {sameOriginUrl};');
process.stdout.write(JSON.stringify(['/api/x', '/harvey/api/y', '//evil.example/api'].map(p => { const u = T.sameOriginUrl(p); return u ? u.pathname : null; })));
"""
    js = Path(dash.__file__).resolve().parent / "web" / "auth.js"
    out = json.loads(subprocess.run([node, "-e", script, str(js)], capture_output=True, text=True, check=True).stdout)
    assert out == ["/harvey/api/x", "/harvey/api/y", None]
