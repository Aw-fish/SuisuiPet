"""PySide6 settings window and system-tray integration."""
from __future__ import annotations

from PySide6.QtCore import QEvent, QEasingCurve, QPoint, QPropertyAnimation, Qt, Signal
from PySide6.QtGui import QAction, QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QComboBox, QFrame, QGraphicsOpacityEffect,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu, QPushButton,
    QRadioButton, QSpinBox, QStackedWidget, QSystemTrayIcon, QTextEdit,
    QVBoxLayout, QWidget,
)
from app.config import load_settings, save_settings


class SettingsWindow(QMainWindow):
    settings_saved = Signal(dict)
    ACCENT = "#917DE8"

    def __init__(self) -> None:
        super().__init__()
        self.data = load_settings()
        self.animation: QPropertyAnimation | None = None
        self.drag_position: QPoint | None = None
        self.setWindowTitle("SuisuiPet")
        self.setFixedSize(900, 620)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
        self._build()
        self._build_tray()
        self._load()
        screen = QApplication.primaryScreen()
        if screen:
            self.move(screen.availableGeometry().center() - self.rect().center())

    def _build(self) -> None:
        root = QWidget(objectName="root")
        self.setCentralWidget(root)
        root.installEventFilter(self)
        shell = QHBoxLayout(root)
        shell.setContentsMargins(0, 0, 0, 0)
        shell.setSpacing(0)
        sidebar = QFrame(objectName="sidebar")
        sidebar.setFixedWidth(206)
        nav = QVBoxLayout(sidebar)
        nav.setContentsMargins(14, 22, 14, 18)
        nav.addWidget(QLabel("SuisuiPet", objectName="brand"))
        nav.addWidget(QLabel("陪伴在桌面的一小段时间", objectName="hint"))
        nav.addSpacing(24)
        self.stack = QStackedWidget()
        self.group = QButtonGroup(self)
        for i, title in enumerate(("角色设置", "模式设置", "工具")):
            button = QPushButton(title, objectName="nav")
            button.setCheckable(True)
            button.clicked.connect(lambda checked=False, index=i: self.show_page(index))
            self.group.addButton(button)
            nav.addWidget(button)
            if i == 0:
                button.setChecked(True)
        nav.addStretch()
        nav.addWidget(QLabel("MVP · 本地设置", objectName="hint"))
        content = QFrame(objectName="content")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(42, 25, 42, 28)
        controls = QHBoxLayout()
        controls.addStretch()
        for text in ("—", "×"):
            button = QPushButton(text, objectName="control")
            button.clicked.connect(self.hide)
            controls.addWidget(button)
        content_layout.addLayout(controls)
        self.stack.addWidget(self._character_page())
        self.stack.addWidget(self._mode_page())
        self.stack.addWidget(self._tools_page())
        content_layout.addWidget(self.stack, 1)
        save = QPushButton("保存设置", objectName="save")
        save.clicked.connect(self.save)
        content_layout.addWidget(save, alignment=Qt.AlignRight)
        shell.addWidget(sidebar)
        shell.addWidget(content, 1)
        self.setStyleSheet(self._qss())

    def _page(self, title: str, description: str) -> tuple[QWidget, QVBoxLayout]:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 14, 0, 0)
        layout.addWidget(QLabel(title, objectName="title"))
        text = QLabel(description, objectName="description")
        text.setWordWrap(True)
        layout.addWidget(text)
        layout.addSpacing(20)
        return page, layout

    @staticmethod
    def _label(text: str) -> QLabel:
        return QLabel(text, objectName="label")

    def _character_page(self) -> QWidget:
        page, layout = self._page("角色设置", "选择角色资源，并配置角色使用的 AI 对话服务。")
        layout.addWidget(self._label("角色模型"))
        self.character = QComboBox()
        self.character.addItems(["Suisui（占位角色）", "Demo 角色 A", "Demo 角色 B"])
        layout.addWidget(self.character)
        layout.addSpacing(10)
        layout.addWidget(self._label("AI 模型名称"))
        self.model = QLineEdit()
        self.model.setPlaceholderText("例如：gpt-5")
        layout.addWidget(self.model)
        layout.addWidget(self._label("API Key"))
        self.key = QLineEdit(); self.key.setEchoMode(QLineEdit.Password); self.key.setPlaceholderText("仅保存在本机")
        layout.addWidget(self.key)
        layout.addWidget(self._label("角色提示词"))
        self.prompt = QTextEdit(); self.prompt.setFixedHeight(100); self.prompt.setPlaceholderText("描述角色的性格、语气与对话边界……")
        layout.addWidget(self.prompt)
        layout.addStretch()
        return page

    def _mode_page(self) -> QWidget:
        page, layout = self._page("模式设置", "分别控制角色在桌面上的行为和对话入口。")
        layout.addWidget(self._label("角色动作模式"))
        self.movable, self.stationary = QRadioButton("可移动"), QRadioButton("原地待机")
        self.motion_group = QButtonGroup(self)
        self.motion_group.addButton(self.movable); self.motion_group.addButton(self.stationary)
        layout.addWidget(self.movable); layout.addWidget(QLabel("可以拖拽角色，并允许它在桌面上移动。", objectName="hint"))
        layout.addWidget(self.stationary); layout.addWidget(QLabel("固定在当前位置，仅播放待机或触发动作。", objectName="hint"))
        layout.addSpacing(20)
        layout.addWidget(self._label("对话模式"))
        self.floating, self.hidden = QRadioButton("显示浮动对话框"), QRadioButton("隐藏对话窗口")
        self.dialog_group = QButtonGroup(self)
        self.dialog_group.addButton(self.floating); self.dialog_group.addButton(self.hidden)
        layout.addWidget(self.floating); layout.addWidget(self.hidden)
        layout.addWidget(QLabel("隐藏后仍可从任务栏角标或角色交互中调出。", objectName="hint"))
        layout.addStretch()
        return page

    def _tools_page(self) -> QWidget:
        page, layout = self._page("工具", "设置桌宠提供的轻量日常工具。")
        layout.addWidget(self._label("番茄钟时长（分钟）"))
        self.pomodoro = QSpinBox(); self.pomodoro.setRange(5, 120); self.pomodoro.setSuffix(" 分钟")
        layout.addWidget(self.pomodoro)
        layout.addSpacing(18)
        layout.addWidget(self._label("天气城市"))
        self.city = QLineEdit(); self.city.setPlaceholderText("例如：上海")
        layout.addWidget(self.city)
        layout.addStretch()
        return page

    def _load(self) -> None:
        c, a, t = self.data["character"], self.data["conversation"], self.data["tools"]
        self.character.setCurrentText(c["selected"]); self.model.setText(a["model"]); self.key.setText(a["api_key"])
        self.prompt.setPlainText(a.get("system_prompt", "")); self.movable.setChecked(self.data["motion"]["mode"] == "movable")
        self.stationary.setChecked(not self.movable.isChecked()); self.floating.setChecked(a["show_floating_dialog"]); self.hidden.setChecked(not self.floating.isChecked())
        self.pomodoro.setValue(t["pomodoro_minutes"]); self.city.setText(t["weather_city"])

    def save(self) -> None:
        self.data["character"]["selected"] = self.character.currentText()
        self.data["conversation"].pop("api_url", None)
        self.data["conversation"].update({"model": self.model.text().strip(), "api_key": self.key.text().strip(), "system_prompt": self.prompt.toPlainText().strip(), "show_floating_dialog": self.floating.isChecked()})
        self.data["motion"]["mode"] = "movable" if self.movable.isChecked() else "stationary"
        self.data["tools"].update({"pomodoro_minutes": self.pomodoro.value(), "weather_city": self.city.text().strip()})
        save_settings(self.data)
        self.settings_saved.emit(self.data)
        self.hide()

    def apply_external_settings(self, data: dict) -> None:
        self.data = data
        self._load()

    def _build_tray(self) -> None:
        pixmap = QPixmap(64, 64); pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap); painter.setRenderHint(QPainter.Antialiasing); painter.setPen(Qt.NoPen); painter.setBrush(QColor(self.ACCENT)); painter.drawEllipse(6, 6, 52, 52); painter.end()
        self.tray = QSystemTrayIcon(QIcon(pixmap), self); self.tray.setToolTip("SuisuiPet")
        menu = QMenu(self); menu.setObjectName("trayMenu"); menu.setStyleSheet("QMenu#trayMenu { background: #FFFFFF; border: 1px solid #ECEAF2; border-radius: 10px; padding: 6px; } QMenu#trayMenu::item { color: #4D485A; border-radius: 7px; padding: 8px 34px 8px 12px; } QMenu#trayMenu::item:selected { background: #F0EDFB; color: #6D5DC0; } QMenu#trayMenu::separator { height: 1px; background: #EEEAF4; margin: 5px 8px; }"); open_action = QAction("打开设置", self); open_action.triggered.connect(self.show_from_tray)
        quit_action = QAction("退出", self); quit_action.triggered.connect(QApplication.instance().quit)
        menu.addAction(open_action); menu.addSeparator(); menu.addAction(quit_action); self.tray.setContextMenu(menu); self.tray.show()

    def show_page(self, index: int) -> None:
        if index == self.stack.currentIndex(): return
        self.stack.setCurrentIndex(index); effect = QGraphicsOpacityEffect(self.stack.currentWidget()); self.stack.currentWidget().setGraphicsEffect(effect)
        self.animation = QPropertyAnimation(effect, b"opacity", self); self.animation.setDuration(180); self.animation.setStartValue(.2); self.animation.setEndValue(1.0); self.animation.setEasingCurve(QEasingCurve.OutCubic); self.animation.start()

    def show_from_tray(self) -> None:
        self.data = load_settings()
        self._load()
        self.showNormal(); self.activateWindow(); self.raise_()

    def eventFilter(self, watched, event):  # type: ignore[no-untyped-def]
        if watched is self.centralWidget():
            if event.type() == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton:
                self.drag_position = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            elif event.type() == QEvent.Type.MouseMove and self.drag_position and event.buttons() & Qt.MouseButton.LeftButton:
                self.move(event.globalPosition().toPoint() - self.drag_position)
            elif event.type() == QEvent.Type.MouseButtonRelease:
                self.drag_position = None
        return super().eventFilter(watched, event)

    def closeEvent(self, event) -> None: event.ignore(); self.hide()

    @classmethod
    def _qss(cls) -> str:
        return f'''QWidget#root {{ background:white; border-radius:18px; }} QFrame#sidebar {{ background:#FAFAFD; border-top-left-radius:18px; border-bottom-left-radius:18px; }} QLabel#brand {{ color:#423C64; font:700 19px "Microsoft YaHei UI"; padding:3px 8px; }} QLabel#hint, QLabel#description {{ color:#9993A5; font:10px "Microsoft YaHei UI"; padding:0 8px; }} QPushButton#nav {{ border:0; border-radius:10px; background:transparent; color:#79738E; padding:12px 14px; text-align:left; }} QPushButton#nav:hover {{ background:#F0EDFB; }} QPushButton#nav:checked {{ background:#EEEAFE; color:#6D5DC0; font-weight:600; }} QLabel#title {{ color:#302C40; font:600 22px "Microsoft YaHei UI"; }} QLabel#label {{ color:#625D70; font:600 11px "Microsoft YaHei UI"; margin-top:10px; }} QLineEdit,QComboBox,QSpinBox,QTextEdit {{ background:white; border:1px solid #E9E7EF; border-radius:9px; padding:8px 10px; color:#403C4B; min-height:20px; }} QComboBox::drop-down {{ width:34px; border:0; border-left:1px solid #F0EEF4; }} QSpinBox::up-button,QSpinBox::down-button {{ width:28px; border:0; background:#F7F5FB; }} QSpinBox::up-button {{ border-top-right-radius:8px; }} QSpinBox::down-button {{ border-bottom-right-radius:8px; }} QLineEdit:focus,QComboBox:focus,QSpinBox:focus,QTextEdit:focus {{ border-color:#B8AAF3; }} QRadioButton {{ color:#494451; padding:5px 0; }} QPushButton#control {{ border:0; border-radius:8px; background:transparent; color:#A19CAD; min-width:28px; max-width:28px; min-height:28px; }} QPushButton#control:hover {{ background:#F5F3FA; }} QPushButton#save {{ background:{cls.ACCENT}; border:0; border-radius:10px; color:white; font-weight:600; padding:11px 24px; }} QPushButton#save:hover {{ background:#806CD9; }}'''







