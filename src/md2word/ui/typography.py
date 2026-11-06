"""Native typography pickers; display names never enter the style schema."""
import re

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import QComboBox, QListView, QTextEdit


CHINESE_SIZES = {
    "初号": 42, "小初": 36, "一号": 26, "小一": 24, "二号": 22,
    "小二": 18, "三号": 16, "小三": 15, "四号": 14, "小四": 12,
    "五号": 10.5, "小五": 9, "六号": 7.5, "小六": 6.5,
}
SIZE_NAMES = {points: name for name, points in CHINESE_SIZES.items()}


def size_label(points):
    name = SIZE_NAMES.get(points)
    return f"{name}（{points:g} pt）" if name else f"{points:g} pt"


class FontPicker(QComboBox):
    def __init__(self, families, parent=None):
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(8)
        self.setMaxVisibleItems(12)
        self.setView(QListView())
        self.view().setMinimumWidth(320)
        self.view().setUniformItemSizes(True)
        preferred = ("宋体", "黑体", "楷体", "仿宋", "微软雅黑", "Microsoft YaHei",
                     "Times New Roman", "Arial", "Calibri", "Consolas")
        common = [family for family in preferred if family in families]
        remaining = [family for family in families if family not in common]
        for family in common + remaining:
            self.addItem(family)
            self.setItemData(self.count() - 1, QFont(family, 11), Qt.ItemDataRole.FontRole)
            self.setItemData(self.count() - 1, family, Qt.ItemDataRole.ToolTipRole)
        self.completer().setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.completer().setFilterMode(Qt.MatchFlag.MatchContains)
        self.setToolTip("点击右侧箭头选择本机字体；列表按各自字体显示，也可输入名称搜索。")


class SizePicker(QComboBox):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setEditable(True)
        self.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.setMinimumContentsLength(8)
        self.setMaxVisibleItems(14)
        self.setView(QListView())
        self.view().setMinimumWidth(180)
        for points in (6, 6.5, 7.5, 8, 9, 10, 10.5, 11, 12, 14, 15, 16,
                       18, 20, 22, 24, 26, 28, 32, 36, 42, 48):
            self.addItem(size_label(points), float(points))
        self.completer().setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self.completer().setFilterMode(Qt.MatchFlag.MatchContains)
        self.setToolTip("选择中文字号或磅值；也可输入 6–48 之间的自定义字号，如 13.5。")

    def points(self):
        text = self.currentText().strip()
        index = self.findText(text, Qt.MatchFlag.MatchExactly)
        if index >= 0:
            return self.itemData(index)
        if text in CHINESE_SIZES:
            return float(CHINESE_SIZES[text])
        return float(re.sub(r"\s*pt$", "", text, flags=re.IGNORECASE))

    def set_points(self, points):
        index = self.findData(float(points))
        if index >= 0:
            self.setCurrentIndex(index)
        else:
            self.setEditText(format(points, "g"))


class TypographyPreview(QTextEdit):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("typographyPreview")
        self.setAccessibleName("字体字号效果预览")
        self.setReadOnly(True)
        self.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        self.setFixedHeight(88)
        self.setToolTip("按所选正文字体和字号显示示例。较大字号可滚动查看；文档效果见中间样式预览。")
        self.document().setDocumentMargin(10)

    def show_style(self, style):
        self.clear()
        cursor = QTextCursor(self.document())
        body = style.config.body
        for index, (family, sample) in enumerate(((body.font_zh, "春风有信，文字有形。"),
                                                  (body.font_latin, "Word Preview 012345"))):
            if index:
                cursor.insertBlock()
            font = QFont(family)
            font.setPointSizeF(body.size_pt)
            char_format = QTextCharFormat()
            char_format.setFont(font)
            cursor.insertText(sample, char_format)
        self.moveCursor(QTextCursor.MoveOperation.Start)
        self.horizontalScrollBar().setValue(0)
        self.verticalScrollBar().setValue(0)
