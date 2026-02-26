"""
撤回消息响应插件 (recall_response_plugin)
==========================================

功能：
    监听群聊中的消息撤回事件，按照概率格式化输出对撤回事件的调侃反应。
    反应格式固定为：{撤回人名称}：{十个字左右的动作描述}（{绝望/开心}地撤回）
    其中"动作描述"由 LLM 通过官方 generator_api 结合人设和群聊上下文生成。

工作原理：
    1. 通过 ON_MESSAGE_PRE_PROCESS 事件钩子，拦截所有进入系统的消息（含 notice 类型）
    2. 检测消息段（message_segments）中类型为 "notify"、sub_type 为 "recall" 的段
    3. 满足条件后：概率抽签 → 冷却检查 → 根据概率确定状态后缀 → 调用 generator_api 生成动作描述 → 拼接格式发送
    4. 全程异步执行，不阻塞消息的正常处理流程

注意：
    - 本插件 **不修改任何 core 代码**，完全通过插件系统实现
    - 只响应群聊撤回（私聊撤回不处理）
    - 对麦麦自身的撤回不响应，避免死循环
"""

import asyncio
import random
import re
import time
from typing import List, Optional, Tuple, Type

from src.common.logger import get_logger
from src.plugin_system import (
    BaseEventHandler,
    BasePlugin,
    ConfigField,
    EventType,
    MaiMessages,
    send_api,
    register_plugin,
)
from src.plugin_system.apis import chat_api, generator_api
from src.plugin_system.base.component_types import ComponentInfo
from src.config.config import global_config

logger = get_logger("recall_response_plugin")

# ─────────────────────────────────────────────
# 模块级冷却记录表
# key:   群的 group_id (str)
# value: 上次成功响应的时间戳 (float, UNIX time)
# ─────────────────────────────────────────────
_last_response_time: dict[str, float] = {}

# 文本清洗用正则：移除括号包裹的注释或情绪标注
_BRACKET_RE = re.compile(r'（[^）]*）|\([^)]*\)|\[[^\]]*\]|【[^】]*】')


class RecallEventHandler(BaseEventHandler):
    """
    撤回事件处理器

    订阅 ON_MESSAGE_PRE_PROCESS 事件，检测群聊撤回通知。
    该事件在消息正式进入 HeartFlow 之前触发，能捕获包括 notice 在内的所有原始消息。

    Attributes:
        event_type:          订阅的事件类型
        handler_name:        处理器唯一标识
        handler_description: 处理器功能描述（显示在 WebUI）
        intercept_message:   是否拦截消息（False = 异步旁观，不影响后续处理）
    """

    event_type = EventType.ON_MESSAGE_PRE_PROCESS
    handler_name = "recall_response_handler"
    handler_description = "检测群聊撤回事件，有概率用符合人设的语气发出反应"
    intercept_message = False  # 异步模式：只旁观，不拦截后续消息处理

    async def execute(
        self, message: MaiMessages | None
    ) -> Tuple[bool, bool, Optional[str], None, None]:
        """
        事件处理入口

        框架约定返回值：(continue_flag, success, log_message, custom_result, modified_message)
        - continue_flag: True = 允许后续处理器继续执行
        - success:       True = 本处理器执行成功
        """
        # 消息为空时直接放行
        if not message:
            return True, True, None, None, None

        # 只处理群聊消息（私聊撤回不响应）
        if not message.is_group_message:
            return True, True, None, None, None

        # 尝试提取撤回信息；如果不是撤回事件则返回 None
        recall_info = self._extract_recall_info(message)
        if not recall_info:
            return True, True, None, None, None

        operator_name, operator_id, group_id, platform = recall_info

        # 过滤麦麦自身的撤回，避免死循环
        bot_id = str(global_config.bot.qq_account)
        if operator_id == bot_id:
            return True, True, None, None, None

        logger.info(
            f"[recall_response] 检测到撤回：{operator_name}（{operator_id}）"
            f" 在群 {group_id} 撤回了消息"
        )

        # 异步触发响应逻辑，不阻塞当前消息处理流程
        asyncio.create_task(
            self._handle_recall_response(
                operator_name=operator_name,
                group_id=group_id,
                platform=platform,
            )
        )

        return True, True, "检测到撤回事件，已异步触发响应", None, None

    # ─────────────────────────────────────────────
    # 私有方法
    # ─────────────────────────────────────────────

    def _extract_recall_info(
        self, message: MaiMessages
    ) -> Optional[Tuple[str, str, str, str]]:
        """
        从 MaiMessages 对象中提取撤回事件信息

        QQ 的撤回通知经过 maim_message 协议转换后，结构如下：
            message.message_segments[0].type = "notify"
            message.message_segments[0].data = {
                "sub_type": "group_recall",   # Napcat 发来的实际字段
                "message_id": "<被撤回的消息 ID>",
                "recalled_user_info": { "user_id": ..., "user_nickname": ... }  # 可能为空
            }
        操作者（谁撤回了消息）信息在 message.message_base_info 中。

        Returns:
            (operator_name, operator_id, group_id, platform) 四元组，若非撤回事件则返回 None
        """
        segs = message.message_segments
        if not segs:
            return None

        seg = segs[0]

        # 检查是否为 notify 类型的消息段
        if getattr(seg, "type", None) != "notify":
            return None

        # 检查 sub_type 是否为撤回事件
        # Napcat 发来的撤回事件 sub_type 为 "group_recall"（而非通用的 "recall"）
        data = getattr(seg, "data", None)
        if not isinstance(data, dict):
            return None
        sub_type = data.get("sub_type", "")
        if sub_type not in ("recall", "group_recall"):
            return None

        # 从基础信息中提取操作者信息（优先显示群名片，其次昵称，最后 ID）
        base_info = message.message_base_info
        operator_name: str = (
            base_info.get("user_cardname")
            or base_info.get("user_nickname")
            or str(base_info.get("user_id", "某人"))
        )
        operator_id: str = str(base_info.get("user_id", ""))
        group_id: str = str(base_info.get("group_id", ""))
        platform: str = str(base_info.get("platform", "qq"))

        # group_id 为空则无法后续处理，跳过
        if not group_id:
            return None

        return operator_name, operator_id, group_id, platform

    async def _handle_recall_response(
        self,
        operator_name: str,
        group_id: str,
        platform: str,
    ) -> None:
        """
        实际的响应处理逻辑（异步执行）

        按顺序执行：
            1. 概率抽签（先于冷却，未命中就不消耗冷却）
            2. 冷却检查（防止同群短时间内多次响应）
            3. 锁定冷却时间戳
            4. 通过官方 chat_api 获取该群的 ChatStream / stream_id
            5. 随机确定状态后缀（绝望/开心）
            6. 通过 generator_api 生成动作描述
            7. 拼装格式字符串并发送
        """
        try:
            # ── 读取配置 ───────────────────────────────
            response_probability: float = self.get_config(
                "recall_response.response_probability", 0.3
            )
            cooldown_seconds: int = self.get_config(
                "recall_response.cooldown_seconds", 60
            )

            # ── 概率抽签（先于冷却，未触发时不消耗冷却次数）──
            if random.random() > response_probability:
                logger.debug(
                    f"[recall_response] 概率未命中（{response_probability:.0%}），本次不响应"
                )
                return

            # ── 冷却检查 ─────────────────────────────────
            now = time.time()
            last_time = _last_response_time.get(group_id, 0.0)
            remaining = cooldown_seconds - (now - last_time)
            if remaining > 0:
                logger.debug(
                    f"[recall_response] 群 {group_id} 冷却中，剩余 {remaining:.0f}s，跳过"
                )
                return

            # 立即锁定冷却：无论后续是否发送成功，本次触发都消耗一次冷却
            _last_response_time[group_id] = now

            # ── 获取聊天流 & stream_id（官方 chat_api）──────
            # chat_api.get_stream_by_group_id 封装了底层遍历逻辑，有日志和错误处理
            chat_stream = chat_api.get_stream_by_group_id(group_id, platform)
            if not chat_stream:
                logger.warning(
                    f"[recall_response] 找不到群 {group_id} 的聊天流，跳过"
                    "（可能该群从未发过消息，或 MaiBot 尚未加载该群的聊天流）"
                )
                return

            stream_id = chat_stream.stream_id

            # ── 状态后缀确定 ──────────────────────────
            desperate_prob: float = self.get_config(
                "recall_response.desperate_probability", 0.5
            )
            status_suffix = "绝望" if random.random() < desperate_prob else "开心"

            # ── 生成动作描述（官方 generator_api）──────────
            # generate_response_custom 使用麦麦自身的 replyer 模型生成符合人设的文字
            action_desc = await self._generate_response(
                operator_name=operator_name,
                chat_stream=chat_stream,
            )
            if not action_desc:
                logger.debug("[recall_response] LLM 未生成有效内容，跳过本次响应")
                return

            response_text = f"{operator_name}：{action_desc}（{status_suffix}地撤回）"

            # ── 发送消息（纯文本）─────────────────────────
            success = await send_api.text_to_stream(response_text, stream_id)

            if success:
                logger.info(
                    f"[recall_response] ✅ 已响应撤回（群 {group_id}）：{response_text!r}"
                )
            else:
                # 发送失败则重置冷却（允许下次撤回重试）
                _last_response_time.pop(group_id, None)
                logger.warning(
                    f"[recall_response] ❌ 消息发送失败（群 {group_id}）"
                )

        except Exception as e:
            logger.error(f"[recall_response] 处理撤回响应时发生异常：{e}", exc_info=True)

    async def _generate_response(
        self,
        operator_name: str,
        chat_stream,  # ChatStream
    ) -> Optional[str]:
        """
        调用 generator_api 生成符合人设的撤回动作描述

        流程：
            1. 构造 prompt，描述撤回事件和要求的输出格式
            2. 通过 generator_api.generate_response_custom 用 replyer 模型生成
               （replyer 模型天生携带麦麦人设和群聊上下文，无需手动注入）
            3. 对输出做简单清洗（去除引号、括号注释，截取第一句）

        Args:
            operator_name: 执行撤回操作的用户名
            chat_stream:   对应群的 ChatStream 对象（携带上下文）

        Returns:
            生成的动作描述字符串，生成失败则返回 None
        """
        try:
            response_style: str = self.get_config(
                "recall_response.response_style",
                "用第三人称描述撤回人的动作或心态，内容可以是吐槽对方发错群、暴露XP、说了蠢话等",
            )

            # ── 构造 Prompt ───────────────────────────────
            # generator_api 的 replyer 模型会自动携带麦麦人设和聊天记录，
            # 这里 prompt 只需要描述本次任务指令即可
            prompt = f"""群里 {operator_name} 刚刚撤回了一条消息。

请以你的视角和人设脑回路，想象或揣测【{operator_name}】撤回这条消息的原因（TA做出了什么动作或处于什么心态）。
生成一句约十个字的【动作描述】。

要求：
- 只输出对撤回人行为/心态的描述，不加任何前缀、序号、标点或括号
- 描述风格参考：{response_style}
- 约8-15个汉字长度，例如：发现自己暴露了XP / 撤回了刚才的暴言 / 肯定又发错群了"""

            # ── 调用 generator_api ──────────────────────
            response = await generator_api.generate_response_custom(
                chat_stream=chat_stream,
                request_type="plugin.recall_response",
                prompt=prompt,
            )

            if not response:
                logger.warning("[recall_response] generator_api 返回空内容")
                return None

            # ── 清洗输出 ──────────────────────────────
            response = response.strip().strip('"\'"「」')

            # 移除模型附带的情绪标注括号（如"（笑）"）
            response = _BRACKET_RE.sub("", response).strip()

            # 若模型输出了多句话，只取第一句
            if len(response) > 50:
                for sep in ["。", "！", "？", "…", "\n", "，", ","]:
                    idx = response.find(sep)
                    if idx != -1:
                        response = response[:idx]
                        break
                else:
                    response = response[:20]  # 兜底截断

            return response if response else None

        except Exception as e:
            logger.error(f"[recall_response] LLM 生成出错：{e}", exc_info=True)
            return None


# ═══════════════════════════════════════════════
# 插件主类注册
# ═══════════════════════════════════════════════


@register_plugin
class RecallResponsePlugin(BasePlugin):
    """
    撤回消息响应插件

    通过 EventHandler 机制监听群聊撤回事件，
    按概率让麦麦发出符合人设的一句反应。
    完全无侵入，不修改任何 core 代码。
    """

    # ── 插件基础信息 ──────────────────────────────
    plugin_name: str = "recall_response_plugin"   # 全局唯一标识符
    enable_plugin: bool = True                     # 是否启用
    dependencies: List[str] = []                  # 依赖的其他插件（无）
    python_dependencies: List[str] = []           # 依赖的 Python 包（无）
    config_file_name: str = "config.toml"         # 配置文件路径（相对插件目录）

    # ── WebUI 配置节描述 ──────────────────────────
    config_section_descriptions = {
        "plugin": "插件基础设置",
        "recall_response": "撤回响应行为设置 · 控制麦麦对撤回消息的响应概率与冷却时间",
    }

    # ── 配置 Schema（WebUI 自动渲染表单用）────────
    config_schema = {
        "plugin": {
            "enabled": ConfigField(
                type=bool,
                default=True,
                description="总开关：关闭后插件完全不响应任何撤回事件",
            ),
        },
        "recall_response": {
            "response_probability": ConfigField(
                type=float,
                default=0.3,
                description="每次撤回事件触发响应的概率。0.0 = 永不响应，1.0 = 每次必响应，建议填 0.2~0.5",
            ),
            "cooldown_seconds": ConfigField(
                type=int,
                default=60,
                description="同一个群两次响应之间的最短冷却时间（秒）。设置较大的值可防止刷屏，建议 30~120",
            ),
            "desperate_probability": ConfigField(
                type=float,
                default=0.5,
                description="设置撤回状态后缀为「绝望」的概率（0.0~1.0）。剩余概率触发「开心」。例如 0.5 表示各占一半。",
            ),
            "response_style": ConfigField(
                type=str,
                input_type="textarea",
                default="用第三人称描述撤回人的动作或心态，内容可以是吐槽对方发错群、暴露XP、说了蠢话等",
                description="描述撤回人行为风格指引，直接嵌入 LLM 提示词。",
            ),
        },
    }

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        """
        返回本插件注册的组件列表

        只在插件启用时注册 RecallEventHandler，
        禁用时返回空列表，EventHandler 不会被加载。
        """
        if not self.config.get("plugin", {}).get("enabled", True):
            logger.info("[recall_response_plugin] 插件已禁用，跳过组件注册")
            return []
        return [
            (RecallEventHandler.get_handler_info(), RecallEventHandler),
        ]
