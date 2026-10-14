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
