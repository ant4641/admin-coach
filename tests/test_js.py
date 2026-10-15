"""對不依賴 DOM 的前端邏輯（coach/web/intake.js）跑 node 內建的 test runner，並檢查所有 JS 語法。找不到 node 就跳過。"""

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
NODE = shutil.which("node")
JS_FILES = sorted((ROOT / "coach" / "web").glob("*.js")) + sorted((ROOT / "tests" / "js").glob("*.js"))


@pytest.mark.skipif(NODE is None, reason="找不到 node")
def test_intake_logic_passes_node_tests():
    files = [str(p) for p in sorted((ROOT / "tests" / "js").glob("*.test.js"))]
    assert files
    r = subprocess.run([NODE, "--test", *files], capture_output=True, text=True, encoding="utf-8", cwd=ROOT)
    assert r.returncode == 0, r.stdout + r.stderr


@pytest.mark.skipif(NODE is None, reason="找不到 node")
@pytest.mark.parametrize("path", JS_FILES, ids=lambda p: p.name)
def test_js_syntax(path):
    r = subprocess.run([NODE, "--check", str(path)], capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, r.stderr


def _home_btn_handler(js: str) -> str:
    start = js.index('$("home-btn").addEventListener')
    return js[start : js.index("});", start)]


def test_home_button_only_navigates():
    """「回首頁」不可清除案例；只有 reset-btn 走 clearFlowStorage。"""
    js = (ROOT / "coach" / "web" / "app.js").read_text(encoding="utf-8")
    handler = _home_btn_handler(js)
    assert 'go("#/")' in handler
    assert "clearFlowStorage" not in handler
    reset = js[js.index('$("reset-btn").addEventListener') :]
    assert "clearFlowStorage()" in reset[: reset.index("});")]


def test_home_shows_active_case_note_and_resumes_result():
    html = (ROOT / "coach" / "web" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "coach" / "web" / "app.js").read_text(encoding="utf-8")
    assert 'id="service-note"' in html and "有進行中的案例" in html and "重置，開新案例" in html
    assert "updateHomeNote()" in js
    # 從首頁點進服務且已有結果 → 直接進結果頁
    assert 'direct && s.status === "vfp"' in js
