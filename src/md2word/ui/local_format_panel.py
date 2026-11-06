"""Word-style local controls for a selection in the rendered preview."""
from dataclasses import replace
import json

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QWidget,QVBoxLayout,QFormLayout,QLabel,QPushButton,QHBoxLayout,
    QComboBox,QDoubleSpinBox,QCheckBox,QScrollArea,QMessageBox,QToolButton,QMenu)
from ..domain.local_formatting import CharacterFormat,ParagraphFormat,apply_selection
from ..domain.models import DocumentError
from .typography import FontPicker,SizePicker


class LocalFormatPanel(QWidget):
    def __init__(self,window):
        super().__init__()
        self.window = window
        self.undo_history = []
        self.redo_history = []
        self.history_source = (window.session.session_id,window.session.content_revision)
        self.selection = None
        self.content_history = {}
        self.content_session = window.session.session_id
        self.setObjectName("stylePanel")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16,18,16,16)
        title = QLabel("局部排版")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        self.selection_note = QLabel("在右侧预览中拖选文字，再设置下方格式。")
        self.selection_note.setWordWrap(True)
        self.selection_note.setObjectName("muted")
        layout.addWidget(self.selection_note)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        content = QWidget()
        form = QFormLayout(content)
        form.setContentsMargins(0,0,4,0)
        form.setVerticalSpacing(10)
        self.font_zh = FontPicker(window.styles.fonts)
        self.font_latin = FontPicker(window.styles.fonts)
        for picker,label in ((self.font_zh,"中文字体"),(self.font_latin,"西文字体")):
            picker.insertItem(0,"保持原样")
            picker.setCurrentIndex(0)
            picker.setAccessibleName("选区"+label)
            form.addRow(label,picker)
        self.size = SizePicker()
        self.size.insertItem(0,"保持原样",None)
        self.size.setCurrentIndex(0)
        self.size.setAccessibleName("选区字号")
        form.addRow("字号 / pt",self.size)
        self.letter_spacing = self.optional_spin(-2.1,20,0.1)
        form.addRow("字间距 / pt",self.letter_spacing)
        self.bold,self.italic,self.underline = (QCheckBox(text) for text in ("加粗","斜体","下划线"))
        for box in (self.bold,self.italic,self.underline):
            box.setTristate(True)
            box.setCheckState(Qt.CheckState.PartiallyChecked)
            box.setToolTip("半选：保持原样；勾选：启用；未勾选：取消。")
        styles = QHBoxLayout()
        for box in (self.bold,self.italic,self.underline):
            styles.addWidget(box)
        form.addRow(styles)
        help_text = QLabel("半选表示不修改。字体、字号和字间距只作用于选中文字；公式对象保留原格式。")
        help_text.setWordWrap(True)
        help_text.setObjectName("muted")
        form.addRow(help_text)
        subtitle = QLabel("所在段落")
        subtitle.setObjectName("subTitle")
        form.addRow(subtitle)
        self.line_mode = QComboBox()
        self.line_mode.addItems(["保持原样","倍数行距","固定行距 / pt"])
        self.line_value = QDoubleSpinBox()
        self.line_value.setRange(.8,3)
        self.line_value.setSingleStep(.1)
        self.line_value.setDecimals(2)
        self.line_value.setValue(1.5)
        self.line_value.setEnabled(False)
        self.line_mode.currentIndexChanged.connect(self.line_mode_changed)
        form.addRow("行距类型",self.line_mode)
        form.addRow("行距值",self.line_value)
        self.before = self.optional_spin(-1,72,1)
        self.after = self.optional_spin(-1,72,1)
        form.addRow("段前 / pt",self.before)
        form.addRow("段后 / pt",self.after)
        self.alignment = QComboBox()
        for name,value in (("保持原样",None),("左对齐","left"),("居中","center"),("右对齐","right"),("两端对齐","justify")):
            self.alignment.addItem(name,value)
        form.addRow("对齐",self.alignment)
        note = QLabel("行距、段前段后和对齐会应用到选区涉及的整个段落。")
        note.setWordWrap(True)
        note.setObjectName("muted")
        form.addRow(note)
        scroll.setWidget(content)
        layout.addWidget(scroll,1)
        self.apply_button = QPushButton("应用到选区")
        self.apply_button.setObjectName("primary")
        self.apply_button.clicked.connect(lambda:self.read_selection(self.apply_current))
        layout.addWidget(self.apply_button)
        self.clear_button = QPushButton("清除选区")
        self.clear_button.setToolTip("清除选中文字的字符格式，以及所在段落的局部段落设置。")
        self.clear_button.clicked.connect(lambda:self.read_selection(lambda selection:self.apply_current(selection,clear=True)))
        row = QHBoxLayout()
        row.setSpacing(4)
        row.addWidget(self.clear_button)
        self.undo_button,self.redo_button = QPushButton("撤销"),QPushButton("重做")
        self.undo_button.setToolTip("撤销局部排版")
        self.redo_button.setToolTip("重做局部排版")
        self.undo_button.clicked.connect(self.undo)
        self.redo_button.clicked.connect(self.redo)
        row.addWidget(self.undo_button)
        row.addWidget(self.redo_button)
        more = QToolButton()
        more.setText("⋯")
        more.setAccessibleName("更多局部排版操作")
        more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(more)
        self.clear_all_button = menu.addAction("清除全部局部排版",self.clear_all)
        self.stale_button = menu.addAction("清除失效的局部排版",self.clear_stale)
        self.stale_button.setVisible(False)
        more.setMenu(menu)
        row.addWidget(more)
        layout.addLayout(row)
        window.preview.view.selectionChanged.connect(lambda:self.read_selection())
        self.update_history()

    def optional_spin(self,minimum,maximum,step):
        box = QDoubleSpinBox()
        box.setDecimals(1)
        box.setRange(minimum,maximum)
        box.setSingleStep(step)
        box.setSpecialValueText("保持原样")
        box.setValue(minimum)
        return box

    def line_mode_changed(self,index):
        self.line_value.setEnabled(index != 0)
        self.line_value.setRange(8,72) if index == 2 else self.line_value.setRange(.8,3)
        self.line_value.setValue(18 if index == 2 else 1.5)

    def patch(self):
        def font(picker):
            text = picker.currentText().strip()
            return None if text == "保持原样" else text
        def optional(box):
            return None if box.value() == box.minimum() else box.value()
        def check(box):
            return None if box.checkState() == Qt.CheckState.PartiallyChecked else box.isChecked()
        character = CharacterFormat(font_zh=font(self.font_zh),font_latin=font(self.font_latin),size_pt=self.size.points(),
            letter_spacing_pt=optional(self.letter_spacing),bold=check(self.bold),italic=check(self.italic),underline=check(self.underline))
        spacing = None
        if self.line_mode.currentIndex() == 1:
            spacing = {"mode":"multiple","multiplier":self.line_value.value()}
        elif self.line_mode.currentIndex() == 2:
            spacing = {"mode":"fixed","points":self.line_value.value()}
        paragraph = ParagraphFormat(line_spacing=spacing,before_pt=optional(self.before),after_pt=optional(self.after),
                                    alignment=self.alignment.currentData())
        return character,paragraph

    def read_selection(self,callback=None):
        w = self.window
        key = w.current_key()
        if not w.preview.loaded:
            return
        def received(raw):
            if w.disposed or w.closing or key != w.current_key():
                return
            try:
                selection = json.loads(raw) if raw else None
            except (ValueError,TypeError):
                selection = None
            valid = isinstance(selection,dict) and selection.get("revision") == w.preview.revision_token(key)
            self.selection = selection if valid else None
            if valid and selection.get("blocks"):
                count = sum(item["end"]-item["start"] for item in selection["blocks"])
                self.selection_note.setText(f"已选 {count} 字符 · {len(selection['blocks'])} 个段落")
            elif not w.session.local_formats:
                self.selection_note.setText("在右侧预览中拖选文字，再设置下方格式。")
            if callback:
                if valid and selection.get("blocks"):
                    callback(selection["blocks"])
                else:
                    QMessageBox.information(w,"先选择文字","请在右侧已更新的文档预览中拖选文字，然后应用局部排版。一次最多选择 64 个段落。")
        w.preview.page.runJavaScript("JSON.stringify(window.getLocalSelection?window.getLocalSelection():null)",received)

    def apply_current(self,selection,*,clear=False):
        w = self.window
        result = w.latest_preview
        if result is None or result.key != w.current_key() or result.document is None:
            return
        try:
            character,paragraph = (None,None) if clear else self.patch()
            if not clear and character.empty() and paragraph.empty():
                self.selection_note.setText("请选择至少一项需要改变的格式。")
                return
            values = apply_selection(result.document,w.session.local_formats,selection,character,paragraph,clear=clear)
            w.preview.selection_restore = (w.session.session_id,w.session.content_revision,selection)
            self.commit(values)
        except (ValueError,DocumentError):
            self.selection_note.setText("参数或选区无效。字号使用 6–48 pt，步长 0.5 pt；请刷新后重试。")

    def commit(self,values,*,remember=True):
        w = self.window
        if tuple(values) == w.session.local_formats:
            return
        if remember:
            self.undo_history.append(w.session.local_formats)
            self.undo_history = self.undo_history[-32:]
            self.redo_history.clear()
        w.session = w.session.format_selection(tuple(values))
        self.remember_content()
        w.math_fallbacks = ()
        w.math_verified = False
        w.invalidate()
        w.request_preview()
        self.update_history()

    def accept_preview(self,result):
        w = self.window
        if result.failure is not None or result.document is None:
            return result
        self.stale_button.setVisible(bool(result.local_unresolved))
        if not result.local_unresolved and result.local_resolved != w.session.local_formats:
            w.session = replace(w.session,local_formats=result.local_resolved)
            w.sessionChanged.emit()
            result = replace(result,key=w.current_key())
        self.update_history()
        return result

    def source_changed(self):
        source = (self.window.session.session_id,self.window.session.content_revision)
        if source != self.history_source:
            self.undo_history.clear()
            self.redo_history.clear()
            self.history_source = source
            self.update_history()

    def remember_content(self):
        session = self.window.session
        if session.session_id != self.content_session:
            self.content_history.clear()
            self.content_session = session.session_id
        if session.local_formats or self.content_history:
            key = session.snapshot().content_hash
            self.content_history.pop(key,None)
            self.content_history[key] = session.local_formats
            while (len(self.content_history) > 32 or sum(len(p.text) for values in self.content_history.values() for p in values) > 2_000_000):
                self.content_history.pop(next(iter(self.content_history)))

    def restore_content_formats(self):
        session = self.window.session
        if session.session_id == self.content_session and self.content_history:
            values = self.content_history.get(session.snapshot().content_hash)
            if values is not None:
                self.window.session = replace(session,local_formats=values)

    def update_history(self):
        self.undo_button.setEnabled(bool(self.undo_history))
        self.redo_button.setEnabled(bool(self.redo_history))
        self.clear_all_button.setEnabled(bool(self.window.session.local_formats))

    def undo(self):
        if self.undo_history:
            previous = self.undo_history.pop()
            self.redo_history.append(self.window.session.local_formats)
            self.commit(previous,remember=False)

    def redo(self):
        if self.redo_history:
            value = self.redo_history.pop()
            self.undo_history.append(self.window.session.local_formats)
            self.commit(value,remember=False)

    def clear_all(self):
        if QMessageBox.question(self.window,"清除局部排版","清除当前文档所有局部排版，恢复整篇样式？文字内容保留。",QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            self.commit(())

    def clear_stale(self):
        result = self.window.latest_preview
        if result and result.key == self.window.current_key():
            self.commit(result.local_resolved)
