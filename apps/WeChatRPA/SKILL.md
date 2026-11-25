---
name: wechat-intelligent-chat
description: Windows 微信视觉 RPA — 无坐标模式。基于 win32gui 动态窗口定位 + 视觉 AI 聊天消息区分析，自动监控聊天消息并发送 AI 回复；内置朋友圈自动发布模块；提供本地 Web 控制台统一管理。支持多联系人轮询、抗抖变化检测、消息变化确认、守株待兔等待、回复延迟节奏控制与本地/视觉判定冲突熔断。配置通过 runtime/ 下的 JSON 文件或 Web 控制台完成。
---

# 微信视觉 RPA — 无坐标模式

**一句话：** 截图右侧聊天消息区 → 视觉 AI 分析 → 自动回复；另可自动发朋友圈；双击 bat 起 Web 控制台，浏览器里点按钮管理。

## 项目结构

```
apps/WeChatRPA/                  # 位于 x.HR 启动器(Launcher)目录下，随宿主一同分发
├── 启动微信RPA.bat              # 备用：一键启动 Web 控制台（自动开浏览器）
├── SKILL.md                     # 本文件
└── runtime/                     # 运行目录（所有可执行代码和配置）
    ├── app.py                       # ★ Web 控制台后端（纯标准库 http.server，端口 8765）
    ├── web/index.html               # ★ 控制台前端（黑白灰极简窄边栏）
    ├── wechat_vision_rpa.py         # ★ 聊天监控主脚本
    ├── ai_chat_strategy.py          # AI 对话策略引擎（VisualChatStrategy）
    ├── wechat_moments_publisher.py  # ★ 朋友圈自动发布模块
    ├── user_config.json             # API Key / model / base_url + 运行参数
    ├── my_contacts.json             # 联系人名单 + 聊天目标
    ├── chat_strategy_config.json    # AI 角色 / 策略 / 隐私规则 / 主动发送 / 回复延迟
    ├── change_detect_config.json    # 「是否有新消息」判定阈值
    ├── relay_config.json            # 达成事件转发（可选）
    ├── moments_config.json          # 朋友圈按钮坐标偏移 / 节奏参数
    ├── moments_queue.json           # 朋友圈发布队列
    ├── requirements.txt / _full.txt  # 依赖清单
    ├── python/                      # 内置便携 Python 3.11（bat 直接调它，不依赖系统 Python）
    ├── logs/                        # 运行日志（自动生成）
    ├── screenshots/
    │   ├── chat/                    # 聊天正式截图（每轮调 AI 前保存的画面）
    │   ├── compare/                 # 对比/调试截图（前后帧对比 + 朋友圈定位截图）
    │   └── moments/                 # 待发送朋友圈的素材图片
    ├── conversation_memory/         # 按联系人落盘的对话记忆
    └── events/                      # 达成事件（pending/ + delivered/）
```

## 前置条件

- **Windows** 系统
- **微信 PC 版** 已安装并登录（可最小化到系统托盘，程序会自动唤出）
- **网络** 能访问 AI API
- 无需自己装 Python：`runtime/python/` 是便携版，bat 自动用它

## 启动

### 方式一：双击 bat（推荐）

```
双击「启动微信RPA.bat」
```

会做三件事：
1. 用内置便携 Python 起 `app.py`
2. 自动打开浏览器到 `http://127.0.0.1:8765`
3. 在网页里点按钮启动/停止聊天监控、发朋友圈、改配置、看日志

### 方式二：命令行直接跑（高级）

```powershell
cd runtime
python wechat_vision_rpa.py            # 聊天监控
python wechat_moments_publisher.py --open-only   # 只开朋友圈窗口，用于校准坐标
python wechat_moments_publisher.py --text "文案" --images a.jpg b.jpg   # 立即发一条
python wechat_moments_publisher.py --queue        # 跑发布队列
```

### 聊天监控命令行参数

| 参数 | 说明 |
|------|------|
| _(无参数)_ | 监控模式：全部联系人跑完后等待 15 秒再轮询 |
| `--once 联系人名` | 单次模式：截图→AI→发送后退出 |
| `--test-vision 联系人名` | 视觉测试：仅 AI 分析，不发送 |
| `--scan` | 扫描当前所有微信窗口信息（HWND / rect / PID / exe） |
| `-i, --interval N` | 每轮全部联系人跑完后的等待秒数（默认 15） |
| `--max-cycles N` | 最大循环次数（默认 0 = 不限） |
| `--account-id ID` | 当前微信账号记忆空间 ID，隔离同名联系人历史 |

## Web 控制台

浏览器打开 `http://127.0.0.1:8765`，左侧窄边栏六个面板：

| 面板 | 干什么 |
|------|--------|
| 仪表盘 | 状态灯（绿=运行中/灰=停）、一键启停聊天监控和朋友圈队列 |
| 聊天监控 | 联系人表格增删改，保存写回 `my_contacts.json` |
| 朋友圈 | 填文案+图片路径立即发；查看队列状态 |
| 策略配置 | 角色设定、策略文本、回复延迟、主动发消息间隔 |
| API 设置 | key / base_url / model / fallbacks |
| 运行日志 | 下拉选日志文件，黑底终端风 tail 显示 |

后端是纯标准库 `http.server`，零额外依赖；通过 `subprocess.Popen` 启停 RPA 子进程，配置直接读写 JSON 文件。

## 配置

### 1. API 与运行参数 — `runtime/user_config.json`

```json
{
  "lobster": {
    "api_key": "你的API_Key",
    "base_url": "http://your-api:8080/v1/chat/completions",
    "model": "qwen",
    "model_fallbacks": ["kimi"],
    "enable_thinking": false
  }
}
```

- `model_fallbacks`：主模型失败时按顺序尝试的备用模型。
- `enable_thinking`：`false`（默认）直接生成回复；`true` 让推理模型先思考再回复。

> **密钥安全**：`user_config.json` 含明文 API Key 且被 git 跟踪。若密钥敏感，请轮换后改用本地覆盖文件或环境变量，不要提交真实 Key。

### 2. 变化判定阈值 — `runtime/change_detect_config.json`（可选）

```json
{
  "enabled": true,
  "strong_fraction": 0.02,
  "min_fraction": 0.0002,
  "cluster_min_cells": 12,
  "cluster_min_rows": 2,
  "cluster_min_cols": 4
}
```

- `strong_fraction`：整幅差异格占比超过它即判定「整版位移 = 有新消息」。不填用默认 2%。
- `min_fraction`：占比低于它直接当噪声，不进入局部判定。
- `cluster_min_cells` / `cluster_min_rows` / `cluster_min_cols`：**局部连通变化块**门槛（1 格 = 8×8px）。

> 这个文件**不是** `user_config.json`：Web 控制台每次从界面启动 RPA 都会用只含 `lobster` 段的内容重写 `user_config.json`，写在里面的阈值会被清掉。

### 3. 联系人 — `runtime/my_contacts.json`

```json
{
  "group_name": "Default",
  "strategy": "",
  "contacts": [
    { "name": "微信备注名", "gender": "未知", "chat_goal": "聊天目标描述" }
  ],
  "default_chat_goal": "当某联系人未单独写 chat_goal 时的兜底目标"
}
```

### 4. AI 角色与策略 — `runtime/chat_strategy_config.json`

```json
{
  "ai_role": "我方角色设定",
  "strategy_text": "## 策略要求……（角色设定 / 节奏 / 样板对话）",
  "privacy_strict_finance": false,
  "privacy_strict_contacts": false,
  "privacy_custom_rules": "",
  "proactive_enabled": false,
  "proactive_interval_seconds": 1800,
  "min_reply_delay_seconds": 0
}
```

- `proactive_enabled` / `proactive_interval_seconds`：是否允许在对方长时间（默认 1800s）没消息时**主动**发起话题。关闭时仅被动回复。
- `min_reply_delay_seconds`：**回复延迟节奏**。检测到对方新消息后，先等满该秒数再调 AI 回复，模拟真人不秒回。默认 `0`（关闭）。延迟期内若对方又发来新消息，从最新一条重新计时。

> 策略/联系人 JSON 修改后需**重启监控进程**生效（Web 控制台里点停止再启动即可）。

### 5. 朋友圈 — `runtime/moments_config.json`

```json
{
  "sidebar": { "x_offset": 28, "y_offset": 218 },
  "camera_button": { "rx": 0.965, "ry": 0.045 },
  "post_button": { "rx": 0.94, "ry": 0.93 },
  "text_box": { "rx": 0.35, "ry": 0.35 },
  "step_delay_seconds": 1.0,
  "window_wait_seconds": 3.0,
  "post_interval_seconds": 300,
  "debug_screenshots": true
}
```

- `sidebar.x_offset / y_offset`：主窗口左侧栏"朋友圈"图标相对窗口左上角的像素偏移。
- `camera_button / post_button / text_box`：朋友圈窗口/编辑窗口上各按钮的**归一化位置**（0~1，相对窗口宽高）。
- **首次使用必须校准**：先跑 `python wechat_moments_publisher.py --open-only`，打开朋友圈后看 `screenshots/compare/` 里的截图，量出相机/发表按钮实际在窗口的什么比例，改这里。默认值是通用布局拍的，不同微信版本/窗口尺寸大概率要点偏。

朋友圈队列 `moments_queue.json`：

```json
{
  "items": [
    { "text": "文案", "images": ["D:\\assets\\1.jpg"], "status": "pending", "created_at": "..." }
  ]
}
```

`status` 取值 `pending / done / failed`，队列模式发完自动更新。

## 聊天监控核心流程

```
查找微信窗口 → 激活窗口 → 搜索联系人 → 裁剪右侧聊天消息区截图
    → 抗抖指纹变化检测 → (变化) 确认复查 → [延迟节奏闸门] → 视觉 AI 分析
    → 本地/视觉冲突熔断 → 拆句发送回复 → 守株待兔等待 → 下一轮
```

**关键机制：**
- **无坐标**：`win32gui` 枚举窗口动态获取矩形；截图裁掉左侧会话列表和底部输入框
- **抗抖变化检测**：聊天区转灰度→1/8 降采样→以主背景亮度算「相对暗度」，按 **8 级桶宽**量化为感知指纹。判定分两段：
  - **整版位移**：差异格占比 > `strong_fraction`（默认 2%）
  - **局部新增**：占比不大，但出现一块足够大的**连通变化块**（默认 ≥12 格、≥2 行、≥4 列）
- **变化确认**：检测到变化后延迟 3 秒复查，取稳定后的最新截图
- **延迟节奏闸门**：按 `min_reply_delay_seconds` 等待后才回复
- **冲突熔断**：本地像素判定与视觉模型判定连续冲突达阈值时，强制写入基准落定
- **拆句发送**：优先按换行拆成多条（上限 5 条）；无换行时只按句末标点「。！？」拆分，**不按逗号拆**
- **句柄刷新**：每轮重新激活窗口，防止 Chromium 窗口重建导致句柄失效

## 朋友圈发布流程

```
主窗口点左侧栏朋友圈图标 → 等朋友圈窗口弹出
  → 点右上角相机 → 等编辑窗口弹出
  → 点输入框 → pyperclip 粘文案
  → 逐张加图：点图片区 → Alt+N 聚焦文件对话框 → 粘路径 → Enter
  → 点发表 → 等编辑窗口自动关闭 = 成功
```

每步存调试截图到 `screenshots/compare/`，校准坐标时对照看。

## 退出

- 聊天监控窗口按 **ESC** 或 **Ctrl+C** 停止
- Web 控制台直接关 bat 窗口即可（会顺手 terminate 子进程）

## 常见问题

**窗口激活失败 `(0, 'SetForegroundWindow')`**
→ Windows 前台权限限制，已内置三策略容错（AttachThreadInput → SwitchToThisWindow → ShowWindow 直调）。

**句柄失效 / 窗口找不到**
→ 微信基于 Chromium（WeChatAppEx），窗口可能重建。程序会自动重新枚举窗口获取新句柄。

**中文乱码**
→ bat 脚本已设置 `chcp 65001`；Python 脚本内置 `_setup_console_utf8()` 强制 UTF-8 输出。

**朋友圈点了没反应 / 点偏了**
→ 坐标没校准。跑 `--open-only`，对照 `screenshots/compare/` 里的截图量实际位置，改 `moments_config.json`。

**对方明明发了消息却不回复（日志一直 `无新消息，跳过`）**
→ 看 `runtime/logs/` 里的 `[检测]` 行：
1. 「零星变化 / 占比过低」：把 `cluster_min_cells` 调小（如 8）、`min_fraction` 调小（如 0.0001）
2. 「无变化」：截图没抓到新内容（窗口被遮挡、独立会话窗口、弹窗盖住），对照 `screenshots/compare/` 里那轮的 `*_nochange_*.png`
3. 「有新消息」之后又出现 `[冷却]` / `[熔断]`：模型判定「无需回复」，看 `[AI] 无需回复判定` 行

**对方只发了一个句号 / 单字 / 表情，AI 判成不回复**
→ 已内置三层保护，正常不该再漏：提示词里的【极短消息规则】、策略层「末条为对方极短消息时定向重试」、
RPA 层 `bare_message_needs_reply()` 强制接话。日志里会打印 `[AI] 兜底: 对方只发了「。」，强制接话`。
若仍漏回，先确认 `[AI] 无需回复判定` 里的 `last_sender`：
- 是 `other` 却仍不回 → 检查 `bare_message_needs_reply()` 是否把这个字符判进去了（标点集在 `_PUNCT_ONLY_CHARS`）；
- 是 `me`/`unknown` → 说明模型把句号气泡当成界面残留丢了，此时看 `screenshots/chat/` 原图确认气泡是否完整入镜
  （被裁掉一半会同时命中裁剪问题，见上一条裁剪边距）。

**区分「嗯嗯是同意」和「嗯嗯是敷衍」**
→ 同一个「嗯」「嗯嗯」「好」「行」「可以」，含义看我方上一句，判定在 `ack_is_agreement()`：
- 我方上一句是提问/邀约/让确认（结尾带「？」「吗」「呢」，或含「是否可行」「方不方便」「哪天」「一起」
  「行不行」「见个面」等）→ 这是**同意**，不转人工，并顺着目标推进下一步（约时间/地点/材料）。
  日志打印 `[联动] … 对方的「嗯嗯」是在答应我方上一句，顺势推进不转人工`。
- 我方上一句只是陈述、闲聊或已收尾 → 才是敷衍，走 `is_perfunctory_or_closing_other_text()` →
  `[达成] … 停止自动回复` 转人工。
- 我方上一句取 `last_me_message_text()`（`conversation_memory/` 里 sender=me 的最后一条）。记忆为空时保守按敷衍处理。

要调宽松/严格，改 `_PREV_ASK_MARKERS`（问句标记）和 `_ACK_BARE_TEXTS`（肯定词集合）即可，别动转人工集合本身。

**判定口径：以 reply 内容为准，不以 should_reply 为准**
→ 实测结论（真实截图 + 真实 API）：
- 对方最后一条（含只有「。」「嗯嗯」）→ 模型 6/6 都主动给出了回复；
- 我方最后一条 → 模型 4/4 都输出空 reply，且 `last_message_sender=me`；
- 但也出现过一次「should_reply=false 却写了 reply」的矛盾输出。
因此 RPA 层按内容判定：`should_reply is not True` 但 `reply` 非空且末条是对方 → 直接放行发送，
日志打印 `[AI] 按 reply 为准: 模型已给出内容，放行发送`。
末条是我方（sender=me）时 reply 一律不发，避免自己接自己的话。

**reply_only_mode 全链路（`chat_strategy_config.json` 里 `"reply_only_mode": true`，改 false 回退旧口径）**
- `explicit_should_reply()` / `explicit_vision_should_reply()` 带 `reply_only_mode` 参数，true 时
  `should_reply = bool(reply.strip())`——只从内容派生，不再信模型的布尔。
- 提示词里【发送规则（最高优先）】写明「只看 reply 有没有内容」，`json_fields` 里 `should_reply`
  降级为「仅供参考」。
- `instruction_core` 的「末条是我方 reply 必须为空」按 `allow_proactive` 分支替换：主动模式下改为
  「即使末条是我方/窗口为空，也必须写出新内容」——否则主动阶梯会一直空转（实测踩过）。
- 两处硬闸门（`is_self_echo_last_text`、末条发送者=me）**只在被动模式生效**，主动模式放行。
  这是 reply 口径下唯一保留的确定性保护，因为实测模型会把右侧气泡误判成对方。
- 「敷衍/收尾」转人工在模型已写出内容时**不再转人工**（打印 `[联动] … 按 reply 为准继续发送`）；
  模型没写出内容时仍按原规则转人工。「达成转人工」「主动阶梯」「冲突熔断」不受影响——熔断读的是
  `local_sender`/`vision_sender` 与画面摘要，从来不看 `should_reply`。
- 代价（已确认接受）：对方只是陈述/收尾时 AI 也会回一句礼貌收尾，比旧口径更黏人。
- 回归脚本 `runtime/_regress_reply_only.py`：6 例真实 API，当前 6/6 PASS。

**对方说「别再发了」时的礼貌收尾**
→ 实测漏点（log_20261111_123245 第 6/7 轮）：对方发「你不要再给我发消息了」，模型按 reply 口径给空 reply，
程序一句话都不回，冷得像装死。现在的处理：
- `is_reject_stop_other_text()` 命中 `_REJECT_MARKERS`（不要再给我发/别发了/不感兴趣/别打扰我/烦我/拉黑/投诉…）→
  **强制先发一句礼貌收尾**：模型回复若短且像收尾（含「打扰/好的/明白/收到/理解/抱歉/祝」且 ≤40 字）就用它，
  否则用 `fallback_reject_reply()` 四句轮换池（避免被「已发过相同内容」拦掉）。
- 同时把该联系人加入 `_REJECTED_CONTACTS`，本进程内后续轮次直接跳过（日志 `[跳过] … 对方已表示不想再收到消息`），
  重启监控后自动清空。
- 提示词【发送规则】里也补了这一条：拒绝场景**不是沉默场景**，必须回一句体面收尾，不复述、不辩解、不追问、不再推销。

**AI 只发半句 / 消息被截断**
→ 根因是输出撞 `max_tokens` 上限：JSON 里的 reply 字符串被硬切，`_robust_json_parse` 又会「补引号」把它救成一个合法对象，
于是半句话（「你这边要是还没定下来，我」）被正常发出去，日志根本看不出错。
三层保护：
1. 视觉主路径 `max_tokens` = 1200（沿革：500 截断第二条 → 700 仍会截断 4 条长回复 → 1200）；
2. 提示词【长度硬约束】：reply 最多 3 条、每条不超过 20 字、不写长段落；
3. 发送前 `trim_incomplete_reply()`（在 `_send_split_messages` 里）：末条不以句末标点收尾且末字是
   「悬空字」（我/你/就/再/和/把/是/要…）→ 认定被截断，收到最后一个逗号处；连分句都没有就整条丢掉。
   `_call_api` 里遇到 `finish_reason=length` 会打印 `[API] 警告: 输出被 max_tokens 截断`，便于定位。
   注意判据要保守：只是没写句号的完整句（「我这就给你留一份」）不能误伤。

**HTTP 202 排队（平台高峰期）**
→ 平台会把请求压进队列并返回 202 `{"queued":true,"check_url":"/api/queue/status/req_..."}`。
以前这被当作不可重试错误 → `is_api_stop_error` 判致命 → 打印 `[停止] AI 服务调用失败（HTTP 202）`
→ 整轮监控退出。现在 `_call_api` 里先按 `check_url` 轮询（`_poll_queued_api_result`，最多 24 秒）
取回结果；取不到则按可重试处理（重试/换 fallback 模型），不会再停监控。

**截图疑似全黑，但程序照样正常回复**
→ 先看微信窗口在哪个屏：`x`/`y` 为负 = 被拖到了左侧副屏。
Pillow 的 `ImageGrab.grab(bbox=...)` **默认只抓主屏**，副屏坐标整块返回纯黑（实测 mean=0、var=0），
于是日志每轮都打「截图疑似全黑 → 强制唤醒 → 仍全黑 → PrintWindow 兜底」，
而 PrintWindow 不依赖屏幕坐标，照样拿到画面，所以 AI 回复一直是正常的。
修法：`_grab_screen_bbox()` 统一用 `all_screens=True` 抓取；窗口越界时日志打
`[截图] 微信窗口在副屏 (x=-2237, y=44)，按全屏模式抓取`。PrintWindow 保留为兜底，
但它的画面有概率缺侧栏/半渲染，能走真屏幕抓取就别走它。

**Web 控制台打不开 / 端口被占**
→ 改 `app.py` 顶部 `PORT = 8765`，或关掉占用该端口的进程。

**停止监控 / 重启后联系人回填**
- 「实时监控」和「聊天监控」两个页面头部都有黑底大号「■ 停止监控」按钮（运行中才可点），
  点击走 confirm，调 `POST /api/monitor/stop`；「聊天监控」页另有「↻ 重启监控生效」，改完名单直接重启。
- 监控进程启动时会写 `runtime/monitor.pid`。GUI 重启后 `_monitor_proc` 句柄丢失，
  `/api/status` 会回退读 pid 文件判断是否仍在运行（`_pid_alive` 用 OpenProcess+GetExitCodeProcess），
  停止按钮据此仍能把遗留进程 taskkill 掉；关闭窗口时 `gui.py` 也会清理遗留 pid。
- 技能配置弹窗的联系人**必须从 `my_contacts.json` 回填**，不能用 localStorage——
  pywebview 里 localStorage 重启即丢，之前就是靠它，导致每次重启弹窗都是空的。
- 弹窗确认后先 `stop` 再 `start`，否则 start 返回 already running，新名单/新开关不会生效；
  同时刷新「聊天监控」表格和策略页的主动开关，保证三处名单一致。
- `_activate_skill` 合并旧文件的 gender：弹窗只填备注名和目标，别把之前手填的性别抹成「未知」。
