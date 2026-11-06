from PySide6.QtCore import Qt, QSize, QRect, Signal
from PySide6.QtGui import QColor, QPainter, QFont
from PySide6.QtWidgets import QPlainTextEdit, QWidget


class LineNumbers(QWidget):
    def __init__(self, editor):
        super().__init__(editor)
        self.editor = editor

    def sizeHint(self):
        return QSize(self.editor.number_width(), 0)

    def paintEvent(self, event):
        self.editor.paint_numbers(event)


class Editor(QPlainTextEdit):
    plainPasted = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAccessibleName("文档内容编辑器")
        self.setPlaceholderText("在这里粘贴你的内容…\n\n支持 Markdown 标题、列表、表格、代码与公式。\n也可以选择纯文本模式，保留符号的字面含义。")
        self.setFont(QFont("Consolas", 12))
        self.setTabStopDistance(self.fontMetrics().horizontalAdvance(" ") * 4)
        self.numbers = LineNumbers(self)
        self.blockCountChanged.connect(self.update_margin)
        self.updateRequest.connect(self.update_numbers)
        self.update_margin()

    def number_width(self):
        return 22 + self.fontMetrics().horizontalAdvance("9") * len(str(max(1, self.blockCount())))

    def update_margin(self, *_):
        self.setViewportMargins(self.number_width(), 0, 12, 0)

    def update_numbers(self, rect, dy):
        if dy:
            self.numbers.scroll(0, dy)
        else:
            self.numbers.update(0, rect.y(), self.numbers.width(), rect.height())
        if rect.contains(self.viewport().rect()):
            self.update_margin()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        rect = self.contentsRect()
        self.numbers.setGeometry(QRect(rect.left(), rect.top(), self.number_width(), rect.height()))

    def paint_numbers(self, event):
        painter = QPainter(self.numbers)
        painter.fillRect(event.rect(), QColor("#fafbf9"))
        block = self.firstVisibleBlock()
        index = block.blockNumber()
        top = round(self.blockBoundingGeometry(block).translated(self.contentOffset()).top())
        bottom = top + round(self.blockBoundingRect(block).height())
        painter.setPen(QColor("#6d7971"))
        while block.isValid() and top <= event.rect().bottom():
            if block.isVisible() and bottom >= event.rect().top():
                painter.drawText(0, top, self.numbers.width()-12, self.fontMetrics().height(), Qt.AlignmentFlag.AlignRight, str(index+1))
            block = block.next()
            index += 1
            top = bottom
            bottom = top + round(self.blockBoundingRect(block).height())

    def insertFromMimeData(self, source):
        self.plainPasted.emit(source.text())
        self.insertPlainText(source.text())
