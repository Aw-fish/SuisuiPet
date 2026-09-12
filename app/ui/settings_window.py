"""PySide6 settings window and system-tray integration."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QEasingCurve, QPoint, QPropertyAnimation, QThread, Qt, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices

from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QComboBox, QDoubleSpinBox, QFileDialog, QFrame,
    QGraphicsOpacityEffect, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu,
    QPushButton, QRadioButton, QScrollArea, QSlider, QSpinBox, QStackedWidget,
    QSystemTrayIcon, QTextEdit, QVBoxLayout, QWidget,
)
from app import characters
from app.config import load_settings, save_settings
from app.conversation.service import check_connection
from app.ui.dialogs import StyledDialog
from app.ui.icons import app_icon
from app.ui.menus import styled_menu


class _ConnectWorker(QThread):
    """在后台跑「测试连接」，避免界面卡住。"""

    done = Signal(bool, str)

    def __init__(self, info: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._info = dict(info)

    def run(self) -> None:
        try:
            ok, message = check_connection(self._info)
        except Exception as exc:  # noqa: BLE001 - 线程边界统一兜住
            ok, message = False, f"测试失败：{exc}"
        self.done.emit(ok, message)


class ComboBox(QComboBox):
    """下拉框：去掉系统方投影，并把圆角画在弹出容器上，避免露出直角。

    Qt 的弹出列表装在一个 ``QComboBoxPrivateContainer`` 里，`QAbstractItemView`
    的圆角只会作用于内部列表，容器本身仍是直角底；这里让容器透明、由 QSS
    自己画圆角卡片，并关掉系统投影。
    """

    def showPopup(self) -> None:  # noqa: N802 - 覆盖 Qt 虚函数
        super().showPopup()
        container = self.view().window()
        if container.property("suisuiRounded"):
            return
        container.setWindowFlags(
            container.windowFlags() | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint
        )
        container.setAttribute(Qt.WA_TranslucentBackground)
        container.setStyleSheet(
            "background: white; border: 1px solid #E9E7EF; border-radius: 10px;"
        )
        container.setProperty("suisuiRounded", True)
        container.show()


class SettingsWindow(QMainWindow):
    settings_saved = Signal(dict)
    ACCENT = "#917DE8"

    def __init__(self) -> None:
        super().__init__()
        self.data = load_settings()
        self.animation: QPropertyAnimation | None = None
        self.drag_position: QPoint | None = None
        self._active_character: str | None = None
        self._character_cache: dict[str, dict] = {}
        self._test_worker: _ConnectWorker | None = None
        self.setWindowTitle("SuisuiPet")
        self.setFixedSize(900, 740)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
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
        # nav.addWidget(QLabel("陪伴在桌面的一小段时间", objectName="hint"))
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
        """标题固定在顶部，字段区域可滚动，避免以后加设置项时被挤扁。"""
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 14, 0, 0)
        outer.addWidget(QLabel(title, objectName="title"))
        text = QLabel(description, objectName="description")
        text.setWordWrap(True)
        outer.addWidget(text)
        outer.addSpacing(16)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(0, 0, 10, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)
        return page, layout

    @staticmethod
    def _label(text: str) -> QLabel:
        return QLabel(text, objectName="label")

    def _character_page(self) -> QWidget:
        page, layout = self._page("角色设置", "每个角色拥有独立文件夹，包含立绘、对话配置与记忆。")
        layout.addWidget(self._label("角色选择"))
        picker = QHBoxLayout()
        picker.setSpacing(8)
        self.character = ComboBox()
        self.character.currentTextChanged.connect(self._on_character_changed)
        self.add_character = QPushButton("添加角色", objectName="mini")
        self.add_character.setToolTip("新建一个角色文件夹")
        self.add_character.clicked.connect(self._add_character)
        self.import_character_button = QPushButton("导入角色", objectName="mini")
        self.import_character_button.setToolTip("复制一个已有的角色文件夹进来")
        self.import_character_button.clicked.connect(self._import_character)
        self.remove_character = QPushButton("删除角色", objectName="mini")
        self.remove_character.setToolTip("断开连接或连同数据一起删除")
        self.remove_character.clicked.connect(self._remove_character)
        picker.addWidget(self.character, 1)
        picker.addWidget(self.add_character)
        picker.addWidget(self.import_character_button)
        picker.addWidget(self.remove_character)
        layout.addLayout(picker)
        layout.addSpacing(10)
        layout.addWidget(self._label("角色模型"))
        formats = QHBoxLayout()
        formats.setSpacing(16)
        self.img_format = QRadioButton("img格式")
        self.live2d_format = QRadioButton("live2d格式")
        self.format_group = QButtonGroup(self)
        self.format_group.addButton(self.img_format)
        self.format_group.addButton(self.live2d_format)
        self.img_format.toggled.connect(self._on_format_changed)
        formats.addWidget(self.img_format)
        formats.addWidget(self.live2d_format)
        formats.addStretch()
        layout.addLayout(formats)
        layout.addWidget(self._label("模型文件"))
        self.asset_path = QLineEdit()
        self.asset_path.setReadOnly(True)
        self.asset_path.setPlaceholderText("角色目录下的 sprites 文件夹")
        self.browse_button = QPushButton("打开目录", objectName="mini")
        self.browse_button.clicked.connect(self._browse_asset)
        asset_row = QHBoxLayout()
        asset_row.setSpacing(8)
        asset_row.addWidget(self.asset_path, 1)
        asset_row.addWidget(self.browse_button)
        layout.addLayout(asset_row)
        layout.addWidget(self._label("移动频率"))
        activity_row = QHBoxLayout()
        activity_row.setSpacing(10)
        self.activity = QSlider(Qt.Horizontal)
        self.activity.setRange(1, 10)
        self.activity.setSingleStep(1)
        self.activity.setPageStep(1)
        self.activity.setFixedHeight(24)
        self.activity.valueChanged.connect(self._on_activity_changed)
        self.activity_value = QLabel(objectName="chip")
        self.activity_value.setAlignment(Qt.AlignCenter)
        self.activity_value.setFixedWidth(52)
        activity_row.addWidget(self.activity, 1)
        activity_row.addWidget(self.activity_value)
        layout.addLayout(activity_row)
        layout.addWidget(QLabel("数值越大，角色随机移动越频繁、距离越远。", objectName="hint"))
        layout.addWidget(self._label("AI 模型名称"))
        self.model = QLineEdit()
        self.model.setPlaceholderText("例如：deepseek-chat")
        layout.addWidget(self.model)
        layout.addWidget(self._label("API 地址"))
        self.base_url = QLineEdit()
        self.base_url.setPlaceholderText("例如：https://api.deepseek.com/v1")
        layout.addWidget(self.base_url)
        layout.addWidget(self._label("API Key"))
        self.key = QLineEdit()
        self.key.setEchoMode(QLineEdit.Password)
        self.key.setPlaceholderText("仅保存在本机，character.json 不会被上传")
        layout.addWidget(self.key)
        params = QHBoxLayout()
        params.setSpacing(12)
        temperature_box = QVBoxLayout()
        temperature_box.addWidget(self._label("温度"))
        self.temperature = QDoubleSpinBox()
        self.temperature.setRange(0.0, 2.0)
        self.temperature.setSingleStep(0.1)
        self.temperature.setDecimals(1)
        temperature_box.addWidget(self.temperature)
        limit_box = QVBoxLayout()
        limit_box.addWidget(self._label("上下文轮数"))
        self.context_limit = QSpinBox()
        self.context_limit.setRange(2, 50)
        self.context_limit.setSuffix(" 轮")
        limit_box.addWidget(self.context_limit)
        params.addLayout(temperature_box, 1)
        params.addLayout(limit_box, 1)
        layout.addLayout(params)
        layout.addWidget(self._label("角色提示词"))
        self.prompt = QTextEdit(); self.prompt.setFixedHeight(88); self.prompt.setPlaceholderText("描述角色的性格、语气与对话边界……")
        layout.addWidget(self.prompt)
        test_row = QHBoxLayout()
        test_row.setSpacing(10)
        self.test_button = QPushButton("测试连接", objectName="mini")
        self.test_button.clicked.connect(self._test_connection)
        self.test_result = QLabel("", objectName="hint")
        self.test_result.setWordWrap(True)
        test_row.addWidget(self.test_button)
        test_row.addWidget(self.test_result, 1)
        layout.addLayout(test_row)
        layout.addStretch()
        return page

    def _on_activity_changed(self, value: int) -> None:
        self.activity_value.setText(f"{value} / 10")

    def _mode_page(self) -> QWidget:
        page, layout = self._page("模式设置", "分别控制角色在桌面上的行为和对话入口。")
        layout.addWidget(self._label("角色动作模式"))
        self.movable, self.stationary = QRadioButton("自由移动"), QRadioButton("固定位置")
        self.motion_group = QButtonGroup(self)
        self.motion_group.addButton(self.movable); self.motion_group.addButton(self.stationary)
        layout.addWidget(self.movable); layout.addWidget(QLabel("允许在桌面上移动。", objectName="hint"))
        layout.addWidget(self.stationary); layout.addWidget(QLabel("固定在当前位置。", objectName="hint"))
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

    # ---- 角色目录与配置 ----------------------------------------------------

    def _registered(self) -> list[str]:
        return self.data["character"]["registered"]

    def _character_info(self, name: str) -> dict:
        """角色配置先进内存缓存，点「保存设置」时统一写入 character.json。"""
        if name not in self._character_cache:
            self._character_cache[name] = characters.load_character(name)
        return self._character_cache[name]

    def _refresh_character_combo(self, selected: str) -> None:
        self.character.blockSignals(True)
        self.character.clear()
        self.character.addItems(self._registered())
        self.character.setCurrentText(selected)
        self.character.blockSignals(False)
        self._active_character = selected
        self._load_character_fields(selected)

    def _load_character_fields(self, name: str) -> None:
        info = self._character_info(name)
        is_img = info.get("format", "img") != "live2d"
        self.img_format.setChecked(is_img)
        self.live2d_format.setChecked(not is_img)
        self._show_asset_target(name, info)
        self.model.setText(str(info.get("model", "")))
        self.base_url.setText(str(info.get("base_url", "")))
        self.key.setText(str(info.get("api_key", "")))
        try:
            temperature = float(info.get("temperature", 0.8))
        except (TypeError, ValueError):
            temperature = 0.8
        self.temperature.setValue(max(0.0, min(2.0, temperature)))
        try:
            limit = int(info.get("max_context_messages", 20))
        except (TypeError, ValueError):
            limit = 20
        self.context_limit.setValue(max(2, min(50, limit)))
        self.prompt.setPlainText(str(info.get("system_prompt", "")))
        try:
            activity = int(info.get("activity", 5))
        except (TypeError, ValueError):
            activity = 5
        self.activity.setValue(max(1, min(10, activity)))
        self._on_activity_changed(self.activity.value())
        self.test_result.setText("")
        self.test_result.setStyleSheet("")

    def _show_asset_target(self, name: str, info: dict) -> None:
        """img 格式固定指向角色目录下的 sprites，live2d 指向所选模型文件。"""
        if info.get("format", "img") != "live2d":
            self.asset_path.setText(str(characters.sprite_dir(name, info)))
            self.browse_button.setText("打开目录")
            self.browse_button.setToolTip("打开该角色的立绘文件夹")
        else:
            self.asset_path.setText(str(info.get("asset_path", "")))
            self.browse_button.setText("浏览")
            self.browse_button.setToolTip("选择 Live2D 模型文件（*.json）")

    def _store_character_fields(self, name: str) -> None:
        if not name or name not in self._registered():
            return
        info = self._character_info(name)
        is_img = self.img_format.isChecked()
        info.update({
            "format": "img" if is_img else "live2d",
            "asset": characters.SPRITE_DIRNAME,
            "asset_path": "" if is_img else self.asset_path.text().strip(),
            "model": self.model.text().strip(),
            "base_url": self.base_url.text().strip(),
            "api_key": self.key.text().strip(),
            "temperature": round(self.temperature.value(), 1),
            "max_context_messages": self.context_limit.value(),
            "system_prompt": self.prompt.toPlainText().strip(),
            "activity": self.activity.value(),
        })

    # ---- 对话配置与连接测试 ------------------------------------------------

    def _conversation_info(self) -> dict:
        """用界面上当前的值组一份配置，供测试连接使用（不必先保存）。"""
        info = dict(self._character_info(self._active_character)) if self._active_character else {}
        info.update({
            "model": self.model.text().strip(),
            "base_url": self.base_url.text().strip(),
            "api_key": self.key.text().strip(),
            "temperature": round(self.temperature.value(), 1),
            "max_context_messages": self.context_limit.value(),
            "system_prompt": self.prompt.toPlainText().strip(),
        })
        return info

    def _test_connection(self) -> None:
        if self._test_worker is not None:
            return
        self.test_button.setEnabled(False)
        self.test_result.setStyleSheet("")
        self.test_result.setText("测试中…")
        worker = _ConnectWorker(self._conversation_info(), self)
        worker.done.connect(self._on_test_done)
        worker.finished.connect(worker.deleteLater)
        self._test_worker = worker
        worker.start()

    def _on_test_done(self, ok: bool, message: str) -> None:
        self._test_worker = None
        self.test_button.setEnabled(True)
        self.test_result.setStyleSheet(f"color:{'#3FA46A' if ok else '#D0605F'};")
        self.test_result.setText(message)

    def _flush_characters(self) -> None:
        for name in self._registered():
            info = self._character_cache.get(name)
            if info is not None:
                characters.save_character(name, info)

    def _activate(self, name: str) -> None:
        """切换当前角色，并把注册表立即写入 settings.json。"""
        self.data["character"]["selected"] = name
        self._flush_characters()
        save_settings(self.data)
        self._refresh_character_combo(name)

    def _on_format_changed(self) -> None:
        if not hasattr(self, "asset_path") or not self._active_character:
            return
        info = self._character_info(self._active_character)
        info["format"] = "img" if self.img_format.isChecked() else "live2d"
        self._show_asset_target(self._active_character, info)

    def _on_character_changed(self, name: str) -> None:
        if not name or name == self._active_character:
            return
        if self._active_character in self._registered():
            self._store_character_fields(self._active_character)
        self._active_character = name
        self._load_character_fields(name)

    def _add_character(self) -> None:
        name = StyledDialog.ask_text(
            self,
            "添加角色",
            "为新角色创建一个独立文件夹，立绘、对话配置与记忆都会放在里面。",
            placeholder="例如：Suisui",
        )
        if name is None:
            return
        name = name.strip()
        if not characters.is_valid_name(name):
            StyledDialog.notice(self, "添加角色", '角色名不能为空，也不能包含 \\ / : * ? " < > | 这些字符。')
            return
        self._store_character_fields(self._active_character or "")
        if name in self._registered():
            StyledDialog.notice(self, "添加角色", f"角色“{name}”已经在列表里了。")
            return
        if characters.character_dir(name).is_dir():
            # 之前「断开连接」保留下来的文件夹，直接重新挂上
            characters.ensure_character(name)
        else:
            name = characters.create_character(name)
        self._registered().append(name)
        self._character_cache.pop(name, None)
        self._activate(name)

    def _import_character(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择角色文件夹", str(Path.home()))
        if not folder:
            return
        self._store_character_fields(self._active_character or "")
        try:
            name = characters.import_character(folder)
        except (OSError, ValueError) as exc:
            StyledDialog.notice(self, "导入角色", f"导入失败：{exc}")
            return
        self._registered().append(name)
        self._character_cache.pop(name, None)
        self._activate(name)

    def _remove_character(self) -> None:
        name = self._active_character or self.data["character"]["selected"]
        if name not in self._registered():
            return
        if len(self._registered()) <= 1:
            StyledDialog.notice(self, "删除角色", "至少需要保留一个角色。")
            return
        choice = StyledDialog.choose(
            self,
            "删除角色",
            f"角色“{name}”的文件夹里包含立绘、对话配置与记忆。\n\n"
            "· 断开连接：只从列表移除，文件夹完整保留，之后用「添加角色」同名即可重新挂上\n"
            "· 删除数据：连同文件夹一起永久删除，无法恢复",
            options=(("disconnect", "断开连接", "ghost"), ("delete", "删除数据", "danger")),
        )
        if choice is None:
            return
        self._registered().remove(name)
        self._character_cache.pop(name, None)
        if choice == "delete":
            characters.delete_character_data(name)
        self._activate(self._registered()[0])

    def _browse_asset(self) -> None:
        name = self._active_character
        if not name:
            return
        info = self._character_info(name)
        if info.get("format", "img") != "live2d":
            directory = characters.sprite_dir(name, info)
            directory.mkdir(parents=True, exist_ok=True)
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))
            return
        current = self.asset_path.text().strip()
        path, _ = QFileDialog.getOpenFileName(
            self, "选择 Live2D 模型文件", current, "Live2D 模型 (*.json);;所有文件 (*)"
        )
        if path:
            self.asset_path.setText(path)

    def _load(self) -> None:
        self._character_cache.clear()
        c, a, t = self.data["character"], self.data["conversation"], self.data["tools"]
        registered = self._registered()
        selected = c["selected"] if c["selected"] in registered else registered[0]
        c["selected"] = selected
        self._refresh_character_combo(selected)
        self.movable.setChecked(self.data["motion"]["mode"] == "movable")
        self.stationary.setChecked(not self.movable.isChecked())
        self.floating.setChecked(a["show_floating_dialog"]); self.hidden.setChecked(not self.floating.isChecked())
        self.pomodoro.setValue(t["pomodoro_minutes"]); self.city.setText(t["weather_city"])

    def save(self) -> None:
        name = self._active_character or self.data["character"]["selected"]
        if name not in self._registered():
            name = self._registered()[0]
        self._store_character_fields(name)
        self.data["character"]["selected"] = name
        self.data["conversation"].pop("api_url", None)
        self.data["conversation"]["show_floating_dialog"] = self.floating.isChecked()
        self.data["motion"]["mode"] = "movable" if self.movable.isChecked() else "stationary"
        self.data["tools"].update({"pomodoro_minutes": self.pomodoro.value(), "weather_city": self.city.text().strip()})
        self._flush_characters()
        save_settings(self.data)
        self.settings_saved.emit(self.data)
        self.hide()

    def apply_external_settings(self, data: dict) -> None:
        self.data = data
        self._load()

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(app_icon(), self); self.tray.setToolTip("SuisuiPet")
        menu = styled_menu(self); open_action = QAction("打开设置", self); open_action.triggered.connect(self.show_from_tray)
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
        return f'''QWidget#root {{ background:white; border:1px solid #ECEAF2; border-radius:18px; }} QFrame#sidebar {{ background:#FAFAFD; border-top-left-radius:18px; border-bottom-left-radius:18px; }} QLabel#brand {{ color:#423C64; font:700 19px "Microsoft YaHei UI"; padding:3px 8px; }} QLabel#hint, QLabel#description {{ color:#9993A5; font:10px "Microsoft YaHei UI"; padding:0 8px; }} QPushButton#nav {{ border:0; border-radius:10px; background:transparent; color:#79738E; padding:12px 14px; text-align:left; }} QPushButton#nav:hover {{ background:#F0EDFB; }} QPushButton#nav:checked {{ background:#EEEAFE; color:#6D5DC0; font-weight:600; }} QLabel#title {{ color:#302C40; font:600 22px "Microsoft YaHei UI"; }} QLabel#label {{ color:#625D70; font:600 11px "Microsoft YaHei UI"; margin-top:6px; }} QLineEdit,QComboBox,QSpinBox,QDoubleSpinBox,QTextEdit {{ background:white; border:1px solid #E9E7EF; border-radius:9px; padding:8px 10px; color:#403C4B; min-height:20px; }} QComboBox::drop-down {{ width:34px; border:0; border-left:1px solid #F0EEF4; }} QSpinBox::up-button,QSpinBox::down-button,QDoubleSpinBox::up-button,QDoubleSpinBox::down-button {{ width:28px; border:0; background:#F7F5FB; }} QSpinBox::up-button,QDoubleSpinBox::up-button {{ border-top-right-radius:8px; }} QSpinBox::down-button,QDoubleSpinBox::down-button {{ border-bottom-right-radius:8px; }} QLineEdit:focus,QComboBox:focus,QSpinBox:focus,QDoubleSpinBox:focus,QTextEdit:focus {{ border-color:#B8AAF3; }} QRadioButton {{ color:#494451; padding:5px 0; }} QPushButton#control {{ border:0; border-radius:8px; background:transparent; color:#A19CAD; min-width:28px; max-width:28px; min-height:28px; }} QPushButton#control:hover {{ background:#F5F3FA; }} QPushButton#save {{ background:{cls.ACCENT}; border:0; border-radius:10px; color:white; font-weight:600; padding:11px 24px; }} QPushButton#save:hover {{ background:#806CD9; }} QPushButton#mini {{ background:#F4F1FB; border:1px solid #E9E5F5; border-radius:9px; color:#6D5DC0; font:600 11px "Microsoft YaHei UI"; padding:9px 12px; }} QPushButton#mini:hover {{ background:#EBE6FA; }} QPushButton#mini:pressed {{ background:#E2DAF8; }} QLineEdit:read-only {{ background:#FAFAFD; color:#7A7488; border-color:#EFEDF5; }} QLabel#chip {{ background:#F4F1FB; border:1px solid #E9E5F5; border-radius:8px; color:#6D5DC0; font:600 11px "Microsoft YaHei UI"; padding:5px 0; }} QSlider::groove:horizontal {{ height:6px; background:#EFEDF7; border-radius:3px; }} QSlider::sub-page:horizontal {{ background:{cls.ACCENT}; border-radius:3px; }} QSlider::add-page:horizontal {{ background:#EFEDF7; border-radius:3px; }} QSlider::handle:horizontal {{ width:12px; height:12px; margin:-5px 0; background:white; border:2px solid {cls.ACCENT}; border-radius:8px; }} QSlider::handle:horizontal:hover {{ border-color:#806CD9; }} QComboBox QAbstractItemView {{ background:transparent; border:0; border-radius:10px; padding:4px; outline:0; color:#403C4B; selection-background-color:#F0EDFB; selection-color:#6D5DC0; }} QComboBox QAbstractItemView::item {{ min-height:22px; padding:4px 8px; }} QScrollArea {{ background:transparent; border:0; }} QScrollArea > QWidget > QWidget {{ background:transparent; }} QScrollBar:vertical {{ background:transparent; width:8px; margin:0; }} QScrollBar::handle:vertical {{ background:#DFDAEC; border-radius:4px; min-height:30px; }} QScrollBar::handle:vertical:hover {{ background:#CFC8E0; }} QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical {{ height:0; }} QScrollBar::add-page:vertical,QScrollBar::sub-page:vertical {{ background:transparent; }}'''







