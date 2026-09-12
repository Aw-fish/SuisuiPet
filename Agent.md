# Agent.md — SuisuiPet（AI 桌宠）项目现状说明

> 本文档面向后续接手的开发者 / AI Agent，用于快速理解当前代码结构、运行方式与实现进度。
> 最后更新：2026-09-12

---

## 1. 项目概述

**SuisuiPet** 是一个基于 **Python + PySide6 (Qt6)** 的桌面 AI 伙伴应用。
当前处于 **MVP 早期骨架阶段**：已完成桌面窗口框架、设置面板、托盘入口、番茄钟与一个**代码绘制（非 Live2D）**的简化桌宠形象；AI 对话与天气、快速笔记等仍为占位或未实现。

- 语言 / 运行时：Python 3（使用了 `dict[str, Any]`、`X | None` 等写法，需 **Python 3.10+**）
- GUI 框架：PySide6
- 依赖清单：**尚未提供 `requirements.txt` / `pyproject.toml`**

---

## 2. 目录结构

```
test/
├── main.py                     # 应用入口
├── readme.md                   # 产品需求（MVP 功能范围）
├── Agent.md                    # 本文档
├── app/
│   ├── __init__.py             # 包声明
│   ├── config.py               # 默认设置 + 读写 data/settings.json
│   └── ui/
│       ├── __init__.py         # 包声明
│       ├── pet_window.py       # 桌宠主窗口 + 聊天气泡/对话窗 + 番茄钟浮窗
│       └── settings_window.py  # 设置主窗口 + 系统托盘
└── data/
    └── settings.json           # 用户设置持久化（当前含测试数据）
```

---

## 3. 核心模块说明

### 3.1 `main.py` — 入口

- 创建 `QApplication`，设置 `setQuitOnLastWindowClosed(False)`（关闭窗口不退出，靠托盘常驻）。
- 创建 `SettingsWindow`（同时负责托盘）与 `PetWindow`。
- 双向接线：
  - `settings.show_from_tray` → 传给 `PetWindow`，作为右键菜单“设置”入口；
  - `settings.apply_external_settings` → 传给 `PetWindow`，用于外部改设置后刷新；
  - `settings.settings_saved` 信号 → `pet.apply_settings`。
- `pet.show()` 后进入事件循环。

### 3.2 `app/config.py` — 配置层

- `ROOT_DIR` / `SETTINGS_PATH = <root>/data/settings.json`。
- `DEFAULT_SETTINGS` 结构：
  - `character.selected`
  - `conversation.{model, api_key, show_floating_dialog}`
  - `motion.mode`（`movable` / `stationary`）
  - `tools.{pomodoro_minutes, weather_city, quick_note_hint}`
- `load_settings()`：深拷贝默认值 → 按分组浅合并磁盘 JSON，异常时安全回落默认值。
- `save_settings()`：`ensure_ascii=False, indent=2` 写入。

### 3.3 `app/ui/pet_window.py` — 桌宠与浮动组件

| 类 | 职责 |
|---|---|
| `ChatDialog` | 无边框、半透明背景、可拖动标题栏的对话窗；消息气泡自适应宽度并自动滚动到底部 |
| `TimerWindow` | 番茄钟倒计时浮窗（跟随桌宠位置），含“停止”按钮 |
| `PetCanvas` | **QPainter 手绘**的占位角色（圆脸 + 眼睛 + 嘴 + 耳朵 + 手臂） |
| `PetWindow` | 桌宠主窗口：拖拽移动、右键菜单、气泡提示、番茄钟、随机游走 |

关键行为：
- 窗口：168×192，`FramelessWindowHint | Tool | WindowStaysOnTopHint`，背景透明。
- **拖拽**：`mousePress/Move/ReleaseEvent` 实现桌面拖动，并同步移动番茄钟浮窗。
- **右键菜单**：切换固定/自由模式、打开/关闭对话框、开始/停止番茄钟、打开设置。
- **动作模式**：
  - `movable`：`_wander()` 定时器（5.5s 间隔、28% 概率）触发 `QPropertyAnimation` 随机位移，并播放 `PetCanvas.act()`；
  - `stationary`：固定原地。
- **气泡提示**：`say(text, duration)` 在角色上方短暂显示文本。
- **番茄钟**：`start_pomodoro()` 读取配置分钟数，`QTimer` 每秒递减；归零后气泡提示“专注完成！”。
- **对话（占位）**：`ChatDialog.send()` 对任意输入固定回复 **“已收到信息，此时显示测试文本123123。”**，无任何网络请求。

### 3.4 `app/ui/settings_window.py` — 设置与托盘

- 无边框 `QMainWindow`（900×620），自定义标题栏拖动，`closeEvent` 改为隐藏。
- 左侧导航 + `QStackedWidget` 三页：
  1. **角色设置**：角色下拉（仅 `Suisui`）、AI 模型名称、API Key（密码态）、角色提示词（`QTextEdit`）。
  2. **模式设置**：动作模式（自由移动 / 固定位置）、对话模式（显示浮动对话框 / 隐藏对话窗口）。
  3. **工具**：番茄钟时长（SpinBox 5–120）、天气城市。
- `save()`：写回 `self.data` → `save_settings()` → 发出 `settings_saved(dict)` → 隐藏窗口。
  - 注意：保存时显式 `self.data["conversation"].pop("api_url", None)`，即当前**没有 API 地址字段**。
- `show_page()`：切页时用 `QGraphicsOpacityEffect` + `QPropertyAnimation` 做淡入。
- **系统托盘**：程序内绘制紫色圆形图标 `QSystemTrayIcon`，右键菜单含“打开设置 / 退出”。
  - 未连接 `activated`（单击/双击）信号，只能通过右键菜单进入设置。

---

## 4. 运行方式

```bash
pip install PySide6
python main.py
```

- 桌宠默认出现在屏幕右下角。
- 设置文件位于 `data/settings.json`，首次运行会自动回落到默认值。

---

## 5. 数据模型（settings.json）

```json
{
  "character":  { "selected": "Suisui" },
  "conversation": {
    "model": "",
    "api_key": "",
    "system_prompt": "",
    "show_floating_dialog": false
  },
  "motion":     { "mode": "stationary" },
  "tools":      { "pomodoro_minutes": 25, "weather_city": "", "quick_note_hint": true }
}
```

> 注意：`system_prompt` 在默认值中未声明，但设置窗口保存时会写入（`_load` 用 `.get()` 容错），属于默认定义不完整。

---

## 6. 对照 readme 的功能实现进度

### ✅ 已实现

| readme 条目 | 实现情况 |
|---|---|
| 角色可在桌面上移动、拖拽和定位 | `PetWindow` 拖拽 + 初始右下角定位 |
| 两种动作模式（可移动 / 原地待机） | 右键菜单与设置面板均可切换，移动模式下有随机游走动画 |
| 提供可隐藏的聊天窗口 | `ChatDialog` 可显示/隐藏，设置面板可控制默认显示状态 |
| 任务栏（通知区域）固定图标 | `QSystemTrayIcon` 常驻 |
| 番茄钟：开始 / 计时 / 结束提醒 | 开始、每秒倒计时、结束气泡提醒、浮窗展示 |
| 设置面板框架 | 三分页、自定义样式、保存持久化 |

### ❌ 尚未实现（按 readme 逐条对照）

**1. Live2D 桌宠角色**
- [ ] 未接入任何 Live2D SDK / 运行时（当前 `PetCanvas` 为 QPainter 手绘占位形象）。
- [ ] 无模型加载、无模型资源目录（`Suisui` 仅在配置里是个名字，无对应资源文件）。
- [ ] 未实现真正的**待机动作与基础动作播放**（`act()` 只是抬手的静态切换，无帧动画、无表情）。
- [ ] 未预留表情 / 语音等扩展接口。

**2. AI 对话**
- [ ] **无真实 API 调用**：`ChatDialog.send()` 返回硬编码测试文本，无网络请求。
- [ ] 设置中**缺少 API 地址（Endpoint）配置项**（代码中反而主动删除 `api_url`）。
- [ ] 无“服务初始化 / 连通性测试”流程，无错误处理、无加载态、无流式输出。
- [ ] **未使用**已配置的 `model` / `api_key` / `system_prompt`。
- [ ] 无对话历史 / 上下文记忆。
- [ ] `show_floating_dialog` 仅控制聊天窗口显隐，**缺少独立的“浮动对话框快速交流”轻量输入形态**。

**3. 轻量实用助手**
- [ ] 番茄钟：**缺少暂停与重置**（readme 要求“开始、暂停、重置”），当前只有开始/停止。
- [ ] 番茄钟结束仅气泡提示，**未使用系统通知**（托盘 `showMessage` 等）。
- [ ] **天气功能完全未实现**：无天气 API、无数据展示、`weather_city` 仅是一个配置输入框。
- [ ] **快速笔记完全未实现**：无创建 / 查看 / 保存笔记的 UI 与存储，仅默认配置里有 `quick_note_hint` 残留字段。

**4. 任务栏与设置**
- [ ] 托盘图标**未连接左键单击/双击**（readme：点击图标可调出设置面板），目前只能通过右键菜单。
- [ ] 设置面板“角色选择与切换”：下拉仅一个 `Suisui`，无多角色资源，切换后角色外观也不会变化。
- [ ] 设置面板“对话 API 配置与初始化”：缺少 API 地址与初始化动作（见上）。
- [ ] 设置面板“快速笔记的基础偏好设置”：缺失。
- [ ] 设置面板“天气偏好”：仅有城市输入框，无生效链路。
- [ ] 设置面板无“显示/隐藏浮动对话框”之外的聊天窗口行为控制（如窗口尺寸、置顶等，readme 未强制）。

**5. 工程化缺口（readme 未写但影响可运行性）**
- [ ] 无 `requirements.txt` / 依赖版本锁定。
- [ ] 无打包 / 发布配置（PyInstaller 等）。
- [ ] 无测试、无 CI。
- [ ] `data/settings.json` 中残留测试数据（`model: "11"`、`api_key: "123"`、`pomodoro_minutes: 11`）。
- [ ] 无 `.gitignore`（`app/**/__pycache__/*.pyc` 已存在于工作区）。

---

## 7. 后续开发建议优先级

1. **配置与依赖补齐**：加 `requirements.txt`、`system_prompt` 默认值、清理测试数据、补 `.gitignore`。
2. **AI 对话打通**：设置中增加 API 地址 + 模型 + Key + 提示词 → 抽出 `conversation/service.py` 负责请求；`ChatDialog.send()` 改为异步调用，带加载态与错误提示。
3. **天气工具**：新增 `tools/weather.py` + 展示入口（角色旁浮窗或气泡）。
4. **快速笔记**：新增 `tools/quick_note.py` + 独立窗口 + `data/notes.json` 存储。
5. **Live2D 接入**：确定运行时方案（如 `live2d-py` / web 引擎嵌入），引入 `assets/` 资源目录并替换 `PetCanvas`。
6. **番茄钟完善**：补充暂停 / 重置，结束改用系统通知。
7. **托盘交互**：连接 `activated` 信号实现单击/双击唤起设置。

---

## 8. 约定与注意事项（给后续 Agent）

- 配置读写统一走 `app.config.load_settings / save_settings`，不要在 UI 中直接读写 JSON。
- 设置变更通过 `SettingsWindow.settings_saved` 信号广播，`PetWindow.apply_settings` 负责响应。
- UI 样式统一写在类内的 QSS 字符串（`_qss()` / `setStyleSheet`），无独立 style 文件。
- 所有窗口使用 `FramelessWindowHint`，拖动逻辑各自实现（`pet_window` 用 `mouseMoveEvent`，`settings_window` 用 `eventFilter`）。
- 保持中文注释 / 文案风格一致。
