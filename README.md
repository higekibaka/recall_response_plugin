# 撤回消息响应插件

> 检测群聊中的消息撤回事件，有概率让麦麦发出一句调侃反应。

---

## 功能简介

当群内有人撤回消息时，麦麦会按照配置的概率"注意到"这件事，并以固定格式进行回应。

**响应格式：**
```
{撤回人名称}：{动作描述}（{状态后缀}地撤回）
```

状态后缀为"绝望"或"开心"，可配置比例。

**示例：**
- `空想之境：hide...（绝望地撤回）`
- `小明：好时代，来临了...（开心地撤回）`

---

## 两种工作模式

| 模式 | 触发条件 | 说明 |
|------|----------|------|
| **定型文模式**（推荐） | `fixed_responses` 非空 | 每次随机选一条，无需 LLM，稳定快速 |
| **LLM 模式** | `fixed_responses` 为空列表 | 通过 `generator_api` 用 replyer 模型动态生成 |

---

## 显示名称逻辑

- **本人撤回**：显示撤回者名称
- **管理员/群主撤回他人消息**：显示**被撤回的原发送者**名称，不显示管理员

---

## 工作流程

```
群聊有人撤回消息
    ↓
ON_MESSAGE_PRE_PROCESS 事件触发
    ↓
识别撤回事件 + 确定显示名称（recalled_user_info）
    ↓
概率抽签（默认 30%）   ← 先抽签，未命中不消耗冷却
    ↓
冷却检查（默认同群 60 秒）
    ↓
随机决定状态后缀（绝望/开心）
    ↓
fixed_responses 非空？
    ├── 是 → 随机抽取一条定型文
    └── 否 → generator_api.generate_response_custom 生成
    ↓
send_api.text_to_stream 发送
```

**全程异步，不阻塞正常消息处理流程。**

---

## 配置说明

配置文件路径：`plugins/recall_response_plugin/config.toml`

```toml
[plugin]
enabled = true                   # 总开关

[recall_response]
response_probability = 0.3       # 触发概率（0.0~1.0）
cooldown_seconds = 60            # 同群冷却时间（秒）
desperate_probability = 0.5      # 触发"绝望地撤回"的概率，剩余为"开心地撤回"

# 定型文列表（非空则使用定型文，空列表则使用 LLM）
fixed_responses = [
    "hide...",
    "好时代，来临了...",
    "撤回了不该说的话",
]

# LLM 模式风格指引（fixed_responses 非空时无效）
response_style = "用第三人称描述撤回人的动作或心态..."
```

| 参数 | 默认值 | 说明 |
|------|--------|------|
| `plugin.enabled` | `true` | 总开关 |
| `recall_response.response_probability` | `0.3` | 每次撤回触发响应的概率 |
| `recall_response.cooldown_seconds` | `60` | 同一个群两次响应的最短间隔（秒） |
| `recall_response.desperate_probability` | `0.5` | 状态后缀为「绝望」的概率 |
| `recall_response.fixed_responses` | `[]` | 定型文列表，非空时激活定型文模式 |
| `recall_response.response_style` | (见上) | LLM 模式风格指引 |

---

## WebUI 支持

插件已完整适配 MaiBot WebUI：
- **`_manifest.json`**：完整元数据，规范格式
- **`config_schema`**：所有字段含类型、默认值、说明，WebUI 自动渲染
  - `fixed_responses` 渲染为字符串列表编辑控件（`item_type: "string"`）
  - `response_style` 渲染为多行文本框

---

## 日志示例

```
[recall_response] 检测到撤回：小明（123456） 在群 88888888 撤回了消息
[recall_response] 使用定型文：'hide...'
[recall_response] ✅ 已响应撤回（群 88888888）："小明：hide...（绝望地撤回）"
[recall_response] 概率未命中（30%），本次不响应
[recall_response] 群 88888888 冷却中，剩余 43s，跳过
```

---

## 注意事项

- 只响应**群聊**撤回，私聊撤回不触发
- 不响应麦麦自身的撤回（避免死循环）
- 群从未发过消息时（ChatStream 未创建）会跳过

---

## 核心 API

| API | 用途 |
|-----|------|
| `chat_api.get_stream_by_group_id(group_id, platform)` | 获取群聊 ChatStream |
| `generator_api.generate_response_custom(chat_stream, prompt)` | LLM 模式动态生成 |
| `send_api.text_to_stream(text, stream_id)` | 发送纯文本消息 |
