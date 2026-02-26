# 撤回消息响应插件

> 检测群聊中的消息撤回事件，有概率让麦麦发出一句符合人设的调侃反应。 

---

## 功能简介

当群内有人撤回消息时，麦麦会按照配置的概率"注意到"这件事，通过官方 `generator_api`（使用麦麦自身的 replyer 模型）生成对撤回者的「动作或心态揣测」，以固定格式发回群里。

**响应格式：**
```
{撤回人显示名称}：{符合人设的动作描述，约十字}（{状态后缀}地撤回）
```
状态后缀为"绝望"或"开心"，可通过配置比例控制。

**示例：**
- `空想之境：发现自己暴露了XP（绝望地撤回）`
- `小明：默默撤回了刚才的暴言（开心地撤回）`

---

## 工作原理

```
群聊有人撤回消息
    ↓
ON_MESSAGE_PRE_PROCESS 事件触发
    ↓
检测撤回事件消息段
    ↓
概率抽签（默认 30%）             ← 先抽签，未命中不消耗冷却
    ↓
冷却检查（默认同群 60 秒）
    ↓
随机决定状态后缀（绝望/开心）
    ↓
chat_api.get_stream_by_group_id → 获取群聊 ChatStream
    ↓
generator_api.generate_response_custom → 用 replyer 模型生成动作描述
    ↓
拼装格式并使用 send_api.text_to_stream 发送
```

**全程异步，不阻塞麦麦的正常消息处理流程。**

---

## 文件结构

```
plugins/recall_response_plugin/
├── plugin.py         ← 核心实现
├── config.toml       ← 配置文件（可调参数）
├── _manifest.json    ← 插件元数据（WebUI 展示）
├── LICENSE           ← MIT 许可证
└── README.md         ← 本文件
```

---

## 配置说明

配置文件路径：`plugins/recall_response_plugin/config.toml`

```toml
[plugin]
enabled = true                   # 总开关

[recall_response]
response_probability = 0.3       # 触发概率（0.0~1.0）
cooldown_seconds = 60            # 同群冷却时间（秒）
desperate_probability = 0.5      # 触发"绝望地撤回"的概率（0.0~1.0），剩余为"开心地撤回"
response_style = "用第三人称描述撤回人的动作或心态，内容可以是吐槽对方发错群、暴露XP、说了蠢话等"
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `plugin.enabled` | `true` | 总开关，`false` 时插件完全不响应 |
| `recall_response.response_probability` | `0.3` | 每次撤回触发响应的概率，建议 0.2~0.5 |
| `recall_response.cooldown_seconds` | `60` | 同一个群两次响应的最短间隔（秒），建议 30~120 |
| `recall_response.desperate_probability` | `0.5` | 状态后缀为「绝望」的概率，剩余为「开心」 |
| `recall_response.response_style` | (见上) | 描述风格指引，直接嵌入 LLM 提示词 |

> **调参建议**  
> 活跃：`probability = 0.5`，`cooldown = 30`  
> 安静：`probability = 0.15`，`cooldown = 120`  
> 临时关闭：`plugin.enabled = false`

---

## WebUI 支持

插件已完整适配 MaiBot WebUI，包括：
- `_manifest.json`：完整元数据，支持在插件市场展示
- `config_schema`：每个配置项含类型、默认值、说明，WebUI 自动渲染为表单

---

## 使用说明

1. 将插件目录放置于 `plugins/recall_response_plugin/`
2. 启动/重启 MaiBot
3. 在日志中搜索 `recall_response` 实时查看事件记录

**日志示例：**
```
[recall_response] 检测到撤回：小明（123456） 在群 88888888 撤回了消息
[recall_response] ✅ 已响应撤回（群 88888888）："小明：发现自己发错了表情包（绝望地撤回）"
[recall_response] 概率未命中（30%），本次不响应
[recall_response] 群 88888888 冷却中，剩余 43s，跳过
```

---

## 注意事项

- **只响应群聊**：私聊撤回不触发
- **不响应自身撤回**：避免死循环
- **依赖聊天流初始化**：群从未发过消息时（stream 未创建）会跳过
- **使用 replyer 模型**：动作描述由 `generator_api` 驱动，使用与麦麦正常回复相同的模型和风格

---

## 实现参考

| 组件 | 类型 | 说明 |
|------|------|------|
| `RecallEventHandler` | `BaseEventHandler` | 订阅 `ON_MESSAGE_PRE_PROCESS`，检测 recall 并异步触发响应 |
| `RecallResponsePlugin` | `BasePlugin` | 插件注册类，管理配置和组件列表 |

**核心 API：**
- `chat_api.get_stream_by_group_id(group_id, platform)` — 获取群聊 ChatStream（官方封装）
- `generator_api.generate_response_custom(chat_stream, prompt)` — 用 replyer 模型生成回应
- `send_api.text_to_stream(text, stream_id)` — 发送纯文本消息
