"""A damaged personal style must not prevent startup or usable styles/export."""
import pytest
from md2word.domain.models import DocumentError
from md2word.exporting.models import TaskState
from md2word.exporting.output import OutputIntent
from md2word.exporting.scheduler import Scheduler
from md2word.exporting.service import ExportService
from md2word.storage.files import UserPaths
from md2word.templates.service import PersonalStyleRepository, resolve_style
from md2word.ui.main_window import MainWindow
from test_stage6 import app, spin


DEEP_STYLE = b'{"schema_version":1,"nested":' + b"[" * 30000 + b"0" + b"]" * 30000 + b"}"
BAD_ID = "user." + "a" * 32


@pytest.mark.parametrize("payload", [DEEP_STYLE, b"{broken", b"x" * (128 * 1024 + 1), b"\xff"] ,
                         ids=["deep-json", "malformed-json", "oversized", "invalid-utf8"])
def test_invalid_personal_style_is_reported_skipped_and_never_overwritten(tmp_path, payload):
    repo = PersonalStyleRepository(tmp_path / "styles")
    valid = repo.save(resolve_style().config, name="valid control style")
    path = repo.path(BAD_ID)
    path.write_bytes(payload)
    with pytest.raises(DocumentError) as error:
        repo.load(BAD_ID)
    assert error.value.code == "STYLE_READ_FAILED"
    listing = repo.list()
    assert {s.id for s in listing.styles} == {"lecture", valid.id}
    assert [i.code for i in listing.issues] == ["STYLE_READ_FAILED"]
    assert repo.load(valid.id) == valid
    for operation in (lambda:repo.rename(BAD_ID, "replace bad style"),
                      lambda:repo.save(valid, name="replace bad style", style_id=BAD_ID)):
        with pytest.raises(DocumentError) as error:
            operation()
        assert error.value.code == "STYLE_READ_FAILED"
        assert path.read_bytes() == payload


@pytest.mark.parametrize("has_valid_personal", [False, True], ids=["builtin-only", "valid-personal"])
def test_real_window_starts_with_deep_style_and_can_select_preview_export(app, tmp_path, has_valid_personal):
    paths = UserPaths(tmp_path / "isolated-state")
    repo = PersonalStyleRepository(paths.styles)
    valid = repo.save(resolve_style(overrides={"body":{"size_pt":14}}).config,
                      name="valid personal style") if has_valid_personal else None
    bad = repo.path(BAD_ID)
    bad.write_bytes(DEEP_STYLE)
    w = MainWindow(user_paths=paths, style_repository=repo,
                   scheduler=Scheduler(ExportService(work_root=paths.tasks)))
    w.show()
    try:
        spin(lambda:w.preview.loaded)
        assert any(i.code == "STYLE_READ_FAILED" for i in w.storage_issues)
        assert w.styles.templates.count() == (2 if valid else 1)
        assert w.styles.templates.findData(BAD_ID) == -1
        if valid:
            w.styles.templates.setCurrentIndex(w.styles.templates.findData(valid.id))
        assert w.styles.valid and w.styles.style.config.body.size_pt == (14 if valid else 12)
        w.editor.setPlainText("# Fixed test\n\nUsable styles survive a damaged neighbor.")
        w.request_preview()
        spin(lambda:w.latest_preview is not None and w.latest_preview.key == w.current_key())
        assert w.latest_preview.failure is None
        w.start_export(OutputIntent.capture(tmp_path / "working-style.docx"))
        spin(lambda:w.last_result is not None, timeout=30000)
        assert w.last_result.state == TaskState.SUCCEEDED
        assert bad.read_bytes() == DEEP_STYLE
    finally:
        w.persistence.close_choice = lambda:"discard"
        w.persistence.close_failure_choice = lambda:"discard"
        w.close()
        spin(lambda:w.disposed)
    assert bad.read_bytes() == DEEP_STYLE
