import math
from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QFormLayout, QLineEdit, QLabel,
    QComboBox, QPushButton, QHBoxLayout, QToolButton, QScrollArea, QInputDialog,
    QMessageBox, QCheckBox)
from ..domain.models import DocumentError
from ..domain.style_models import StyleConfig, ResolvedStyle
from ..templates.service import PersonalStyleRepository, resolve_style
from ..templates.fonts import system_fonts
from .typography import FontPicker, SizePicker, TypographyPreview, size_label


class StylePanel(QWidget):
    styleChanged = Signal(object)
    validityChanged = Signal(bool)
    issueRaised = Signal(object)

    def __init__(self, repository=None, parent=None):
        super().__init__(parent)
        self.setObjectName("stylePanel")
        self.repository = repository or PersonalStyleRepository()
        self.style = resolve_style()
        self.valid = True
        self.loading = False
        self.fields = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 20, 18, 18)
        layout.setSpacing(14)
        title = QLabel("排版设置")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        self.templates = QComboBox()
        self.templates.setAccessibleName("模板选择")
        layout.addWidget(self.templates)
        self.templates.currentIndexChanged.connect(self.choose_template)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget()
        stack = QVBoxLayout(content)
        stack.setContentsMargins(0, 0, 4, 0)
        stack.setSpacing(18)
        common = QFormLayout()
        common.setVerticalSpacing(12)
        common.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        stack.addLayout(common)
        self.fonts = sorted(system_fonts() or {"宋体", "Times New Roman", "Microsoft YaHei", "Arial"})
        self.field(common, "body.font_zh", "中文字体", font=True)
        self.field(common, "body.font_latin", "西文字体", font=True)
        self.field(common, "body.size_pt", "字号 / pt")
        self.size_note = QLabel()
        self.size_note.setWordWrap(True)
        self.size_note.setObjectName("muted")
        common.addRow(self.size_note)
        self.font_preview = TypographyPreview()
        common.addRow(self.font_preview)
        self.spacing = QComboBox()
        self.spacing.addItems(["倍数行距", "固定行距 / pt"])
        self.spacing.currentIndexChanged.connect(self.validate)
        common.addRow("行距类型", self.spacing)
        self.field(common, "body.line_value", "行距值")
        self.field(common, "body.first_line_chars", "首行 / 字符")
        margins = QLabel("页边距 / cm")
        margins.setObjectName("subTitle")
        stack.addWidget(margins)
        margin_form = QFormLayout()
        for key, label in [("top", "上"), ("bottom", "下"), ("left", "左"), ("right", "右")]:
            self.field(margin_form, "page." + key + "_mm", label)
        stack.addLayout(margin_form)
        self.advanced_toggle = QToolButton()
        self.advanced_toggle.setText("高级：标题、表格与代码  +")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setObjectName("textButton")
        stack.addWidget(self.advanced_toggle)
        self.advanced = QWidget()
        advanced = QFormLayout(self.advanced)
        advanced.setContentsMargins(0, 0, 0, 0)
        for level in range(1, 7):
            self.field(advanced, f"headings.{level-1}.size_pt", f"{level} 级标题 / pt")
        self.field(advanced, "table.size_pt", "表格字号 / pt")
        self.field(advanced, "table.cell_horizontal_mm", "单元格左右 / mm")
        self.field(advanced, "table.cell_vertical_mm", "单元格上下 / mm")
        self.field(advanced, "code.font_latin", "代码字体", font=True)
        self.field(advanced, "code.size_pt", "代码字号 / pt")
        self.field(advanced, "quote.indent_mm", "引用缩进 / mm")
        self.page_numbers = QCheckBox("导出页脚页码")
        self.page_numbers.toggled.connect(self.validate)
        advanced.addRow(self.page_numbers)
        self.advanced.hide()
        stack.addWidget(self.advanced)
        self.advanced_toggle.toggled.connect(self.toggle_advanced)
        stack.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)
        self.error = QLabel()
        self.error.setObjectName("error")
        self.error.setWordWrap(True)
        layout.addWidget(self.error)
        for labels, callbacks in [(["另存样式", "恢复默认"], [self.save_style, self.restore]),
                                  (["重命名", "删除"], [self.rename_style, self.delete_style])]:
            row = QHBoxLayout()
            for label, callback in zip(labels, callbacks):
                button = QPushButton(label)
                button.clicked.connect(callback)
                row.addWidget(button)
            layout.addLayout(row)
        self.refresh_templates()
        self.load(self.style)

    def field(self, form, path, label, font=False):
        widget = FontPicker(self.fonts) if font else SizePicker() if path.endswith(".size_pt") else QLineEdit()
        if font:
            widget.currentTextChanged.connect(self.validate)
        elif isinstance(widget, SizePicker):
            widget.currentTextChanged.connect(self.validate)
        else:
            widget.setMaximumWidth(120)
            widget.textChanged.connect(self.validate)
        widget.setAccessibleName(label)
        self.fields[path] = widget
        form.addRow(label, widget)

    def toggle_advanced(self, enabled):
        self.advanced.setVisible(enabled)
        self.advanced_toggle.setText("高级：标题、表格与代码  " + ("−" if enabled else "+"))

    def refresh_templates(self, selected="lecture"):
        self.templates.blockSignals(True)
        self.templates.clear()
        listing = self.repository.list()
        for item in listing.styles:
            self.templates.addItem(item.name, item.id)
        self.templates.setCurrentIndex(max(0, self.templates.findData(selected)))
        self.templates.blockSignals(False)
        for issue in listing.issues:
            self.issueRaised.emit(issue)

    def load(self, style):
        self.loading = True
        self.style = style
        raw = style.config.model_dump(mode="json")
        self.spacing.setCurrentIndex(0 if raw["body"]["line_spacing"]["mode"] == "multiple" else 1)
        for path, widget in self.fields.items():
            if path == "body.line_value":
                spacing = raw["body"]["line_spacing"]
                value = spacing.get("multiplier", spacing.get("points"))
            else:
                value = raw
                for part in path.split("."):
                    value = value[int(part)] if isinstance(value, list) else value[part]
                if path.startswith("page."):
                    value /= 10
            if isinstance(widget, SizePicker):
                widget.set_points(value)
            elif isinstance(widget, QComboBox):
                widget.setCurrentText(str(value))
                widget.lineEdit().setCursorPosition(0)
            else:
                widget.setText(format(value, "g"))
        self.page_numbers.setChecked(raw["page"]["page_numbers"])
        self.loading = False
        self.validate()

    def validate(self, *_):
        if self.loading:
            return
        raw = self.style.config.model_dump(mode="json")
        try:
            for path, widget in self.fields.items():
                text = widget.currentText() if isinstance(widget, QComboBox) else widget.text()
                value = widget.points() if isinstance(widget, SizePicker) else text if isinstance(widget, QComboBox) else float(text)
                if not isinstance(value, str) and not math.isfinite(value):
                    raise ValueError()
                if path == "body.line_value":
                    raw["body"]["line_spacing"] = {"mode": "multiple", "multiplier": value} if self.spacing.currentIndex() == 0 else {"mode": "fixed", "points": value}
                    continue
                if path.startswith("page."):
                    value *= 10
                parts = path.split(".")
                target = raw
                for part in parts[:-1]:
                    target = target[int(part)] if isinstance(target, list) else target[part]
                target[parts[-1]] = value
            raw["page"]["page_numbers"] = self.page_numbers.isChecked()
            resolved = ResolvedStyle(StyleConfig.parse_config(raw))
        except (ValueError, DocumentError):
            self.valid = False
            self.error.setText("参数无效。字号 6–48 pt；倍数 0.8–3；固定 8–72 pt；页边距 0.5–6 cm。请检查输入。")
            self.size_note.setText("参数无效，字体效果保留上次有效设置")
            self.validityChanged.emit(False)
            return
        self.valid = True
        self.error.clear()
        self.size_note.setText("字体效果 · " + size_label(resolved.config.body.size_pt))
        self.font_preview.show_style(resolved)
        self.validityChanged.emit(True)
        if resolved != self.style:
            self.style = resolved
            self.styleChanged.emit(resolved)

    def choose_template(self, *_):
        try:
            self.load(resolve_style(personal=self.repository.load(self.templates.currentData())))
            self.styleChanged.emit(self.style)
        except DocumentError as exc:
            self.issueRaised.emit(exc.issue)

    def restore(self):
        self.refresh_templates()
        self.load(resolve_style())
        self.styleChanged.emit(self.style)

    def save_style(self):
        if not self.valid:
            return
        name, ok = QInputDialog.getText(self, "另存个人样式", "样式名称")
        if ok:
            try:
                saved = self.repository.save(self.style.config, name=name.strip())
                self.refresh_templates(saved.id)
                self.load(resolve_style(personal=saved))
                self.styleChanged.emit(self.style)
            except DocumentError as exc:
                self.issueRaised.emit(exc.issue)

    def rename_style(self):
        identity = self.templates.currentData()
        if identity == "lecture":
            self.error.setText("内置模板只读，请另存为个人样式。")
            return
        name, ok = QInputDialog.getText(self, "重命名样式", "新名称", text=self.templates.currentText())
        if ok:
            try:
                self.repository.rename(identity, name.strip())
                self.refresh_templates(identity)
                self.choose_template()
            except DocumentError as exc:
                self.issueRaised.emit(exc.issue)

    def delete_style(self):
        identity = self.templates.currentData()
        if identity == "lecture":
            self.error.setText("内置模板只读。")
            return
        if QMessageBox.question(self, "删除个人样式", "删除所选个人样式？当前文档内容不受影响。", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No) == QMessageBox.StandardButton.Yes:
            try:
                self.repository.delete(identity)
                self.restore()
            except DocumentError as exc:
                self.issueRaised.emit(exc.issue)
