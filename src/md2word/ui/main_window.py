"""Stage 06 composition root and UI state. Workers never access these widgets."""
from dataclasses import replace
from pathlib import Path
import os
import time

from PySide6.QtCore import Qt, QTimer, QUrl, QPropertyAnimation, QEasingCurve, Signal
from PySide6.QtGui import QAction, QKeySequence, QTextCursor, QFont
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout,
    QHBoxLayout, QLabel, QPushButton, QToolButton, QComboBox, QSplitter,
    QTabWidget, QTreeWidget, QTreeWidgetItem, QHeaderView, QFileDialog,
    QMessageBox, QDialog, QDialogButtonBox, QPlainTextEdit, QCheckBox,
    QProgressBar, QGraphicsOpacityEffect, QMenu)
from PySide6.QtGui import QDesktopServices

from ..domain.models import DocumentSession, DocumentError, InputMode, Severity, Issue
from ..domain.repairs import suggest_outer_fence, apply_repair
from ..domain.source_mapping import qt_selection
from ..exporting.models import ExportSnapshot, TaskState
from ..exporting.output import OutputIntent, candidate_from_document
from ..exporting.scheduler import Scheduler
from ..input import read_input
from ..live_preview import PreviewKey, PreviewRequest, PreviewLane
from ..resources import ResourceLocator
from .editor import Editor
from .preview_panel import PreviewPanel
from .style_panel import StylePanel
from .theme import STYLE

STATES = {"queued":"等待开始", "parsing":"解析内容", "checking":"检查公式", "writing":"生成 Word",
          "postprocessing":"应用排版", "validating":"核对内容", "committing":"正在提交文件", "cancelling":"正在取消",
          "succeeded":"导出完成", "failed":"导出失败", "cancelled":"已取消"}


class IssuePanel(QWidget):
    detailsVisibilityChanged = Signal(bool)

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.toggle = QToolButton()
        self.toggle.setText("检查结果  ·  尚未检查")
        self.toggle.setCheckable(True)
        self.toggle.setChecked(False)
        self.toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.toggle.setToolTip("展开 / 收起问题与处理建议")
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.toggle)
        row.addStretch()
        self.auto_expand = QCheckBox("出错时自动展开")
        self.auto_expand.setChecked(True)
        self.auto_expand.setToolTip("仅在出现新的阻断错误时展开；警告和提醒可点击检查结果查看。")
        row.addWidget(self.auto_expand)
        layout.addLayout(row)
        self._seen_errors = set()
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["级别 / 位置", "问题与原文片段", "处理建议"])
        self.tree.setAlternatingRowColors(True)
        self.tree.setRootIsDecorated(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.tree.setMinimumHeight(85)
        layout.addWidget(self.tree)
        self.toggle.toggled.connect(self.set_details_visible)
        self.set_details_visible(False)

    def showEvent(self, event):
        super().showEvent(event)
        # Inherited stylesheet metrics are final only after Qt polishes widgets.
        self.layout().invalidate()
        self.set_details_visible(self.toggle.isChecked())

    def set_details_visible(self, visible):
        self.tree.setVisible(visible)
        self.toggle.setArrowType(Qt.ArrowType.DownArrow if visible else Qt.ArrowType.RightArrow)
        self.setMaximumHeight(16777215 if visible else self.layout().itemAt(0).sizeHint().height())
        self.detailsVisibilityChanged.emit(visible)

    def show_issues(self, issues, checked=True):
        self.tree.clear()
        unique = tuple(dict.fromkeys(issues))
        rank = {Severity.BLOCKING:0, Severity.WARNING:1, Severity.INFO:2}
        labels = {Severity.BLOCKING:"阻止导出", Severity.WARNING:"警告", Severity.INFO:"提醒"}
        counts = {severity:sum(i.severity == severity for i in unique) for severity in rank}
        errors = {i for i in unique if i.severity == Severity.BLOCKING}
        new_errors = errors - self._seen_errors
        self._seen_errors = errors if checked else self._seen_errors | errors
        for issue in sorted(unique, key=lambda i:rank[i.severity]):
            location = issue.location
            where = ""
            message = issue.message
            if location:
                where = f" · {location.section or '正文'} · 元素 {location.ordinal}"
                message += "\n" + location.snippet
                if location.span is None:
                    where += "（上下文）"
            item = QTreeWidgetItem([labels[issue.severity] + where, message, issue.action])
            item.setData(0, Qt.ItemDataRole.UserRole, issue)
            item.setToolTip(1, issue.code + "\n" + message)
            self.tree.addTopLevelItem(item)
        if unique:
            self.toggle.setText(f"检查结果  ·  {counts[Severity.BLOCKING]} 项阻断  /  {counts[Severity.WARNING]} 项警告  /  {counts[Severity.INFO]} 项提醒")
        else:
            self.toggle.setText("检查结果  ·  未发现结构问题；公式在导出时检查" if checked else "检查结果  ·  等待当前版本预览")
        if new_errors and self.auto_expand.isChecked():
            self.toggle.setChecked(True)
        elif not unique and checked:
            self.toggle.setChecked(False)


class MainWindow(QMainWindow):
    sessionChanged = Signal()
    DEBOUNCE_MS = 500
    MANUAL_THRESHOLD = 100_000

    def __init__(self, *, style_repository=None, scheduler=None, preview_lane=None, user_paths=None):
        super().__init__()
        from .identity import configure_identity
        configure_identity()
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self.session = DocumentSession.create()
        self.preview_lane = preview_lane or PreviewLane()
        self.scheduler = scheduler or Scheduler()
        self.reader = self.preview_lane.service.parser.profile
        self.latest_preview = None
        self.preview_issues = ()
        self.export_issues = ()
        self.misc_issues = ()
        self.storage_issues = ()
        self.persistence = None
        self.math_fallbacks = ()
        self.math_verified = False
        self.handle = None
        self.last_result = None
        self.last_output = None
        self.exported_key = None
        self.last_directory = None
        self.loading = False
        self.closing = False
        self.disposed = False
        self.compact = None
        self._style_was_valid = True
        self.setWindowTitle("Markdown 转 Word")
        self.resize(1440, 960)
        self.setMinimumSize(620, 560)
        self.setStyleSheet(STYLE)
        central = QWidget()
        central.setObjectName("workspace")
        self.setCentralWidget(central)
        outer = QVBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        header = QWidget()
        header.setObjectName("header")
        top = QHBoxLayout(header)
        top.setContentsMargins(24, 17, 24, 17)
        top.setSpacing(16)
        mark = self.brand_mark = QLabel("M")
        mark.setText("")
        mark.setPixmap(QApplication.windowIcon().pixmap(46, 46))
        mark.setObjectName("brandMark")
        mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mark.setFixedSize(46, 46)
        top.addWidget(mark)
        brand_stack = QVBoxLayout()
        brand_stack.setSpacing(3)
        brand = self.brand_label = QLabel("Markdown 转 Word")
        brand.setObjectName("brand")
        brand_stack.addWidget(brand)
        self.subtitle = QLabel("本地处理 · 可编辑 Word 文档")
        self.subtitle.setObjectName("subtitle")
        brand_stack.addWidget(self.subtitle)
        top.addLayout(brand_stack)
        top.addStretch()
        self.new_button = self.button("新建", self.new_document)
        self.import_button = self.button("导入文件", self.import_file)
        self.example_button = self.button("示例", self.load_example)
        self.style_toggle = self.button("排版", self.toggle_styles)
        for button in [self.new_button, self.import_button, self.example_button, self.style_toggle]:
            top.addWidget(button)
        self.more = QToolButton()
        self.more.setText("更多")
        self.more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.more)
        for label, callback in [("新建文档", self.new_document), ("加载内置示例", self.load_example), ("展开 / 收起排版设置", self.toggle_styles)]:
            menu.addAction(label, callback)
        self.more.setMenu(menu)
        self.more.hide()
        top.addWidget(self.more)
        self.export_button = self.button("导出 Word", self.export_dialog)
        self.export_button.setObjectName("primary")
        top.addWidget(self.export_button)
        outer.addWidget(header)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setRange(0, 0)
        self.progress.hide()
        outer.addWidget(self.progress)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(24, 20, 24, 12)
        body_layout.setSpacing(12)
        document_row = QHBoxLayout()
        self.document_title = QLabel("未命名文档")
        self.document_title.setObjectName("documentTitle")
        document_row.addWidget(self.document_title)
        self.revision_label = QLabel()
        self.revision_label.setObjectName("muted")
        document_row.addWidget(self.revision_label)
        document_row.addStretch()
        self.status = QLabel("开始一份新的文档")
        self.status.setObjectName("status")
        document_row.addWidget(self.status)
        body_layout.addLayout(document_row)
        self.vertical = QSplitter(Qt.Orientation.Vertical)
        self.vertical.setChildrenCollapsible(False)
        self.horizontal = QSplitter(Qt.Orientation.Horizontal)
        self.horizontal.setChildrenCollapsible(False)
        self.horizontal.setHandleWidth(6)
        self.canvas = QWidget()
        self.canvas_layout = QVBoxLayout(self.canvas)
        self.canvas_layout.setContentsMargins(0, 0, 0, 0)
        self.canvas_layout.setSpacing(0)
        self.columns = QSplitter(Qt.Orientation.Horizontal)
        self.columns.setChildrenCollapsible(False)
        self.columns.setHandleWidth(6)
        self.editor_panel = QWidget()
        self.editor_panel.setObjectName("editorPanel")
        edit_layout = QVBoxLayout(self.editor_panel)
        edit_layout.setContentsMargins(0, 0, 0, 0)
        edit_layout.setSpacing(0)
        bar = QWidget()
        bar.setObjectName("panelBar")
        edit_bar = QHBoxLayout(bar)
        edit_bar.setContentsMargins(18, 10, 14, 10)
        edit_bar.addWidget(QLabel("原文"))
        edit_bar.addStretch()
        self.mode = QComboBox()
        self.mode.addItem("Markdown", InputMode.MARKDOWN)
        self.mode.addItem("纯文本", InputMode.PLAIN)
        self.mode.setAccessibleName("输入模式")
        edit_bar.addWidget(self.mode)
        self.paste_button = self.button("粘贴", self.paste_text)
        edit_bar.addWidget(self.paste_button)
        edit_layout.addWidget(bar)
        self.editor = Editor()
        edit_layout.addWidget(self.editor, 1)
        foot = QHBoxLayout()
        foot.setContentsMargins(18, 8, 14, 8)
        self.count = QLabel("0 字符")
        self.count.setObjectName("muted")
        foot.addWidget(self.count)
        foot.addStretch()
        self.repair_button = self.button("检查外层围栏", self.review_repair)
        foot.addWidget(self.repair_button)
        edit_layout.addLayout(foot)
        self.preview = PreviewPanel()
        self.preview.setObjectName("previewPanel")
        self.preview.setMinimumWidth(240)
        self.columns.addWidget(self.editor_panel)
        self.columns.addWidget(self.preview)
        self.columns.setSizes([470, 650])
        self.tabs = QTabWidget()
        self.tabs.hide()
        self.canvas_layout.addWidget(self.columns, 1)
        self.canvas_layout.addWidget(self.tabs, 1)
        self.horizontal.addWidget(self.canvas)
        self.styles = StylePanel(style_repository)
        self.styles.setMinimumWidth(275)
        self.styles.setMaximumWidth(345)
        from .local_format_panel import LocalFormatPanel
        self.local_panel = LocalFormatPanel(self)
        self.style_tabs = QTabWidget()
        self.style_tabs.setMinimumWidth(275)
        self.style_tabs.setMaximumWidth(365)
        self.style_tabs.addTab(self.styles,"整篇排版")
        self.style_tabs.addTab(self.local_panel,"局部排版")
        self.horizontal.addWidget(self.style_tabs)
        self.horizontal.setStretchFactor(0, 1)
        self.horizontal.setSizes([1070, 290])
        self.vertical.addWidget(self.horizontal)
        self.issues = IssuePanel(self.vertical)
        self.vertical.addWidget(self.issues)
        self.issues.detailsVisibilityChanged.connect(self.resize_issue_panel)
        self.vertical.setSizes([650, 138])
        body_layout.addWidget(self.vertical, 1)
        controls = QHBoxLayout()
        self.refresh_button = self.button("刷新预览", self.request_preview)
        controls.addWidget(self.refresh_button)
        self.manual = QCheckBox("手动刷新")
        self.manual.toggled.connect(self.manual_changed)
        controls.addWidget(self.manual)
        self.motion = QCheckBox("界面动效")
        self.motion.setChecked(True)
        self.motion.toggled.connect(self.set_motion)
        controls.addWidget(self.motion)
        self.help_button = self.button("帮助与关于", self.show_help)
        controls.addWidget(self.help_button)
        controls.addStretch()
        self.cancel_button = self.button("取消导出", self.cancel_export)
        self.cancel_button.hide()
        self.retry_button = self.button("换路径重试", self.retry_dialog)
        self.retry_button.hide()
        self.open_button = self.button("打开 Word", self.open_output)
        self.open_button.hide()
        self.folder_button = self.button("打开目录", self.open_folder)
        self.folder_button.hide()
        for button in [self.cancel_button, self.retry_button, self.open_button, self.folder_button]:
            controls.addWidget(button)
        body_layout.addLayout(controls)
        outer.addWidget(body, 1)
        self.debounce = QTimer(self)
        self.debounce.setSingleShot(True)
        self.debounce.setInterval(self.DEBOUNCE_MS)
        self.debounce.timeout.connect(self.request_preview)
        self.poller = QTimer(self)
        self.poller.timeout.connect(self.poll)
        self.poller.start(80)
        self.editor.textChanged.connect(self.edited)
        self.editor.plainPasted.connect(self.preserve_first_paste)
        self.mode.currentIndexChanged.connect(self.mode_changed)
        self.styles.styleChanged.connect(self.style_changed)
        self.styles.validityChanged.connect(self.style_validity_changed)
        self.styles.issueRaised.connect(self.add_issue)
        self.preview.renderProblem.connect(self.preview_problem)
        self.issues.tree.itemActivated.connect(self.jump_issue)
        self.issues.tree.itemDoubleClicked.connect(self.jump_issue)
        self.shortcuts = []
        for shortcut, callback in [("Ctrl+N", self.new_document), ("Ctrl+O", self.import_file), ("Ctrl+E", self.export_dialog), ("Ctrl+Return", self.request_preview), ("F1", self.show_help)]:
            action = QAction(self)
            action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(callback)
            self.addAction(action)
            self.shortcuts.append(action)
        self.update_revision()
        self.update_buttons()
        QTimer.singleShot(0, self.adapt_layout)
        if user_paths is not None:
            from .persistence import PersistenceController
            self.persistence = PersistenceController(self, user_paths, body_layout)

    def button(self, label, callback):
        button = QPushButton(label)
        button.clicked.connect(callback)
        return button

    def show_help(self):
        from .. import __version__
        dialog = QMessageBox(self)
        dialog.setWindowTitle("帮助与关于")
        dialog.setText("Markdown 转 Word · " + __version__ + "\n作者：暮玥")
        dialog.setInformativeText(
            "本地转换 · 离线预览 · 可编辑 Word\n\n"
            "导入或粘贴 Markdown / TXT，调整排版后导出 Word。\n"
            "预览中的选中文字可使用局部排版。\n"
            "不含 AI 生成，不嵌入图片；预览不代表 Word 的精确分页。\n"
            "自动草稿保存在当前用户数据目录，卸载程序不会自动清除。\n\n"
            "Ctrl+O 导入 · Ctrl+E 导出 · Ctrl+Enter 刷新预览 · F1 帮助")
        guide = dialog.addButton("打开使用手册", QMessageBox.ButtonRole.HelpRole)
        licenses = dialog.addButton("第三方声明", QMessageBox.ButtonRole.HelpRole)
        dialog.addButton(QMessageBox.StandardButton.Close)
        dialog.exec()
        chosen = dialog.clickedButton()
        if chosen in (guide, licenses):
            name = "使用手册.html" if chosen is guide else "第三方声明.html"
            import sys
            document = Path(sys.executable).parent / "说明" / name
            if not getattr(sys, "frozen", False) or not document.is_file():
                document = ResourceLocator().file("help/" + name)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(document)))

    def resize_issue_panel(self, expanded):
        height = max(1, self.vertical.height())
        details = min(180, max(120, height // 4)) if expanded else self.issues.maximumHeight()
        self.vertical.setSizes([max(1, height - details), details])

    def current_key(self):
        return PreviewKey(self.session.session_id, self.session.content_revision,
                          self.session.style_revision, self.reader.fingerprint,
                          self.styles.style.resolved_hash, self.styles.style.template_version,
                          self.math_fallbacks,self.session.local_hash)

    def edited(self):
        if self.loading:
            return
        self.local_panel.remember_content()
        self.session = self.session.edit(self.editor.toPlainText())
        self.local_panel.restore_content_formats()
        self.math_fallbacks = ()
        self.math_verified = False
        self.export_issues = ()
        self.invalidate()

    def mode_changed(self, *_):
        if self.loading:
            return
        self.local_panel.remember_content()
        self.session = self.session.set_mode(self.mode.currentData())
        self.local_panel.restore_content_formats()
        self.math_fallbacks = ()
        self.math_verified = False
        self.export_issues = ()
        self.invalidate()

    def style_changed(self, style):
        if self.loading:
            return
        self.session = self.session.style_changed()
        self.math_fallbacks = ()
        self.math_verified = False
        self.export_issues = ()
        self.invalidate()

    def style_validity_changed(self, valid):
        was_valid, self._style_was_valid = self._style_was_valid, valid
        if not valid:
            self.debounce.stop()
            self.preview.invalidate()
            self.preview.notice.setText("样式预览，分页以 Word 为准 · 参数无效，显示上次有效结果")
        elif not was_valid:
            self.invalidate()
        self.update_buttons()

    def invalidate(self):
        if hasattr(self,"local_panel"):
            self.local_panel.source_changed()
        self.debounce.stop()
        self.preview.invalidate()
        self.preview_issues = ()
        self.misc_issues = ()
        self.issues.show_issues(self.storage_issues, checked=False)
        self.preview.notice.setText("样式预览，分页以 Word 为准 · 内容或样式已变化，预览尚未更新")
        self.status.setText("待刷新" if self.manual.isChecked() else "正在编辑")
        self.update_revision()
        if len(self.session.current_text) >= self.MANUAL_THRESHOLD:
            self.manual.setChecked(True)
            self.manual.setToolTip("达到 100,000 字符，切换为手动刷新以保持编辑响应；原文未截断。")
            self.status.setText("长文已启用手动刷新 · 100,000 字符阈值")
        if not self.manual.isChecked() and self.styles.valid:
            self.debounce.start()
        self.update_buttons()

        self.sessionChanged.emit()

    def update_revision(self):
        self.count.setText(f"{len(self.session.current_text):,} 字符")
        self.revision_label.setText(f"内容 v{self.session.content_revision} · 样式 v{self.session.style_revision}" + (" · 未导出" if self.session.dirty else ""))
        self.setWindowTitle(("* " if self.session.dirty else "") + "Markdown 转 Word")

    def manual_changed(self, enabled):
        self.debounce.stop()
        if not enabled and self.session.current_text:
            self.debounce.start()

    def request_preview(self):
        self.debounce.stop()
        if self.closing or not self.styles.valid:
            return
        if not self.session.current_text.strip():
            self.status.setText("粘贴内容、导入文件或加载示例")
            self.preview.notice.setText("样式预览，分页以 Word 为准 · 当前没有正文")
            return
        self.preview_lane.request(PreviewRequest(self.current_key(), self.session.snapshot(), self.styles.style,self.session.local_formats))
        self.status.setText("正在生成预览")

    def poll(self):
        if self.persistence is not None:
            self.persistence.poll()
        result = self.preview_lane.poll()
        if result is not None and not self.closing and self.styles.valid and result.key == self.current_key():
            result = self.local_panel.accept_preview(result)
            self.preview_issues = result.issues
            if result.failure:
                if self.persistence:
                    self.persistence.logs.record("preview", result.failure.code)
                self.preview.notice.setText("样式预览，分页以 Word 为准 · 预览未更新：" + result.failure.message)
                self.status.setText("预览未更新")
            else:
                self.latest_preview = result
                self.preview.render(result)
                math = (" · 同版本 Word 结构检查通过" if self.math_verified else
                        f" · {result.math_unchecked} 个公式待 Word 转换检查" if result.math_unchecked else "")
                if self.math_fallbacks:
                    math += f" · {len(self.math_fallbacks)} 个公式在 Word 中保留源码"
                self.preview.notice.setText("样式预览，分页以 Word 为准" + math)
                self.status.setText("预览已更新")
            self.refresh_issues()
            self.update_buttons()
        if self.handle is not None:
            for event in self.handle.context.drain_events():
                if self.persistence:
                    self.persistence.logs.record("export", "TASK_" + event.state.value.upper())
                self.status.setText(STATES.get(event.state.value, "正在处理"))
                self.cancel_button.setEnabled(event.state != TaskState.COMMITTING)
            if self.handle.done:
                result = self.handle.poll()
                self.handle = None
                self.export_finished(result)
        if self.closing and self.scheduler.idle and self.preview_lane.idle:
            self.close()
        elif self.persistence is not None and self.persistence.close_requested:
            self.close()

    def refresh_issues(self):
        self.issues.show_issues(self.preview_issues + self.export_issues + self.misc_issues + self.storage_issues)

    def add_issue(self, issue):
        self.misc_issues += (issue,)
        self.refresh_issues()

    def preview_problem(self, message):
        self.add_issue(Issue("PREVIEW_RESOURCES", Severity.WARNING, "preview", message, "恢复本地资源后重新启动。"))

    def update_buttons(self):
        if not hasattr(self, "styles"):
            return
        busy = self.handle is not None
        blocked = any(i.severity == Severity.BLOCKING and i.stage != "preview" for i in self.preview_issues)
        self.export_button.setEnabled(not self.closing and not busy and self.styles.valid and bool(self.session.current_text.strip()) and not blocked)
        self.refresh_button.setEnabled(not self.closing and self.styles.valid)
        self.repair_button.setEnabled(self.session.input_mode == InputMode.MARKDOWN and bool(self.session.current_text))
        self.retry_button.setEnabled(not busy and not self.closing)

    def may_replace(self):
        if not self.session.dirty:
            return True
        return QMessageBox.question(self, "保留当前内容", "当前内容或样式尚未导出。继续会放弃当前编辑，是否继续？", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes

    def replace_session(self, session, title="未命名文档"):
        self.loading = True
        self.session = session
        self.editor.setPlainText(session.current_text)
        self.mode.setCurrentIndex(0 if session.input_mode == InputMode.MARKDOWN else 1)
        self.document_title.setText(title)
        self.loading = False
        self.latest_preview = None
        self.export_issues = ()
        self.math_fallbacks = ()
        self.math_verified = False
        self.exported_key = None
        self.invalidate()
        # Clear the old document even for an empty new session.
        if not session.current_text:
            from ..live_preview import PreviewResult
            self.preview.render(PreviewResult(self.current_key(), html='<div class="empty-sheet"><h1>新的文档，从这里开始。</h1><p>粘贴内容、导入文件或加载示例。</p></div>'))

    def new_document(self):
        if self.may_replace():
            self.replace_session(DocumentSession.create(mode=self.session.input_mode))

    def import_path(self, path):
        session = read_input(Path(path), self.mode.currentData())
        if self.may_replace():
            self.replace_session(session, Path(path).name)
            return True
        return False

    def import_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "导入 UTF-8 文本 · 使用当前输入模式", "", "文本资料 (*.md *.txt)")
        if path:
            try:
                self.import_path(path)
            except DocumentError as exc:
                self.add_issue(exc.issue)

    def load_example(self):
        if self.may_replace():
            session = read_input(ResourceLocator().file("examples/lecture-motion.md"), InputMode.MARKDOWN)
            self.replace_session(replace(session, dirty=True), "课程讲义 · 内置示例")

    def paste_text(self):
        text = QApplication.clipboard().text()
        self.preserve_first_paste(text)
        self.editor.insertPlainText(text)
        self.editor.setFocus()

    def preserve_first_paste(self, text):
        if not self.session.original_text and not self.session.current_text:
            self.session = replace(self.session, original_text=text)

    def apply_repair_suggestion(self, suggestion):
        apply_repair(self.session, suggestion)  # Recheck the unchanged revision before touching Qt.
        cursor = self.editor.textCursor()
        cursor.beginEditBlock()
        cursor.select(QTextCursor.SelectionType.Document)
        cursor.insertText(suggestion.after)
        cursor.endEditBlock()
        self.editor.setTextCursor(cursor)

    def review_repair(self):
        suggestion = suggest_outer_fence(self.session.snapshot()).suggestion
        if suggestion is None:
            QMessageBox.information(self, "外层围栏", "未发现可以安全移除的整篇 Markdown 围栏。没有修改正文。")
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("审阅修复 · 仅移除整篇外层围栏")
        dialog.resize(860, 580)
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel("确认后可用 Ctrl+Z 撤销；原始输入仍保留。"))
        split = QSplitter()
        for title, text in [("修复前", suggestion.before), ("修复后", suggestion.after)]:
            panel = QWidget()
            stack = QVBoxLayout(panel)
            stack.addWidget(QLabel(title))
            edit = QPlainTextEdit(text)
            edit.setReadOnly(True)
            stack.addWidget(edit)
            split.addWidget(panel)
        layout.addWidget(split)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("确认修复")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("保留原文")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            try:
                self.apply_repair_suggestion(suggestion)
            except DocumentError as exc:
                self.add_issue(exc.issue)

    def jump_issue(self, item, *_):
        issue = item.data(0, Qt.ItemDataRole.UserRole)
        if issue is None or issue.location is None:
            return
        try:
            selection = qt_selection(self.session.snapshot(), issue.location)
            if selection is None:
                self.status.setText("仅有上下文位置，请参考原文片段")
                return
            if self.compact:
                self.tabs.setCurrentWidget(self.editor_panel)
            cursor = self.editor.textCursor()
            cursor.setPosition(selection[0])
            cursor.setPosition(selection[1], QTextCursor.MoveMode.KeepAnchor)
            self.editor.setTextCursor(cursor)
            self.editor.ensureCursorVisible()
            self.editor.setFocus()
        except DocumentError as exc:
            self.status.setText(exc.issue.message)

    def save_intent(self, suggested):
        options = QFileDialog.Option.DontConfirmOverwrite
        path, _ = QFileDialog.getSaveFileName(self, "导出 Word · 请选择新的文件名", str((self.last_directory or Path.home() / "Documents") / suggested), "Word 文档 (*.docx)", options=options)
        if not path:
            return None
        target = Path(path)
        if not target.suffix:
            target = target.with_suffix(".docx")
        overwrite = False
        if target.exists():
            answer = QMessageBox.question(self, "同名文件已存在", "该文件已存在。确认后将重新核对目标；当前安全策略可能要求另存，原文件会保留。继续？", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                return None
            overwrite = True
        intent = OutputIntent.capture(target, overwrite=overwrite)
        self.last_directory = target.parent
        return intent

    def start_export(self, intent, snapshot=None):
        if not self.styles.valid or self.closing:
            return
        snapshot = snapshot or ExportSnapshot.capture(self.session, self.styles.style, self.reader)
        self.handle = self.scheduler.submit(snapshot, intent)
        self.progress.show()
        self.cancel_button.setEnabled(True)
        self.cancel_button.show()
        self.retry_button.hide()
        self.status.setText("等待开始")
        self.update_buttons()

    def export_dialog(self):
        if not self.export_button.isEnabled():
            return
        # Capture at the user's action, before native dialogs can run a nested event loop.
        snapshot = ExportSnapshot.capture(self.session, self.styles.style, self.reader)
        suggested = candidate_from_document(self.latest_preview.document) if self.latest_preview and self.latest_preview.key == self.current_key() else "未命名文档.docx"
        try:
            intent = self.save_intent(suggested)
            if intent:
                self.start_export(intent, snapshot)
        except DocumentError as exc:
            self.add_issue(exc.issue)

    def export_finished(self, result):
        self.last_result = result
        self.progress.hide()
        self.cancel_button.hide()
        same = (result.snapshot.input == self.session.snapshot()
                and result.snapshot.style_revision == self.session.style_revision
                and result.snapshot.style == self.styles.style
                and result.snapshot.local_formats == self.session.local_formats
                and self.styles.valid)
        if result.state == TaskState.SUCCEEDED:
            self.last_output = result.output_path
            self.open_button.show()
            self.folder_button.show()
            warnings = any(i.severity == Severity.WARNING for i in result.issues)
            message = "已导出，有警告" if warnings else "导出完成"
            if same:
                self.session = replace(self.session, dirty=False)
                self.sessionChanged.emit()
                self.exported_key = self.current_key()
                self.export_issues = result.issues
                self.math_verified = True
                self.math_fallbacks = tuple(sorted({i.node_id for i in result.issues if i.code == "MATH_FALLBACK" and i.node_id}))
                if self.math_fallbacks:
                    self.request_preview()
                elif self.latest_preview is not None and self.latest_preview.key == self.current_key():
                    self.preview.notice.setText("样式预览，分页以 Word 为准 · 同版本 Word 结构检查通过")
                self.update_revision()
                self.refresh_issues()
            else:
                message += f" · 导出的是内容 v{result.snapshot.input.revision} / 样式 v{result.snapshot.style_revision}，当前编辑未导出"
                self.misc_issues += (Issue("EXPORT_OLDER_VERSION", Severity.INFO, "export", message,
                                           "打开成品核对；需要当前版本时重新导出。"),)
                self.refresh_issues()
            self.status.setText(message)
            if self.persistence:
                self.persistence.logs.record("export", "EXPORT_SUCCEEDED", round(result.elapsed_seconds * 1000))
        else:
            if self.persistence:
                self.persistence.logs.record("export", result.failure.code if result.failure else "EXPORT_" + result.state.value.upper(), round(result.elapsed_seconds * 1000))
            if same:
                self.export_issues = result.issues
            else:
                self.misc_issues += (Issue("EXPORT_OLDER_VERSION", Severity.INFO, "export", "本次导出对应此前版本。", "旧版本的节点问题不会用于定位当前编辑内容。"),)
            if result.failure:
                self.misc_issues += (result.failure,)
            self.status.setText(STATES[result.state.value] + (" · " + result.failure.message if result.failure else ""))
            self.retry_button.setVisible(result.recovery_id is not None)
            self.refresh_issues()
        self.update_buttons()

    def retry_dialog(self):
        if not self.last_result or not self.last_result.recovery_id:
            return
        try:
            intent = self.save_intent("文档_另存.docx")
            if intent:
                self.handle = self.scheduler.retry(self.last_result, intent)
                self.progress.show()
                self.cancel_button.setEnabled(True)
                self.cancel_button.show()
                self.retry_button.hide()
                self.update_buttons()
        except DocumentError as exc:
            self.add_issue(exc.issue)

    def cancel_export(self):
        if self.handle:
            cancelled = self.handle.cancel()
            self.status.setText("正在取消" if cancelled else "文件正在提交，请稍候")

    def open_output(self):
        if self.last_output:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.last_output)))

    def open_folder(self):
        if self.last_output:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.last_output.parent)))

    def toggle_styles(self):
        self.style_tabs.setVisible(not self.style_tabs.isVisible())
        self.adapt_layout()
        if self.style_tabs.isVisible() and self.motion.isChecked():
            if not hasattr(self, "panel_effect"):
                self.panel_effect = QGraphicsOpacityEffect(self.style_tabs)
                self.style_tabs.setGraphicsEffect(self.panel_effect)
                self.panel_animation = QPropertyAnimation(self.panel_effect, b"opacity", self)
                self.panel_animation.setDuration(180)
                self.panel_animation.setEasingCurve(QEasingCurve.Type.OutCubic)
            self.panel_animation.stop()
            self.panel_animation.setStartValue(.4)
            self.panel_animation.setEndValue(1.0)
            self.panel_animation.start()

    def set_motion(self, enabled):
        self.preview.set_motion(enabled)
        if not enabled and hasattr(self, "panel_animation"):
            self.panel_animation.stop()
            self.panel_effect.setOpacity(1.0)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, "columns"):
            self.adapt_layout()

    def adapt_layout(self):
        compact = self.width() < 1120
        narrow = self.width() < 850
        self.brand_label.setText("转 Word" if narrow else "Markdown 转 Word")
        self.brand_mark.setVisible(not narrow)
        self.subtitle.setVisible(not narrow)
        self.new_button.setVisible(not narrow)
        self.example_button.setVisible(not narrow)
        self.style_toggle.setVisible(not narrow)
        self.more.setVisible(narrow)
        if compact == self.compact:
            return
        self.compact = compact
        if compact:
            self.style_tabs.hide()
            self.tabs.addTab(self.editor_panel, "编辑原文")
            self.tabs.addTab(self.preview, "样式预览")
            self.columns.hide()
            self.tabs.show()
        else:
            self.columns.addWidget(self.editor_panel)
            self.columns.addWidget(self.preview)
            self.tabs.hide()
            self.columns.show()
            # Reparenting a tab page keeps its explicit hidden state. Both panes
            # must be shown again when returning to the splitter layout.
            self.editor_panel.show()
            self.preview.show()
            self.columns.setSizes([470, 650])
            self.style_tabs.show()

    def closeEvent(self, event):
        if self.disposed:
            event.accept()
            return
        if not self.closing:
            if self.persistence is not None:
                ready = self.persistence.prepare_close()
            else:
                ready = self.may_replace()
            if not ready:
                event.ignore()
                return
            self.closing = True
            self.debounce.stop()
            self.preview_lane.shutdown()
            self.scheduler.request_shutdown()
            self.update_buttons()
        if not self.scheduler.idle or not self.preview_lane.idle:
            self.status.setText("正在结束后台任务…")
            event.ignore()
            return
        self.poller.stop()
        self.scheduler.dispose()
        if self.persistence is not None:
            self.persistence.dispose()
        self.preview.dispose()
        self.disposed = True
        event.accept()


def run_app(*, user_paths=None):
    app = QApplication.instance() or QApplication([])
    app.setOrganizationName("LocalTools")
    app.setApplicationName("md2word")
    app.setFont(QFont("Microsoft YaHei UI", 10))
    from ..storage.files import UserPaths
    from ..storage.instance import SingleInstance
    from ..templates.service import PersonalStyleRepository
    from ..exporting.service import ExportService
    guard = None
    try:
        paths = user_paths or UserPaths()
        guard = SingleInstance(paths)
        if not guard.acquire():
            return 0
        window = MainWindow(user_paths=paths, style_repository=PersonalStyleRepository(paths.styles),
                            scheduler=Scheduler(ExportService(work_root=paths.tasks)))
        def activate():
            if window.isMinimized():
                window.showNormal()
            window.show()
            window.raise_()
            window.activateWindow()
        guard.activated.connect(activate)
        window.show()
        return app.exec()
    except DocumentError as exc:
        QMessageBox.critical(None, "无法启动", exc.issue.message + "\n" + exc.issue.action)
        return 1
    finally:
        if guard is not None:
            guard.close()
