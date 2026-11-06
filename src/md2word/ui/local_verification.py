"""Opt-in frozen UI check with a real preview drag and isolated draft recovery."""
import json
import ctypes
from pathlib import Path

from PySide6.QtCore import QTimer,QPoint,QEventLoop,Qt
from PySide6.QtGui import QCursor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QMessageBox
from ..domain.models import DocumentSession,Issue,Severity
from ..exporting.models import TaskState
from ..exporting.output import OutputIntent
from ..exporting.scheduler import Scheduler
from ..exporting.service import ExportService
from ..storage.files import UserPaths
from ..storage.repositories import DraftRepository
from ..templates.service import PersonalStyleRepository
from ..conversion.ooxml import Package,NS,W,q
from .main_window import MainWindow
from .verification import wait_for,javascript


def verify_local_gui(directory):
    output = Path(directory).resolve()
    output.mkdir(parents=True,exist_ok=True)
    paths = UserPaths(output / "isolated-user-data")
    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("md2word-local-qa")
    report = {"passed":False,"production_state_used":False,"fixed_sample":True}
    window = None
    timer = QTimer()
    try:
        def new_window():
            result = MainWindow(user_paths=paths,style_repository=PersonalStyleRepository(paths.styles),
                scheduler=Scheduler(ExportService(work_root=paths.tasks)))
            result.may_replace = lambda:True
            # Keep this opt-in, isolated test window above other desktop windows.
            result.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint,True)
            result.show()
            result.raise_()
            result.activateWindow()
            return result
        window = new_window()
        wait_for(lambda:window.preview.loaded)
        window.motion.setChecked(False)
        assert not hasattr(window,"window_controls")
        native = ctypes.WinDLL("user32",use_last_error=True)
        native.GetWindowLongW.argtypes = [ctypes.c_void_p,ctypes.c_int]
        native.GetWindowLongW.restype = ctypes.c_long
        native.PostMessageW.argtypes = [ctypes.c_void_p,ctypes.c_uint,ctypes.c_size_t,ctypes.c_ssize_t]
        native.PostMessageW.restype = ctypes.c_int
        native.GetForegroundWindow.restype = ctypes.c_void_p
        native.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p,ctypes.c_void_p]
        native.GetWindowThreadProcessId.restype = ctypes.c_ulong
        native.AttachThreadInput.argtypes = [ctypes.c_ulong,ctypes.c_ulong,ctypes.c_int]
        native.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        native.BringWindowToTop.argtypes = [ctypes.c_void_p]
        kernel = ctypes.WinDLL("kernel32",use_last_error=True)
        kernel.GetCurrentThreadId.restype = ctypes.c_ulong
        def focus_window():
            foreground = native.GetForegroundWindow()
            owner = native.GetWindowThreadProcessId(foreground,None) if foreground else 0
            current = kernel.GetCurrentThreadId()
            attached = owner != current and owner and native.AttachThreadInput(current,owner,True)
            try:
                native.BringWindowToTop(int(window.winId()))
                native.SetForegroundWindow(int(window.winId()))
            finally:
                if attached:
                    native.AttachThreadInput(current,owner,False)
            QTest.qWait(150)
            assert native.GetForegroundWindow() == int(window.winId())
        def command(value):
            assert native.PostMessageW(int(window.winId()),0x0112,value,0)
        def grab_frame(name):
            focus_window()
            rect = window.frameGeometry()
            window.screen().grabWindow(0,rect.x(),rect.y(),rect.width(),rect.height()).save(str(output / name))
        flags = native.GetWindowLongW(int(window.winId()),-16)
        assert flags & 0x00C00000 == 0x00C00000  # Native caption, minimize/maximize and system menu.
        assert flags & 0x00020000 and flags & 0x00010000 and flags & 0x00080000
        report["single_native_caption"] = True
        assert not window.issues.toggle.isChecked() and not window.issues.tree.isVisible()
        QTest.qWait(250)  # Let the first native-window layout and stylesheet settle.
        default_height = window.horizontal.height()
        warning = Issue("QA_WARNING",Severity.WARNING,"test","固定测试警告","测试建议")
        error = Issue("QA_ERROR",Severity.BLOCKING,"test","固定测试错误","测试建议")
        window.issues.show_issues((warning,))
        assert not window.issues.tree.isVisible()
        window.issues.show_issues((error,))
        QTest.qWait(100)
        report["issue_layout"] = {"initial": default_height, "expanded": window.horizontal.height(),
                                  "visible": window.issues.tree.isVisible(),
                                  "foreground": native.GetForegroundWindow() == int(window.winId())}
        assert window.issues.tree.isVisible() and window.horizontal.height() < default_height - 50
        grab_frame("error-details-expanded.png")
        window.issues.toggle.click()
        window.issues.show_issues((error,warning))
        assert not window.issues.tree.isVisible()
        window.issues.auto_expand.setChecked(False)
        window.issues.show_issues(())
        window.issues.show_issues((error,))
        assert not window.issues.tree.isVisible()
        window.issues.toggle.click()
        assert window.issues.tree.isVisible()
        window.issues.show_issues(())
        report["issue_details_default_closed_and_optional"] = True
        wait_for(lambda:bool(javascript(window,"window.previewState.ready")) or javascript(window,"document.getElementById('paper').textContent.includes('让内容，轻松成文。')"))
        report["generic_empty_copy"] = bool(javascript(window,"document.getElementById('paper').textContent.includes('让内容，轻松成文。')"))
        source = "# 通用文档示例\n\n中文示例 alpha 与默认文字。\n\n公式保持可编辑：$x^2+1$\n"
        window.replace_session(DocumentSession.create(source),"局部排版示例")
        window.request_preview()
        wait_for(lambda:window.latest_preview is not None and window.latest_preview.key == window.current_key())
        wait_for(lambda:bool(javascript(window,"window.previewState.ready")))
        window.style_tabs.setCurrentIndex(1)
        window.activateWindow()
        window.raise_()
        QTest.qWait(250)
        focus_window()
        report["phase"] = "mouse selection"
        rect = json.loads(javascript(window,"""(()=>{const leaf=Array.from(document.querySelectorAll('span[data-lf-start]')).find(e=>e.textContent==='alpha');
            const r=leaf.getBoundingClientRect();return JSON.stringify({x:r.x,y:r.y,width:r.width,height:r.height,zoom:parseFloat(document.getElementById('paper').style.zoom)||1});})()"""))
        view = window.preview.view
        target = view.focusProxy() or view
        scale = rect["zoom"]
        start = target.mapFrom(view,QPoint(round(rect["x"]*scale-1),round((rect["y"]+rect["height"]/2)*scale)))
        end = target.mapFrom(view,QPoint(round((rect["x"]+rect["width"])*scale+1),round((rect["y"]+rect["height"]/2)*scale)))
        report["drag_rect"] = rect
        report["view_size"] = [view.width(),view.height()]
        javascript(window,"document.addEventListener('mousemove',e=>window.qaPointer={x:e.clientX,y:e.clientY,leaf:e.target.closest('span[data-lf-start]')?.textContent?.slice(0,60)||''});true")
        user32 = ctypes.WinDLL("user32",use_last_error=True)
        mouse = user32.mouse_event
        mouse.argtypes = [ctypes.c_ulong,ctypes.c_ulong,ctypes.c_ulong,ctypes.c_ulong,ctypes.c_size_t]
        mouse.restype = None
        QCursor.setPos(target.mapToGlobal(start))
        QTest.qWait(100)
        mouse(0x0002,0,0,0,0)
        mouse(0x0004,0,0,0,0)
        QTest.qWait(100)
        mouse(0x0002,0,0,0,0)
        for step in range(1,9):
            point = QPoint(round(start.x()+(end.x()-start.x())*step/8),round(start.y()+(end.y()-start.y())*step/8))
            QCursor.setPos(target.mapToGlobal(point))
            QTest.qWait(40)
        mouse(0x0004,0,0,0,0)
        QTest.qWait(100)
        report["pointer"] = javascript(window,"window.qaPointer||null")
        report["selected_text_after_drag"] = javascript(window,"getSelection().toString()")
        window.grab().save(str(output / "mouse-selection.png"))
        wait_for(lambda:bool(javascript(window,"window.getLocalSelection() && window.getLocalSelection().blocks.length")))
        report["selected_text"] = javascript(window,"getSelection().toString()")
        assert report["selected_text"].strip() == "alpha"
        report["real_mouse_drag"] = True
        panel = window.local_panel
        panel.font_zh.setCurrentText("黑体")
        panel.font_latin.setCurrentText("Arial")
        panel.size.setCurrentIndex(panel.size.findData(18.))
        panel.letter_spacing.setValue(.8)
        panel.line_mode.setCurrentIndex(1)
        panel.line_value.setValue(2.)
        panel.before.setValue(6.)
        QTest.mouseClick(panel.apply_button,Qt.MouseButton.LeftButton)
        report["phase"] = "local preview"
        wait_for(lambda:bool(window.session.local_formats) and window.latest_preview.key == window.current_key())
        wait_for(lambda:bool(javascript(window,"window.previewState.ready")))
        preview_size = javascript(window,"""(()=>{const leaf=Array.from(document.querySelectorAll('span[data-lf-start]')).find(e=>e.textContent.includes('alpha'));return getComputedStyle(leaf).fontSize;})()""")
        assert float(preview_size.removesuffix("px")) == 24.
        report["local_preview_size_px"] = 24
        assert window.session.current_text == source
        report["markdown_source_unchanged"] = True
        target_docx = output / "局部排版验证.docx"
        window.start_export(OutputIntent.capture(target_docx))
        wait_for(lambda:window.last_result is not None)
        assert window.last_result.state == TaskState.SUCCEEDED
        package = Package.read(target_docx)
        runs = package.document.findall(".//w:body//w:r",NS)
        chosen = [r for r in runs if "alpha" in "".join(t.text or "" for t in r.findall("w:t",NS))]
        default = [r for r in runs if "默认文字" in "".join(t.text or "" for t in r.findall("w:t",NS))]
        assert chosen and default
        assert all(r.find("w:rPr/w:sz",NS).get(q(W,"val")) == "36" for r in chosen)
        assert all(r.find("w:rPr/w:spacing",NS).get(q(W,"val")) == "16" for r in chosen)
        assert all(r.find("w:rPr/w:sz",NS).get(q(W,"val")) == "24" for r in default)
        report["word_selected_and_default_runs_verified"] = True
        QTest.qWait(100)
        grab_frame("desktop-local-format.png")
        command(0xF030)  # SC_MAXIMIZE
        wait_for(lambda:window.isMaximized())
        command(0xF120)  # SC_RESTORE
        wait_for(lambda:not window.isMaximized())
        command(0xF020)  # SC_MINIMIZE
        wait_for(lambda:window.isMinimized())
        command(0xF120)
        wait_for(lambda:not window.isMinimized())
        report["native_window_controls_work"] = True
        before_close = window.session.local_formats
        command(0xF060)  # SC_CLOSE through the native system menu.
        wait_for(lambda:window.disposed)
        stored = DraftRepository(paths.drafts).load().value
        assert stored.schema_version == 2 and stored.local_formats == before_close
        report["draft_v2_preserves_local_format"] = True
        report["phase"] = "recover local format"
        def recover_prompt():
            modal = QApplication.activeModalWidget()
            if isinstance(modal,QMessageBox) and modal.windowTitle() == "恢复本地草稿":
                next(b for b in modal.buttons() if b.text() == "恢复草稿").click()
        timer.timeout.connect(recover_prompt)
        timer.start(40)
        window = new_window()
        wait_for(lambda:window.session.current_text == source)
        timer.stop()
        window.motion.setChecked(False)
        assert not window.issues.auto_expand.isChecked() and not window.issues.tree.isVisible()
        report["issue_auto_expand_preference_recovered"] = True
        window.request_preview()
        wait_for(lambda:window.latest_preview is not None and window.latest_preview.key == window.current_key())
        wait_for(lambda:bool(javascript(window,"window.previewState.ready")))
        assert window.session.local_formats == before_close
        report["recovered_local_format"] = True
        window.resize(720,680)
        window.tabs.setCurrentWidget(window.preview)
        settle = QEventLoop()
        QTimer.singleShot(350,settle.quit)
        settle.exec()
        grab_frame("small-window.png")
        assert window.compact and window.width() <= 740 and not window.issues.tree.isVisible()
        report["small_window_native_frame"] = True
        report["phase"] = "complete"
        report["passed"] = all(report.get(k) is True for k in ("generic_empty_copy","real_mouse_drag","markdown_source_unchanged",
            "word_selected_and_default_runs_verified","single_native_caption","native_window_controls_work",
            "issue_details_default_closed_and_optional","issue_auto_expand_preference_recovered",
            "draft_v2_preserves_local_format","recovered_local_format","small_window_native_frame"))
    except Exception as exc:
        import traceback
        report["verification_traceback"] = traceback.format_exc()
        report["error"] = type(exc).__name__
    finally:
        timer.stop()
        if window is not None and not window.disposed:
            window.persistence.close_choice = lambda:"discard"
            window.persistence.close_failure_choice = lambda:"discard"
            window.close()
            wait_for(lambda:window.disposed)
        app.processEvents()
        (output / "verification.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),"utf-8")
    return 0 if report["passed"] else 1
