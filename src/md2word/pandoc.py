from dataclasses import dataclass
from pathlib import Path
import json
import os
import subprocess
import threading
import time
from typing import Sequence
from .resources import ResourceLocator

class PandocError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code

@dataclass(frozen=True)
class RunResult:
    stdout: bytes
    stderr: bytes
    elapsed_seconds: float
    returncode: int = 0

class PandocRunner:
    """One controlled subprocess implementation; API reserves shared deadline/cancel."""
    MAX_CAPTURE = 32 * 1024 * 1024

    def __init__(self, resources: ResourceLocator | None = None):
        self.resources = resources or ResourceLocator()
        self.executable = self.resources.pandoc
        self._verified_version = None

    def run(self, args: Sequence[str], *, cwd: Path, stdin: bytes = b"",
            deadline: float | None = None,
            cancel: threading.Event | None = None) -> RunResult:
        if isinstance(args, (str, bytes)):
            raise TypeError("Arguments must be an array")
        deadline = deadline if deadline is not None else time.monotonic() + 60
        if cancel is not None and cancel.is_set():
            raise PandocError("CANCELLED", "Conversion cancelled")
        if time.monotonic() >= deadline:
            raise PandocError("EXPORT_TIMEOUT", "Conversion deadline expired")
        started = time.monotonic()
        # Task-local spool files avoid pipe deadlock and uncontrolled memory capture.
        import tempfile
        with tempfile.TemporaryFile(dir=cwd) as out, tempfile.TemporaryFile(dir=cwd) as err, \
                tempfile.TemporaryFile(dir=cwd) as inp:
            inp.write(stdin)
            inp.seek(0)
            with subprocess.Popen(
                [str(self.executable), *args], cwd=cwd, stdin=inp,
                stdout=out, stderr=err, shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ) as proc:
                try:
                    while proc.poll() is None:
                        if cancel is not None and cancel.is_set():
                            raise PandocError("CANCELLED", "Conversion cancelled")
                        if time.monotonic() >= deadline:
                            raise PandocError("EXPORT_TIMEOUT", "Conversion deadline expired")
                        if out.tell() + err.tell() > self.MAX_CAPTURE:
                            raise PandocError("OUTPUT_LIMIT", "Conversion output exceeds spike limit")
                        time.sleep(0.02)
                except BaseException:
                    if proc.poll() is None:
                        proc.terminate()
                        try:
                            proc.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.wait()
                    raise
            out.seek(0)
            err.seek(0)
            stdout = out.read(min(os.fstat(out.fileno()).st_size, self.MAX_CAPTURE + 1))
            stderr = err.read(min(os.fstat(err.fileno()).st_size, self.MAX_CAPTURE + 1))
        if len(stdout) + len(stderr) > self.MAX_CAPTURE:
            raise PandocError("OUTPUT_LIMIT", "Conversion output exceeds spike limit")
        if proc.returncode:
            # Raw output can contain source text: don't include it in normal diagnostics.
            raise PandocError("PANDOC_FAILED", f"Pandoc exited with code {proc.returncode}")
        return RunResult(stdout, stderr, time.monotonic() - started)

    def ensure_version(self, profile, *, cwd: Path, **kwargs):
        if self._verified_version != profile.engine_version:
            actual = self.run(["--version"], cwd=cwd, **kwargs).stdout.decode("utf-8").splitlines()[0]
            if actual != profile.engine_version:
                raise PandocError("ENGINE_VERSION_MISMATCH", "Bundled engine version differs from profile")
            self._verified_version = actual
    def parse(self, text: str, *, cwd: Path, profile=None, **kwargs) -> dict:
        if profile is None:
            from .parsing import load_reader_profile
            profile = load_reader_profile(self.resources)
        self.ensure_version(profile, cwd=cwd, **kwargs)
        result = self.run(["--from", profile.reader, "--to", "json"],
                          cwd=cwd, stdin=text.encode("utf-8"), **kwargs)
        ast = json.loads(result.stdout)
        if not isinstance(ast, dict):
            raise PandocError("AST_INVALID", "Expected a JSON document object")
        if ast.get("pandoc-api-version") != list(profile.ast_api_version):
            raise PandocError("AST_VERSION_UNSUPPORTED", "Unexpected AST API version")
        return ast

    def write_docx(self, ast: dict, target: Path, *, cwd: Path, reference=None,
                   profile=None, **kwargs) -> RunResult:
        # This adapter writes task files only. User-target transactions belong to stage 05.
        if profile is not None:
            self.ensure_version(profile, cwd=cwd, **kwargs)
        options = ["--from=json", "--to=docx", "--output", str(target),
                   "--log", str(cwd / "pandoc-log.json")]
        if reference is not None:
            options.extend(["--reference-doc", str(reference), "--no-highlight"])
        return self.run(options,
                        cwd=cwd, stdin=json.dumps(ast, ensure_ascii=False).encode("utf-8"), **kwargs)
