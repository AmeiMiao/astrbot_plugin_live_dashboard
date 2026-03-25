"""AstrBot 插件入口文件。

维护约定：
- 本文件定义插件主类，供 AstrBot 扫描与加载。
- 业务细节仍下沉到 services / utils 层。
"""

from __future__ import annotations

from typing import Any

import astrbot.api.star as star
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Node, Nodes, Plain, Reply
from astrbot.api.provider import ProviderRequest

from .services.dashboard_service import DashboardService
from .utils.config_parser import get_text_value


def _split_message_blocks(message: str) -> list[str]:
    """把渲染文本按空行分段，用于构建转发节点。"""
    blocks = [block.strip() for block in message.split("\n\n") if block.strip()]
    return blocks if blocks else [message.strip()]


def _parse_list_config(raw_text: str) -> list[str]:
    """解析列表型配置（支持逗号/分号/换行分隔）。"""
    separators = [",", "，", ";", "；", "\n", "\r", "\t"]
    normalized = raw_text
    for separator in separators:
        normalized = normalized.replace(separator, ",")

    values: list[str] = []
    for part in normalized.split(","):
        value = part.strip()
        if value:
            values.append(value)

    return values


class LiveDashboardPlugin(star.Star):
    """Live Dashboard 插件主类（扫描入口 + 命令入口）。"""

    def __init__(self, context: star.Context, config: dict[str, Any] | None = None):
        # 初始化 Star 基类。
        super().__init__(context)

        # 插件配置（由 AstrBot 注入）。
        self.config = config or {}

        # 业务服务：负责请求与渲染。
        self.dashboard_service = DashboardService(self.config)

        logger.info(
            "[视奸面板] 插件初始化完成，base_url=%s, include_offline_devices=%s",
            str(self.config.get("base_url", "")).strip() or "<未配置>",
            self.config.get("include_offline_devices", False),
        )

    def _get_query_denied_text(self, event: AstrMessageEvent) -> str | None:
        """统一处理黑名单拦截，命中时返回拒绝文案。"""
        sender_id = str(event.get_sender_id() or "").strip()
        session_id = str(getattr(event.message_obj, "session_id", "") or "").strip()

        # 群/用户黑名单都来自配置文本，先统一解析成列表。
        group_blacklist = _parse_list_config(
            get_text_value(self.config, "group_blacklist_sessions", "")
        )
        user_blacklist = _parse_list_config(
            get_text_value(self.config, "user_blacklist_senders", "")
        )

        # 兼容两种群黑名单写法：完整 session_id 或仅群号（后缀匹配）。
        is_group_blocked = bool(session_id) and any(
            blocked == session_id or session_id.endswith(f":{blocked}")
            for blocked in group_blacklist
        )
        if is_group_blocked:
            logger.info("[视奸面板] 群组黑名单命中，拒绝查询：session=%s", session_id)
            return "该群组已禁用状态查询喵。"

        is_user_blocked = bool(sender_id) and any(
            blocked == sender_id for blocked in user_blacklist
        )
        if is_user_blocked:
            logger.info("[视奸面板] 用户黑名单命中，拒绝查询：sender=%s", sender_id)
            return "你已被禁止使用该查询喵。"

        return None

    async def _query_dashboard_message(self) -> tuple[str, int]:
        """复用核心查询逻辑，返回渲染文本与展示设备数量。"""
        message, render_device_count = await self.dashboard_service.query_and_render()
        logger.info(
            "[视奸面板] 状态查询完成，reply_chars=%s, render_devices=%s",
            len(message),
            render_device_count,
        )
        return message, render_device_count

    @filter.on_llm_request()
    async def inject_live_dashboard_tool_prompt(
        self, event: AstrMessageEvent, req: ProviderRequest
    ):
        """在 LLM 请求前注入工具使用提示，提升自动调用命中率。"""
        # 该段会直接拼接到 system_prompt，作为工具调用策略提示。
        instruction = (
            "\n\n[Live Dashboard (视奸面板) 工具使用规范]\n"
            "- 你可调用工具 query_live_dashboard_status 获取用户的实时设备状态。\n"
            "- 当用户询问“来视奸我/我现在在干嘛/帮我视奸一下他在干什么/设备在线情况/状态面板/最近在用什么应用”等涉及查询用户设备实时状态的问题时，应优先调用该工具后再回答。\n"
            "- 该工具无需参数；返回值是最新状态文本或失败原因。\n"
            "- 若返回的是权限或配置错误提示，请向用户明确说明原因并给出简短建议，不要编造实时数据。\n"
            "- 获得工具结果后，请按你的人设组织回复。\n"
        )
        req.system_prompt = (req.system_prompt or "") + instruction

    @filter.llm_tool(name="query_live_dashboard_status")
    async def query_live_dashboard_status_tool(self, event: AstrMessageEvent) -> str:
        """查询 Live Dashboard 实时设备状态，供 LLM 在对话中自动调用。

        Args:
            event(object): AstrBot 消息事件上下文（由框架注入）。
        """
        sender_id = str(event.get_sender_id() or "").strip()
        session_id = str(getattr(event.message_obj, "session_id", "") or "").strip()
        logger.info(
            "[视奸面板] LLM 工具触发状态查询，sender=%s, session=%s",
            sender_id or "unknown",
            session_id or "unknown",
        )

        # 与命令路径保持一致：先做权限与黑名单拦截，再发起查询。
        denied_text = self._get_query_denied_text(event)
        if denied_text:
            return denied_text

        message, render_device_count = await self._query_dashboard_message()

        # 识别服务层的已知错误前缀，避免把失败结果包装成“查询成功”。
        known_error_prefixes = (
            "未配置 Live Dashboard 地址",
            "请求超时：",
            "鉴权失败：",
            "请求失败：",
            "网络错误：",
            "发生未预期错误：",
        )
        if message.startswith(known_error_prefixes):
            return f"实时状态查询失败：{message}"

        return (
            f"实时状态查询成功，当前展示设备数：{render_device_count}。\n"
            "以下为状态面板原始文本：\n"
            f"{message}"
        )

    @filter.command("视奸", alias={"live", "dashboard", "设备状态", "状态面板"})
    async def query_live_dashboard(self, event: AstrMessageEvent):
        """状态查询命令处理器。"""
        sender_id = str(event.get_sender_id() or "").strip()
        session_id = str(getattr(event.message_obj, "session_id", "") or "").strip()

        logger.info(
            "[视奸面板] 收到状态查询指令，sender=%s, session=%s",
            sender_id or "unknown",
            session_id or "unknown",
        )

        denied_text = self._get_query_denied_text(event)
        if denied_text:
            yield event.chain_result(
                [
                    Reply(id=event.message_obj.message_id),
                    Plain(text=denied_text),
                ]
            )
            return

        message, render_device_count = await self._query_dashboard_message()

        # 仅在 aiocqhttp 且设备较多时启用合并转发，减少刷屏。
        use_forward_mode = (
            render_device_count >= 2 and event.get_platform_name() == "aiocqhttp"
        )

        # 设备较少：直接引用 + 全量文本。
        if not use_forward_mode:
            yield event.chain_result(
                [Reply(id=event.message_obj.message_id), Plain(text=message)]
            )
            return

        # 设备较多：仅发送一条合并转发；在首节点里包含引用消息，避免额外刷屏。
        blocks = _split_message_blocks(message)
        node_name = "Live Dashboard"
        node_uin = event.get_self_id() or "0"

        first_block = blocks[0] if blocks else message
        forward_nodes: list[Node] = [
            Node(
                uin=node_uin,
                name=node_name,
                content=[
                    Reply(id=event.message_obj.message_id),
                    Plain(text=first_block),
                ],
            )
        ]

        for block in blocks[1:]:
            forward_nodes.append(
                Node(
                    uin=node_uin,
                    name=node_name,
                    content=[Plain(text=block)],
                )
            )

        yield event.chain_result([Nodes(nodes=forward_nodes)])

    async def terminate(self):
        """插件停用/卸载时的资源释放入口。"""
        logger.info("[视奸面板] 正在停止视奸面板插件...")
        try:
            await self.dashboard_service.close()
        except Exception as exc:  # noqa: BLE001
            logger.exception("[视奸面板] 资源清理阶段出现异常：%s", exc)
        finally:
            logger.info("[视奸面板] 视奸面板插件已停止")


__all__ = ["LiveDashboardPlugin"]
