from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

# AstrBot 统一日志对象。
from astrbot.api import logger

from ..utils.config_parser import get_int_value

# 渲染层：把结构化数据转成可直接回复的文本。
from .message_renderer import render_dashboard_message_with_count

# 请求层：负责向 Live Dashboard 拉取原始状态数据。
from .payload_client import fetch_current_payload, fetch_health_records


def _parse_iso_datetime(value: str) -> datetime | None:
    """解析 ISO 时间，统一补齐时区信息。"""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _build_heart_rate_trend_payload(
    payload: dict[str, Any], health_records: list[dict[str, Any]], trend_window_minutes: int
) -> dict[str, dict[str, int]]:
    """按设备汇总指定时间窗内的心率趋势统计。"""
    server_time_text = str(payload.get("server_time") or "").strip()
    server_dt = _parse_iso_datetime(server_time_text) or datetime.now(timezone.utc)
    window_start = server_dt - timedelta(minutes=trend_window_minutes)

    trend_by_device: dict[str, dict[str, int]] = {}
    for record in health_records:
        if record.get("type") != "heart_rate":
            continue

        device_id = str(record.get("device_id") or "").strip()
        if not device_id:
            continue

        value = record.get("value")
        if not isinstance(value, (int, float)):
            continue

        recorded_at_text = str(record.get("recorded_at") or "").strip()
        recorded_dt = _parse_iso_datetime(recorded_at_text)
        if recorded_dt is None or recorded_dt < window_start or recorded_dt > server_dt:
            continue

        rounded_value = round(float(value))
        existing = trend_by_device.get(device_id)
        if existing is None:
            trend_by_device[device_id] = {
                "count": 1,
                "sum": rounded_value,
                "min": rounded_value,
                "max": rounded_value,
            }
            continue

        existing["count"] += 1
        existing["sum"] += rounded_value
        existing["min"] = min(existing["min"], rounded_value)
        existing["max"] = max(existing["max"], rounded_value)

    return trend_by_device


class DashboardService:
    """业务编排层：负责调用外部接口并渲染回复文本。"""

    _HEALTH_RECORD_CACHE_TTL_SECONDS = 30

    def __init__(self, config: dict[str, Any]):
        """保存插件配置，供后续请求和渲染阶段使用。"""
        self.config = config
        timeout_sec = get_int_value(
            config, "request_timeout_sec", 30, min_value=1, max_value=60
        )
        self._http_client = httpx.AsyncClient(timeout=timeout_sec)
        self._health_record_cache: dict[
            tuple[str, int], tuple[datetime, list[dict[str, Any]]]
        ] = {}

    async def _get_health_records_with_cache(
        self, date_text: str, tz_offset_minutes: int
    ) -> list[dict[str, Any]]:
        """读取健康记录，并在 30 秒内复用同日查询结果。"""
        cache_key = (date_text, tz_offset_minutes)
        now_dt = datetime.now(timezone.utc)
        expired_keys = [
            key
            for key, (cached_at, _) in self._health_record_cache.items()
            if (now_dt - cached_at).total_seconds() >= self._HEALTH_RECORD_CACHE_TTL_SECONDS
        ]
        for expired_key in expired_keys:
            self._health_record_cache.pop(expired_key, None)

        cached_entry = self._health_record_cache.get(cache_key)
        if cached_entry is not None:
            cached_at, cached_records = cached_entry
            cache_age = (now_dt - cached_at).total_seconds()
            if cache_age < self._HEALTH_RECORD_CACHE_TTL_SECONDS:
                logger.debug(
                    "[视奸面板] 命中健康数据缓存：date=%s tz=%s age=%.1fs",
                    date_text,
                    tz_offset_minutes,
                    cache_age,
                )
                return cached_records

        records = await fetch_health_records(
            self.config,
            date_text,
            tz_offset_minutes,
            client=self._http_client,
        )
        self._health_record_cache[cache_key] = (now_dt, records)
        return records

    async def close(self) -> None:
        """释放服务层资源。"""
        self._health_record_cache.clear()
        await self._http_client.aclose()
        logger.info("[视奸面板] HTTP 客户端已关闭")

    async def query_and_render(self) -> tuple[str, int]:
        """拉取实时状态并输出可发送文本与设备数量。"""
        # 读取基础地址（允许用户误填前后空格，因此先 strip）。
        base_url = str(self.config.get("base_url", "")).strip()
        # 地址未配置时直接返回可读提示，避免继续请求导致无意义异常。
        if not base_url:
            logger.warning("[视奸面板] 配置缺失：服务地址未填写")
            return "未配置 Live Dashboard 地址，请在插件配置中填写 base_url。", 0

        try:
            # 该日志用于调试阶段定位调用链路（默认 INFO 下不输出）。
            logger.debug("[视奸面板] 开始请求上游状态接口")

            # 从上游拉取当前状态 payload（dict）。
            payload = await fetch_current_payload(self.config, client=self._http_client)

            server_dt = _parse_iso_datetime(str(payload.get("server_time") or "").strip())
            if server_dt is None:
                server_dt = datetime.now(timezone.utc)

            trend_window_minutes = get_int_value(
                self.config,
                "heart_rate_trend_window_minutes",
                60,
                min_value=5,
                max_value=24 * 60,
            )

            tz_offset_minutes = (
                int(server_dt.utcoffset().total_seconds() // 60)
                if server_dt.utcoffset()
                else 0
            )

            query_dates = {server_dt.date().isoformat()}
            window_start = server_dt - timedelta(minutes=trend_window_minutes)
            query_dates.add(window_start.date().isoformat())

            health_records: list[dict[str, Any]] = []
            for date_text in sorted(query_dates):
                health_records.extend(
                    await self._get_health_records_with_cache(
                        date_text,
                        tz_offset_minutes,
                    )
                )

            payload["heart_rate_trend_window_minutes"] = trend_window_minutes
            payload["heart_rate_trend"] = _build_heart_rate_trend_payload(
                payload,
                health_records,
                trend_window_minutes,
            )

            # 仅用于调试观察：统计上游返回的设备数量。
            device_count = (
                len(payload.get("devices", []))
                if isinstance(payload.get("devices"), list)
                else 0
            )
            logger.debug("[视奸面板] 上游请求成功，设备数：%s", device_count)

            # 把上游数据按配置开关渲染成最终回复文本，并返回展示设备数。
            rendered_message, render_device_count = render_dashboard_message_with_count(
                payload,
                self.config,
            )

            # 记录最终输出长度，方便定位“回复过长/过短”的问题。
            logger.info(
                "[视奸面板] 文本渲染完成，回复字符数：%s, 展示设备数：%s",
                len(rendered_message),
                render_device_count,
            )
            return rendered_message, render_device_count

        except httpx.TimeoutException:
            # 超时通常是上游慢或网络抖动，提示用户稍后再试。
            logger.warning("[视奸面板] 请求超时：Live Dashboard 响应过慢")
            return "请求超时：Live Dashboard 响应过慢，请稍后重试。", 0

        except httpx.HTTPStatusError as exc:
            # HTTP 层已连通，但返回非 2xx 状态。
            status_code = exc.response.status_code
            logger.warning("[视奸面板] HTTP 状态异常，状态码：%s", status_code)

            # 401/403 常见于代理鉴权或 token 配置问题。
            if status_code in (401, 403):
                return (
                    "鉴权失败：请检查 auth_token 是否正确，或确认服务端是否允许访问 /api/current。",
                    0,
                )

            # 其他状态码统一提示。
            return f"请求失败：服务端返回 HTTP {status_code}。", 0

        except httpx.RequestError as exc:
            # 网络层错误（DNS、连接失败、证书等）。
            logger.warning("[视奸面板] 网络请求异常：%s", exc)
            return "网络错误：无法连接到 Live Dashboard 服务。", 0

        except Exception as exc:  # noqa: BLE001
            # 兜底异常，避免插件因未预期错误中断命令处理。
            logger.exception("[视奸面板] 未预期异常：%s", exc)
            return "发生未预期错误：请查看 AstrBot 日志。", 0
