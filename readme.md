# AI 桌宠软件（SuisuiPet）

一个面向桌面的 AI 伙伴应用。第一阶段聚焦可感知的角色互动、轻量 AI 对话与几项高频桌面工具；角色素材与动作可先使用 Demo 或占位资源，便于尽早验证整体体验。

## 运行

```bash
pip install PySide6 Pillow
python main.py
```

- 桌宠默认出现在屏幕右下角，**右键角色**可打开功能菜单；托盘图标（紫色圆点）右键可打开设置。
- 需要 Python 3.10+（代码使用了 `dict[str, Any]`、`X | None` 等写法）。
- `Pillow` 仅用于重新生成占位素材的脚本，运行应用本身不需要。

## MVP 功能范围与实现进度（截至 2026-09-12）

### 1. 桌宠角色

- ✅ **img 格式立绘渲染**：按文件夹加载透明背景 PNG 序列，窗口按立绘宽高比自适应，显示高度 224px。
- ✅ 待机与基础动作播放：`Idle` / `Move` / `Talk` / `Focus` / `Drag` 五套动作，按状态配置帧率，缺帧时按 `动作 → Idle → Default` 兜底。
- ✅ 找不到立绘时隐藏桌宠并弹出「找不到立绘！」提示（已移除内置手绘占位形象）。
- ✅ 角色可在桌面上移动、拖拽和定位；拖拽时播放 `Drag`，松开回到基础状态。
- ✅ 两种动作模式：**可移动**（自由移动 + 随机游走） / **原地待机**（固定当前位置）。
- ✅ 移动方向镜像；随机移动与拖拽各自独立的方向规则（见「开发约定」）。
- ✅ 动作频率可调：`移动频率` 1–10，同时控制触发间隔、触发概率与移动距离。
- ✅ 表情：整张全身立绘的表情，可从右键菜单固定或恢复默认。
- ⬜ **Live2D 未接入**：`live2d格式` 目前只保存 JSON 路径，不参与渲染。
- ⬜ 语音、音效、更多动作包。

### 2. AI 对话

- ✅ 可隐藏的浮动对话窗，消息气泡自适应宽度、自动滚底、标题栏可拖动。
- ✅ 每个角色独立配置 `AI 模型名称 / API Key / 角色提示词`。
- ⬜ **无真实 API 调用**：`ChatDialog.send()` 目前返回固定测试文本。
- ⬜ 设置中缺少 **API 地址** 配置项。
- ⬜ 已配置的模型 / Key / 提示词尚未被使用；无初始化与连通性测试、无流式输出、无错误处理。
- ⬜ 无对话历史与上下文记忆。

### 3. 轻量实用助手

- ✅ **番茄钟**：可配置时长（5–120 分钟）、倒计时浮窗跟随角色、结束后气泡提醒。
- ⬜ 番茄钟缺少**暂停与重置**；结束仅气泡提示，未使用系统通知。
- ⬜ **天气**：完全未实现（仅有一个城市输入框）。
- ⬜ **快速笔记**：完全未实现（仅保留 `quick_note_hint` 配置字段）。

### 4. 任务栏与设置

- ✅ 系统托盘常驻图标，右键菜单可打开设置 / 退出。
- ✅ 设置面板（900×740，无边框三页）：
  - **角色设置**：角色选择 + 添加 / 导入 / 删除角色（可断开连接或删除数据）、角色模型格式（img / live2d）、模型文件位置、AI 模型名称、API Key、角色提示词、移动频率；
  - **模式设置**：动作模式、对话模式（显示浮动对话框 / 隐藏对话窗口）；
  - **工具**：番茄钟时长、天气城市。
- ✅ 设置持久化到 `data/settings.json`，旧格式自动迁移。
- ⬜ 托盘图标未连接单击/双击事件，目前只能通过右键菜单进入设置。
- ⬜ 对话 API 配置缺少初始化流程。

## 项目结构

```
SuisuiPet/
├── main.py                          # 入口：QApplication + 设置窗口 + 桌宠窗口
├── readme.md
├── app/
│   ├── paths.py                     # 共享路径常量
│   ├── config.py                    # 全局设置（data/settings.json）读写与旧格式迁移
│   ├── characters.py                # 角色目录的创建 / 导入 / 删除与配置读写
│   ├── pet/
│   │   ├── character_sprite.py      # 扁平文件夹立绘扫描、兜底链、镜像、透明背景校验
│   │   └── animator.py              # 帧播放驱动（基础状态 + 临时动作 + 固定表情）
│   └── ui/
│       ├── pet_window.py            # 桌宠窗口、右键菜单、气泡、番茄钟浮窗、对话窗
│       ├── settings_window.py       # 设置主窗口 + 系统托盘
│       ├── dialogs.py               # 与设置界面同风格的输入 / 确认 / 多选对话框
│       ├── menus.py                 # 圆角、无方形投影的菜单
│       └── icons.py                 # 应用 / 托盘图标加载
├── data/
│   ├── settings.json                # 全局设置 + 角色注册表（不进版本控制）
│   ├── icon.png / icon.ico          # 应用与托盘图标（由 tools/make_icon.py 生成）
│   ├── img/Default.png              # 源立绘，仅作为生成脚本的输入
│   └── characters/<角色名>/
│       ├── character.json           # 该角色的全部配置
│       ├── sprites/                 # img 格式立绘
│       └── memory/                  # 对话记忆（不进版本控制）
└── tools/
    ├── make_placeholder_frames.py   # 从源立绘生成某个角色的占位素材
    └── make_icon.py                 # 生成应用图标
```

**一个角色 = 一个文件夹**，把 `data/characters/<角色名>/` 整体拷走即可迁移。

## 立绘素材规范（img 格式）

img 格式的立绘固定放在角色目录下的 `sprites/`，用文件名前缀区分动作：

| 文件名 | 含义 | 帧数 | fps |
|---|---|---|---|
| `Default.png` | 默认立绘，**必需**（缺失则整个文件夹不可用） | 1 | — |
| `Idle1..n.png` | 待机循环 | 2~4 | 5 |
| `Move1..n.png` | 移动 | 4~6 | 10 |
| `Talk1..n.png` | 说话口型 | 3 | 9 |
| `Focus1..n.png` | 番茄钟专注 | 4 | 4 |
| `Drag1..n.png` | 拖拽悬空 | 2 | 8 |
| `Blink / Happy / Sad / Angry / Surprised / Think .png` | 表情（整张全身立绘，单帧） | 各 1 | — |

- 命名规则为 `<动作名><可选帧序号>`，无序号表示单帧；`_` 开头的文件与目录会被忽略。
- 除上表中的动作名外，其他名字都会被识别为**表情**。
- 所有立绘必须**同尺寸且带透明背景**，否则该文件夹会被跳过并回落到其他可用形象。
- 表情为「整张全身立绘」方案，不做分层叠加，因此各图需保持一致的画布与对齐。

### 重新生成占位素材

```bash
python tools/make_placeholder_frames.py                      # 写入 data/characters/Suisui/sprites/
python tools/make_placeholder_frames.py --character Suisui   # 指定角色
python tools/make_placeholder_frames.py --simplify           # 输出二值化简化版线稿
python tools/make_placeholder_frames.py --source <图片路径>   # 换一张源立绘
```

以 `data/img/Default.png` 为源，生成 `Default / Idle / Move / Talk / Focus / Drag` 与 6 个表情。脚本可重复执行、不会修改源图；表情暂用默认立绘副本占位，等美术出图后**同名覆盖即可，代码无需改动**。

### 重新生成应用图标

```bash
python tools/make_icon.py        # 写出 data/icon.png 与 data/icon.ico
```

图标与界面主色一致（紫色圆角底板 + 线稿风格团子脸）。应用启动时通过 `app.setWindowIcon()` 设为任务栏图标，托盘也复用同一张图。

## 配置与数据

### 全局设置 `data/settings.json`

只保存全局偏好与**角色注册表**（首次运行自动回落默认值，旧格式自动迁移）：

```json
{
  "character": { "selected": "Suisui", "registered": ["Suisui"] },
  "conversation": { "show_floating_dialog": false },
  "motion": { "mode": "stationary" },
  "tools": { "pomodoro_minutes": 25, "weather_city": "", "quick_note_hint": true }
}
```

- `motion.mode`：`movable` 自由移动 / `stationary` 固定位置。
- `registered` 是已挂载的角色名列表；文件夹不存在的条目会在加载时自动剔除。
- 旧版把角色配置内嵌在 `settings.json` 里的结构，会在首次加载时自动拆分到角色目录。

### 角色配置 `data/characters/<角色名>/character.json`

```json
{
  "name": "Suisui",
  "format": "img",
  "asset": "sprites",
  "asset_path": "",
  "model": "",
  "api_key": "",
  "system_prompt": "",
  "activity": 5
}
```

- `format`：`img`（立绘文件夹）/ `live2d`（模型 JSON）。
- `asset`：img 格式的立绘目录，默认是角色目录下的 `sprites`。
- `asset_path`：仅 live2d 使用，指向模型文件。
- `activity`：1–10，数值越大则随机动作越频繁、移动距离越远（等级 1 约 9.0s / 3% / ±30px，等级 10 约 2.7s / 52.5% / ±138px）。
- 角色设置页改的就是当前选中角色的这份配置，**点「保存设置」后写入**。

### 角色管理

| 操作 | 效果 |
|---|---|
| **添加角色** | 新建 `characters/<名字>/`（含 `character.json`、`sprites/`、`memory/`）；若同名文件夹已存在（之前「断开连接」留下的），直接重新挂载 |
| **导入角色** | 选一个已有的角色文件夹，复制进 `characters/` 并挂载（重名自动加 `-2`） |
| **断开连接** | 只从 `registered` 移除，文件夹完整保留，之后同名「添加角色」即可恢复 |
| **删除数据** | 连同整个角色文件夹（立绘 + 配置 + 记忆）永久删除 |

## 交互速查

| 操作 | 效果 |
|---|---|
| 拖动角色 | 移动位置，按拖拽方向镜像，松开回到基础状态 |
| 右键角色 | 固定/自由模式 · 表情 · 对话窗 · 番茄钟 · 设置 |
| 托盘右键 | 打开设置 / 退出 |

## 开发约定

- 全局设置读写统一走 `app.config.load_settings / save_settings`，不要在 UI 中直接读写 JSON。
- 角色数据一律通过 `app.characters` 读写，不要再把角色字段写回 `settings.json`。
- 设置变更通过 `SettingsWindow.settings_saved` 信号广播，由 `PetWindow.apply_settings` 响应。
- 立绘动作名与表情名集中在 `app/pet/character_sprite.py`（`ACTION_NAMES` / `EXPRESSION_LABELS`）。
- 移动方向镜像由 `pet_window.py` 的 `wander_mirrored()`（随机移动）与 `drag_mirrored()`（拖拽）分别决定，素材默认朝向由 `ART_FACES_LEFT` 常量控制，改这一处即可整体反转。
- 样式统一写在类内的 QSS 字符串中，无独立样式文件；窗口均为无边框，拖动逻辑各自实现。

## 后续可迭代方向

- 更丰富的动作、表情、音效和角色资源包。
- 对话记忆、人格配置与语音交互。
- 日程、待办和更多桌面效率工具。
- 角色行为的时间、天气或专注状态联动。
- 工程化补齐：`requirements.txt`、打包发布（PyInstaller）、测试与 CI。
