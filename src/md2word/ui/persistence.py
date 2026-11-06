"""Qt adapter for local state; large draft serialization runs in one worker."""
from datetime import datetime
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, QRect
from PySide6.QtWidgets import (QApplication, QCheckBox, QDialog, QDialogButtonBox,
    QFileDialog, QHBoxLayout, QLabel, QMenu, QMessageBox, QToolButton, QVBoxLayout, QSizePolicy)

from ..domain.models import DocumentError, Issue, Severity
from ..storage.diagnostics import Diagnostics
from ..storage.lane import DraftLane
from ..storage.models import Draft, Settings
from ..storage.repositories import DraftRepository, SettingsRepository


class PersistenceController(QObject):
    SAVE_DELAY_MS = 2000

    def __init__(self, window, paths, layout):
        super().__init__(window)
        self.window, self.paths = window, paths
        self.repository = DraftRepository(paths.drafts)
        self.preferences = SettingsRepository(paths.settings)
        self.logs = Diagnostics(paths.logs)
        self.lane = DraftLane(self.repository)
        loaded_settings = self.preferences.load()
        self.settings = loaded_settings.value or Settings()
        self.window.issues.auto_expand.setChecked(self.settings.auto_expand_errors)
        self.recovery = self.repository.load()
        self.recovery_pending = self.recovery.value is not None or self.repository.blocked
        self.close_requested = False
        self.close_done = False
        self.close_ticket = None
        self.clear_ticket = None
        self.clear_key = None
        self.cleared_key = None
        self.saved_key = None
        self.requested_keys = {}
        self.last_issue = None
        self.restoring = False
        row = QHBoxLayout()
        self.auto_save = QCheckBox("自动保存草稿")
        self.auto_save.setChecked(self.settings.auto_save)
        row.addWidget(self.auto_save)
        self.status = QLabel("尚无草稿")
        self.status.setObjectName("muted")
        self.status.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        row.addWidget(self.status, 1)
        self.menu_button = QToolButton()
        self.menu_button.setText("草稿与设置")
        self.menu_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.menu_button)
        for text, callback in [("立即保存 / 重试保存草稿", self.save_now),
                               ("恢复已有草稿…", self.offer_recovery),
                               ("清空已保存草稿…", self.confirm_clear),
                               ("生成本地诊断包…", self.diagnostic_dialog)]:
            menu.addAction(text, callback)
        self.menu_button.setMenu(menu)
        row.addWidget(self.menu_button)
        layout.addLayout(row)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(self.SAVE_DELAY_MS)
        self.timer.timeout.connect(self._auto_save)
        self.auto_save.toggled.connect(self.toggle_auto_save)
        self.window.sessionChanged.connect(self.changed)
        self.window.motion.toggled.connect(self.save_settings)
        self.window.manual.toggled.connect(self.save_settings)
        self.window.issues.auto_expand.toggled.connect(self.save_settings)
        for issue in loaded_settings.issues + self.recovery.issues:
            self.show_issue(issue)
        for issue in window.styles.repository.list().issues:
            self.show_issue(issue)
        if self.logs.failed:
            self.show_issue(Issue("LOG_UNAVAILABLE", Severity.WARNING, "storage", "本地诊断日志不可写。", "检查用户目录权限。"))
        self.logs.record("startup", "APPLICATION_STARTED")
        try:
            self.repository.cleanup_temps()
        except DocumentError as exc:
            self.show_issue(exc.issue)
        QTimer.singleShot(0, self.apply_settings)
        if self.recovery_pending:
            self.status.setText("已有草稿待处理 · 自动保存暂停")
            QTimer.singleShot(0, self.offer_recovery)
        elif not self.auto_save.isChecked():
            self.status.setText("自动保存已关闭 · 已有草稿保留")

    def key(self):
        s = self.window.session
        return (s.session_id, s.content_revision, s.style_revision, s.dirty, self.window.styles.style.resolved_hash,s.local_hash)

    def document_key(self):
        # Export updates dirty without editing the document. Only actual content,
        # mode or formatting changes release a successful clear's suppression.
        s = self.window.session
        return (s.session_id, s.content_revision, s.style_revision,
                self.window.styles.style.resolved_hash, s.local_hash)

    def show_issue(self, issue):
        issue = replace(issue, severity=Severity.WARNING)
        w = self.window
        w.storage_issues = tuple(i for i in w.storage_issues if i.code != issue.code) + (issue,)
        w.storage_issues = w.storage_issues[-12:]
        w.refresh_issues()
        self.logs.record("storage", issue.code)

    def changed(self):
        if self.restoring or self.close_requested:
            return
        self.timer.stop()
        if self.cleared_key == self.document_key():
            return
        self.cleared_key = None
        if self.recovery_pending:
            self.status.setText("已有草稿待处理 · 当前编辑尚未保存")
        elif self.auto_save.isChecked():
            self.status.setText("草稿待保存…")
            self.timer.start()
        else:
            self.status.setText("自动保存已关闭 · 当前修改未自动保存")

    def _auto_save(self):
        if self.auto_save.isChecked():
            self.save_now()

    def save_now(self, *_):
        self.timer.stop()
        if self.close_requested or self.recovery_pending or self.clear_ticket is not None:
            if self.recovery_pending:
                self.status.setText("请先恢复或明确清空已有草稿")
            return
        self.cleared_key = None
        try:
            draft = Draft.capture(self.window.session, self.window.styles.style, self.window.document_title.text())
            ticket = self.lane.request(draft)
            self.requested_keys[ticket] = self.key()
            # At most active + newest pending; metadata follows the same bound.
            self.requested_keys = {k:v for k, v in self.requested_keys.items() if k >= ticket - 1}
            self.status.setText("正在保存草稿…")
        except (ValueError, DocumentError):
            self.status.setText("草稿未保存 · 请保留原文并重试")
            self.show_issue(Issue("DRAFT_INVALID", Severity.WARNING, "storage", "草稿超出允许范围或无法编码，内存内容仍保留。", "保留原文后重试。"))

    def toggle_auto_save(self, enabled):
        self.timer.stop()
        if not enabled:
            self.lane.pause()
            self.status.setText("自动保存已关闭 · 草稿已清空" if self.cleared_key == self.document_key()
                                else "自动保存已关闭 · 已有草稿保留")
        elif not self.recovery_pending:
            if self.cleared_key == self.document_key():
                self.status.setText("已清空草稿 · 继续编辑后自动保存")
            else:
                self.changed()
        self.save_settings()

    def offer_recovery(self, *_):
        if self.close_requested or self.clear_ticket is not None:
            return
        if not self.lane.idle:
            self.status.setText("正在保存草稿，请完成后再恢复")
            return
        if not self.recovery_pending:
            self.recovery = self.repository.load()
            self.recovery_pending = self.recovery.value is not None or self.repository.blocked
            for issue in self.recovery.issues:
                self.show_issue(issue)
        if self.recovery.value is None:
            if self.repository.blocked:
                self.status.setText("草稿无法恢复，原文件已保留；可主动清空后重新保存")
            else:
                self.status.setText("暂无可恢复的草稿")
            return
        self.timer.stop()
        draft = self.recovery.value
        timestamp = datetime.fromisoformat(draft.saved_at).astimezone().strftime("%Y-%m-%d %H:%M:%S")
        box = QMessageBox(self.window)
        box.setWindowTitle("恢复本地草稿")
        box.setText(f"发现{self.recovery.source}，保存于 {timestamp}。\n可恢复原文、当前编辑、输入模式和完整样式。\n暂不处理时保留文件并暂停自动保存。")
        restore = box.addButton("恢复草稿", QMessageBox.ButtonRole.AcceptRole)
        discard = box.addButton("放弃并清空草稿", QMessageBox.ButtonRole.DestructiveRole)
        later = box.addButton("暂不处理", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(restore)
        box.exec()
        if box.clickedButton() == restore:
            self.restore_draft()
        elif box.clickedButton() == discard:
            self.clear_saved()

    def restore_draft(self):
        if self.recovery.value is None:
            return
        if self.window.session.dirty and not self.window.may_replace():
            return
        draft = self.recovery.value
        self.restoring = True
        self.window.loading = True
        self.window.styles.load(draft.style())
        self.window.loading = False
        self.window.replace_session(draft.session(), draft.title)
        self.restoring = False
        self.recovery_pending = False
        self.saved_key = None
        if self.recovery.source == "上一次有效快照":
            self.show_issue(Issue("DRAFT_READ_FAILED", Severity.WARNING, "storage",
                "已从上一次有效快照恢复；损坏文件会保留供检查。", "确认恢复后的内容。"))
        self.recovery = type(self.recovery)()
        self.status.setText("草稿已恢复 · 源 Markdown 文件未写回")
        self.logs.record("storage", "DRAFT_RESTORED")
        # The user's explicit recovery choice becomes the newest queued snapshot.
        # It also repairs a damaged primary only after recovery was accepted.
        if self.auto_save.isChecked():
            self.save_now()
        else:
            self.saved_key = self.key()
            self.status.setText("草稿已恢复 · 自动保存仍关闭，已有文件保留")

    def confirm_clear(self, *_):
        if QMessageBox.question(self.window, "清空已保存草稿", "清空当前草稿及恢复备份？\n当前编辑内容仍保留；继续编辑后才会重新自动保存。", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self.clear_saved()

    def clear_saved(self):
        if self.close_requested or self.clear_ticket is not None:
            return
        self.timer.stop()
        self.cleared_key = None
        self.clear_key = self.document_key()
        self.clear_ticket = self.lane.request(clear=True)
        self.status.setText("正在清空草稿…")

    def poll(self):
        if self.logs.failed and not any(i.code == "LOG_UNAVAILABLE" for i in self.window.storage_issues):
            self.show_issue(Issue("LOG_UNAVAILABLE", Severity.WARNING, "storage", "本地日志未能写入。", "检查用户目录权限和磁盘空间。"))
        result = self.lane.poll()
        if result is None:
            return
        self.requested_keys.pop(result.ticket, None)
        if result.issue is not None:
            self.show_issue(result.issue)
        if result.ticket != self.lane.latest_ticket:
            return
        self.last_issue = result.issue
        if result.operation == "clear":
            self.clear_ticket = None
            cleared_document, self.clear_key = self.clear_key, None
            self.timer.stop()
            if result.issue is None:
                self.cleared_key = cleared_document
                self.recovery_pending = False
                self.recovery = type(self.recovery)()
                self.saved_key = None
                self.status.setText("已清空草稿 · 当前内容仍保留")
                if self.cleared_key != self.document_key():
                    self.changed()
            else:
                self.cleared_key = None
                self.status.setText("草稿未完全清空 · 请重试")
        elif result.issue is None:
            s = result.value
            self.saved_key = (s.session_id, s.content_revision, s.style_revision, s.dirty, s.style().resolved_hash,s.session().local_hash)
            if self.auto_save.isChecked():
                self.status.setText("草稿已保存" if self.saved_key == self.key() else "已有快照已保存 · 最新修改待保存")
                if not self.window.styles.valid:
                    self.status.setText("正文与有效样式已保存 · 无效排版参数未保存")
            else:
                self.status.setText("自动保存已关闭 · 已有草稿保留")
            self.logs.record("storage", "DRAFT_SAVED", result.elapsed_ms)
        else:
            self.status.setText("草稿未保存 · 可在菜单重试")
        if self.close_ticket == result.ticket:
            if result.issue is None:
                self.close_done = True
            else:
                choice = self.close_failure_choice()
                if choice == "retry":
                    self.close_ticket = self.lane.request(Draft.capture(self.window.session, self.window.styles.style, self.window.document_title.text()))
                elif choice == "discard":
                    self.close_done = True
                else:
                    self.close_requested = False
                    self.window.centralWidget().setEnabled(True)

    def close_choice(self):
        session = self.window.session
        if (not session.current_text and not session.original_text and not session.dirty
                and not self.repository.path.exists() and not self.repository.backup.exists()):
            return "discard"
        if self.auto_save.isChecked() and not self.recovery_pending:
            return "save"
        if not self.window.session.dirty:
            return "discard"
        box = QMessageBox(self.window)
        box.setWindowTitle("关闭前保留内容")
        box.setText("自动保存已关闭或已有草稿尚未处理。\n已有草稿保留；直接退出不会保存本次修改。")
        save = None
        if not self.recovery_pending:
            save = box.addButton("保存草稿并退出", QMessageBox.ButtonRole.AcceptRole)
        discard = box.addButton("退出，不保存本次修改", QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton("取消关闭", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        return "save" if save and box.clickedButton() == save else "discard" if box.clickedButton() == discard else "cancel"

    def close_failure_choice(self):
        box = QMessageBox(self.window)
        box.setWindowTitle("草稿保存失败")
        box.setText("最新草稿未保存，当前内容仍在内存中。\n可以重试、取消关闭并保留内容，或明确放弃本次未保存修改。")
        retry = box.addButton("重试保存", QMessageBox.ButtonRole.AcceptRole)
        discard = box.addButton("放弃未保存修改并退出", QMessageBox.ButtonRole.DestructiveRole)
        cancel = box.addButton("取消关闭，保留内容", QMessageBox.ButtonRole.RejectRole)
        box.setDefaultButton(cancel)
        box.exec()
        return "retry" if box.clickedButton() == retry else "discard" if box.clickedButton() == discard else "cancel"

    def prepare_close(self):
        if self.close_requested:
            return self.close_done and self.lane.idle
        if self.clear_ticket is not None:
            self.status.setText("正在完成草稿清空，请稍候关闭")
            return False
        choice = "discard" if self.cleared_key == self.document_key() else self.close_choice()
        if choice == "cancel":
            return False
        if choice == "save" and not self.window.styles.valid:
            if QMessageBox.question(self.window, "排版参数尚未有效", "部分排版输入无效。草稿将保存正文和上次有效样式，不包含无效参数。继续关闭？", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) != QMessageBox.StandardButton.Yes:
                return False
        self.close_requested = True
        self.window.centralWidget().setEnabled(False)
        self.timer.stop()
        if choice == "save" and self.saved_key != self.key():
            try:
                self.close_ticket = self.lane.request(Draft.capture(self.window.session, self.window.styles.style, self.window.document_title.text()))
            except (ValueError, DocumentError):
                self.close_requested = False
                self.window.centralWidget().setEnabled(True)
                self.status.setText("草稿无法保存，已取消关闭；请保留原文")
                return False
        else:
            self.lane.pause()
            self.close_done = True
        return self.close_done and self.lane.idle

    def current_settings(self):
        w = self.window
        rect = w.normalGeometry() if w.isMaximized() else w.geometry()
        columns = w.columns.sizes()
        vertical = w.vertical.sizes()
        return Settings(x=rect.x(), y=rect.y(), width=max(620, rect.width()), height=max(560, rect.height()),
                        maximized=w.isMaximized(), columns=columns if any(columns) else self.settings.columns,
                        vertical=vertical if any(vertical) else self.settings.vertical,
                        last_directory=str(w.last_directory or ""), auto_save=self.auto_save.isChecked(),
                        motion=w.motion.isChecked(), manual_preview=w.manual.isChecked(),
                        auto_expand_errors=w.issues.auto_expand.isChecked())

    def save_settings(self, *_):
        if self.restoring:
            return
        try:
            self.settings = self.current_settings()
            self.preferences.save(self.settings)
        except (DocumentError, ValueError) as exc:
            issue = exc.issue if isinstance(exc, DocumentError) else Issue("SETTINGS_INVALID", Severity.WARNING, "storage", "界面偏好未保存。", "恢复默认布局后重试。")
            self.show_issue(issue)

    def apply_settings(self):
        self.restoring = True
        w, s = self.window, self.settings
        w.issues.auto_expand.setChecked(s.auto_expand_errors)
        screens = QApplication.screens()
        rect = QRect(s.x, s.y, s.width, s.height)
        screen = next((item for item in screens if item.availableGeometry().contains(rect.center())), QApplication.primaryScreen())
        available = screen.availableGeometry()
        width, height = min(s.width, available.width()), min(s.height, available.height())
        x = max(available.left(), min(s.x, available.right() - width + 1))
        y = max(available.top(), min(s.y, available.bottom() - height + 1))
        w.setGeometry(x, y, width, height)
        # setGeometry addresses the client rectangle. Its native title bar/frame
        # may extend beyond the available screen, so correct using frameGeometry.
        frame = w.frameGeometry()
        frame_width = max(0,frame.width()-w.width())
        frame_height = max(0,frame.height()-w.height())
        if frame.width() > available.width() or frame.height() > available.height():
            w.resize(max(620,min(w.width(),available.width()-frame_width)),
                     max(560,min(w.height(),available.height()-frame_height)))
            frame = w.frameGeometry()
        dx = max(0, available.left() - frame.left())
        dy = max(0, available.top() - frame.top())
        if dx or dy:
            w.move(w.pos().x() + dx, w.pos().y() + dy)
        w.columns.setSizes(s.columns)
        w.vertical.setSizes(s.vertical)
        if s.last_directory and Path(s.last_directory).is_dir():
            w.last_directory = Path(s.last_directory)
        w.motion.setChecked(s.motion)
        w.manual.setChecked(s.manual_preview)
        if s.maximized:
            w.showMaximized()
        self.restoring = False

    def diagnostic_dialog(self, *_):
        dialog = QDialog(self.window)
        dialog.setWindowTitle("生成本地诊断包")
        layout = QVBoxLayout(dialog)
        label = QLabel("默认内容清单：应用 / Python / Qt 版本、系统类型、脱敏阶段日志。\n不包含设置目录、输出路径、草稿文件或公式正文。\n诊断包只保存到你选择的位置。")
        label.setWordWrap(True)
        layout.addWidget(label)
        include = QCheckBox("另外包含原始文本和当前正文（可能含私人内容）")
        layout.addWidget(include)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        target, _ = QFileDialog.getSaveFileName(self.window, "保存诊断包 · 使用新文件名", "md2word-diagnostics.zip", "ZIP (*.zip)")
        if target:
            try:
                self.logs.bundle(Path(target), session=self.window.session if include.isChecked() else None)
                self.status.setText("诊断包已保存到所选位置")
            except DocumentError as exc:
                self.show_issue(exc.issue)

    def dispose(self):
        self.save_settings()
        self.logs.record("shutdown", "APPLICATION_CLOSED")
        self.lane.close()
        self.logs.close()
