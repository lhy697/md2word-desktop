import json
from pathlib import Path
import threading
import time
import pytest
from md2word.pandoc import PandocError, PandocRunner
from md2word.resources import ResourceLocator
from md2word.spike import smoke, math_probe

def walk(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from walk(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk(item)

def test_real_smoke_and_existing_output_protection(tmp_path):
    report = smoke(tmp_path / "中文 空格输出")
    assert report["math_objects"] == 4
    from docx import Document
    document = Document(tmp_path / "中文 空格输出/smoke.docx")
    assert len(document.tables) == 1
    assert document.tables[0].cell(1, 0).text == "12"
    target = tmp_path / "中文 空格输出/smoke.docx"
    original = target.read_bytes()
    with pytest.raises(FileExistsError):
        smoke(target.parent)
    assert target.read_bytes() == original

def test_reader_boundaries(tmp_path):
    runner = PandocRunner()
    sample = r"""
---
title: 不得隐藏 2468
---

货币 $20 和 $30；转义 \$40。

公式 \(x+1\) 与 $y_2$。

\[
z^2
\]

```text
$not_math$
\(not_math\)
```
"""
    ast = runner.parse(sample, cwd=tmp_path)
    assert ast["meta"] == {}
    nodes = list(walk(ast))
    math = [x["c"][1] for x in nodes if x.get("t") == "Math"]
    assert math == ["x+1", "y_2", "\nz^2\n"]
    assert any(x.get("t") == "Str" and "2468" in x.get("c", "") for x in nodes)
    assert any(x.get("t") == "CodeBlock" and "$not_math$" in x["c"][1] for x in nodes)
    profile = json.loads(ResourceLocator().file("reader-profile.json").read_text("utf-8"))
    actual = runner.run(["--list-extensions=markdown"], cwd=tmp_path).stdout.decode().splitlines()
    assert set(profile["effective_extensions"]) == {line[1:] for line in actual}

def test_math_failure_is_not_exit_code_success(tmp_path):
    report = math_probe(tmp_path / "math")
    rows = report["singles"]
    assert rows[0]["status"] == rows[5]["status"] == rows[6]["status"] == "structurally_passed"
    assert rows[4]["returncode"] == 0
    assert rows[4]["status"] == "source_fallback"
    assert rows[7]["status"] == "source_fallback"
    assert rows[8]["status"] == "uncertain"
    assert len({p["marker"] for p in report["batch"] if p["marker"]}) == 9

def test_cancellation_timeout_and_invalid_ast(tmp_path):
    runner = PandocRunner()
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(PandocError, match="cancelled"):
        runner.run(["--version"], cwd=tmp_path, cancel=cancel)
    with pytest.raises(PandocError) as exc:
        runner.run(["--version"], cwd=tmp_path, deadline=time.monotonic() - 1)
    assert exc.value.code == "EXPORT_TIMEOUT"
    with pytest.raises(PandocError) as exc:
        runner.run(["--from=json", "--to=docx"], cwd=tmp_path, stdin=b"not-json")
    assert exc.value.code == "PANDOC_FAILED"

def test_resource_path_escape():
    with pytest.raises(ValueError):
        ResourceLocator().file("../pyproject.toml")

def test_preview_request_whitelist(tmp_path):
    from PySide6.QtCore import QUrl
    from md2word.preview import LocalOnlyInterceptor
    root = tmp_path / "preview"
    root.mkdir()
    allowed = root / "test.js"
    allowed.write_text("void 0", "utf-8")
    secret = tmp_path / "outside.txt"
    secret.write_text("private", "utf-8")
    interceptor = LocalOnlyInterceptor(root)
    class Request:
        blocked = False
        def __init__(self, url):
            self.url = url
        def requestUrl(self):
            return self.url
        def block(self, value):
            self.blocked = value
    for url, expected in [
        (QUrl.fromLocalFile(str(allowed)), False),
        (QUrl.fromLocalFile(str(secret)), True),
        (QUrl("https://example.invalid/asset.js"), True),
        (QUrl("file:///C:/Windows/win.ini"), True),
    ]:
        request = Request(url)
        interceptor.interceptRequest(request)
        assert request.blocked == expected

def test_running_child_timeout_and_cancel(tmp_path):
    import ctypes
    import sys
    runner = PandocRunner()
    runner.executable = Path(sys.executable)  # real controlled long-lived child
    script = tmp_path / "slow.py"
    pid_file = tmp_path / "pid.txt"
    script.write_text("import os, pathlib, time\npathlib.Path('pid.txt').write_text(str(os.getpid()))\ntime.sleep(20)\n", "utf-8")
    for cancelling in (False, True):
        cancel = threading.Event()
        timer = threading.Timer(0.5, cancel.set) if cancelling else None
        if timer:
            timer.start()
        try:
            with pytest.raises(PandocError) as exc:
                runner.run([str(script)], cwd=tmp_path,
                           deadline=time.monotonic() + (10 if cancelling else 0.5),
                           cancel=cancel)
            assert exc.value.code == ("CANCELLED" if cancelling else "EXPORT_TIMEOUT")
            pid = int(pid_file.read_text())
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if handle:
                code = ctypes.c_ulong()
                ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
                ctypes.windll.kernel32.CloseHandle(handle)
                assert code.value != 259  # STILL_ACTIVE
        finally:
            if timer:
                timer.cancel()
