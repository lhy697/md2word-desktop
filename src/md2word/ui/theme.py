"""Single native palette. Document typography is controlled by ResolvedStyle."""
from ..resources import ResourceLocator

STYLE = """
QWidget { color:#25332c; font-family:'Microsoft YaHei UI'; font-size:13px; }
QMainWindow, QWidget#workspace { background:#f3f5f1; }
QWidget#header { background:#ffffff; }
QLabel#brand { font-size:23px; font-weight:600; }
QLabel#brandMark { color:#355e4f; background:#e9f0eb; font-size:24px; font-weight:700; padding:8px; }
QLabel#muted, QLabel#subtitle { color:#65746a; font-size:12px; }
QLabel#sectionTitle { font-size:17px; font-weight:600; }
QLabel#subTitle { font-size:13px; font-weight:600; }
QLabel#documentTitle { font-size:19px; font-weight:600; }
QLabel#previewNotice { background:#e7eee8; color:#3d5849; font-size:12px; padding-left:15px; }
QLabel#error { color:#a43c32; font-size:12px; }
QLabel#status { color:#496353; padding:6px 12px; background:#e8efe9; }
QPushButton, QToolButton { background:#edf1ec; border:0; border-radius:5px; padding:9px 13px; color:#32483b; }
QPushButton:hover, QToolButton:hover { background:#dfe8df; }
QPushButton:pressed, QToolButton:pressed { background:#cadbcc; padding-top:10px; padding-bottom:8px; }
QPushButton:focus, QToolButton:focus { outline:none; border:2px solid #65846c; padding:7px 11px; }
QPushButton:disabled { background:#ecefeb; color:#8a958d; }
QPushButton#primary { background:#355e4f; color:white; font-weight:600; padding:10px 22px; }
QPushButton#primary:hover { background:#294e40; }
QPushButton#primary:pressed { background:#1e4033; }
QPushButton#primary:disabled { background:#cbd7ce; color:#687d6d; }
QToolButton#textButton { background:transparent; padding:5px 0; text-align:left; }
QComboBox, QLineEdit { background:white; border:1px solid #dce3da; border-radius:4px; padding:6px 7px; min-height:19px; selection-background-color:#d8e8dc; }
QComboBox:focus, QLineEdit:focus { border-color:#64816b; }
QComboBox { padding-right:30px; }
QComboBox::drop-down { subcontrol-origin:padding; subcontrol-position:top right; border-left:1px solid #dce3da; width:26px; background:#edf1ec; border-top-right-radius:4px; border-bottom-right-radius:4px; }
QComboBox::down-arrow { image:url('__COMBO_ARROW__'); width:12px; height:8px; }
QComboBox QAbstractItemView { background:white; selection-background-color:#dce9df; }
QComboBox QListView::item { min-height:28px; padding:3px 8px; }
QTextEdit#typographyPreview { background:white; border:1px solid #dce3da; border-radius:4px; selection-background-color:#d8e8dc; }
QPlainTextEdit { background:#fafbf9; border:0; padding:16px 6px; selection-background-color:#d7e8dd; }
QWidget#editorPanel, QWidget#previewPanel { background:white; }
QWidget#panelBar { background:#fff; }
QWidget#stylePanel { background:#fdfefc; }
QTreeWidget { border:0; background:#fdfefb; alternate-background-color:#f5f8f2; }
QTreeWidget::item { padding:7px 4px; }
QTreeWidget::item:selected { background:#e0eade; color:#263d2f; }
QHeaderView::section { background:#f0f4ed; border:0; padding:7px; text-align:left; }
QTabWidget::pane { border:0; }
QTabBar::tab { padding:10px 20px; background:#edf1e9; margin-right:4px; }
QTabBar::tab:selected { background:#dbe8dd; color:#284d37; }
QScrollArea { border:0; background:transparent; }
QScrollBar:vertical { width:8px; background:transparent; }
QScrollBar::handle:vertical { background:#cbd5c9; min-height:30px; border-radius:4px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height:0; }
QSplitter::handle { background:#e4eae0; }
QSplitter::handle:hover { background:#a7bba9; }
QProgressBar { background:#e2e9de; border:0; min-height:4px; max-height:4px; }
QProgressBar::chunk { background:#628869; }
QCheckBox { spacing:8px; }
QDialog { background:#f5f7f2; }
"""
STYLE = STYLE.replace("__COMBO_ARROW__", ResourceLocator().file("ui/chevron-down.svg").as_posix())
