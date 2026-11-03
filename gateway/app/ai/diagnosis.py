"""AI 异常诊断：把规则引擎命中的数值，转成运维可执行的告警文案。

设计要点：
1. 规则负责"是否告警"（确定性、可测试），LLM 只负责"怎么描述、怎么处置"。
2. LLM 未配置 Key、超时、返回异常时，一律降级到内置模板，保证链路永不中断。
"""

import logging
from typing import Dict, Tuple

import httpx

from app.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

METRIC_LABELS = {
    "voltage": "电池电压",
    "temperature": "环境温度",
    "humidity": "环境湿度",
    "rssi": "信号强度",
}

METRIC_UNITS = {"voltage": "V", "temperature": "℃", "humidity": "%RH", "rssi": "dBm"}

# 兜底模板：LLM 不可用时的确定性文案，覆盖四类指标 × 两个级别
FALLBACK_TEMPLATES = {
    ("voltage", "critical"): "设备 {device_id} 电池电压 {value}V 已跌破安全阈值 {threshold}V，存在随时掉电中断上报的风险，建议立即安排现场更换电池并核查供电电路。",
    ("voltage", "warning"): "设备 {device_id} 电池电压 {value}V 低于预警阈值 {threshold}V，电量衰减偏快，建议纳入本周巡检计划并提前备料。",
    ("temperature", "critical"): "设备 {device_id} 温度 {value}℃ 已越过安全阈值 {threshold}℃，可能引发器件损坏或数据失真，请立即排查制冷/散热与安装位置。",
    ("temperature", "warning"): "设备 {device_id} 温度 {value}℃ 接近预警阈值 {threshold}℃，建议检查周边散热环境并持续观察趋势。",
    ("humidity", "critical"): "设备 {device_id} 湿度 {value}%RH 超过安全阈值 {threshold}%RH，存在凝露短路风险，请立即除湿并检查密封。",
    ("humidity", "warning"): "设备 {device_id} 湿度 {value}%RH 高于预警阈值 {threshold}%RH，建议加强通风除湿并关注是否持续上升。",
    ("rssi", "critical"): "设备 {device_id} 信号强度 {value}dBm 已低于可用阈值 {threshold}dBm，存在频繁掉线风险，建议调整天线位置或增设网关。",
    ("rssi", "warning"): "设备 {device_id} 信号强度 {value}dBm 弱于预警阈值 {threshold}dBm，建议关注链路稳定性并排查遮挡。",
}


def _fallback_text(device_id: str, hit: Dict) -> str:
    template = FALLBACK_TEMPLATES.get(
        (hit["metric"], hit["level"]),
        "设备 {device_id} 指标 {metric} 当前值 {value} 越过阈值 {threshold}，请尽快排查。",
    )
    return template.format(
        device_id=device_id,
        metric=METRIC_LABELS.get(hit["metric"], hit["metric"]),
        value=hit["value"],
        threshold=hit["threshold"],
    )


def _build_prompt(device_id: str, hit: Dict) -> str:
    label = METRIC_LABELS.get(hit["metric"], hit["metric"])
    unit = METRIC_UNITS.get(hit["metric"], "")
    return (
        "你是一名资深 IoT 运维工程师，负责智能硬件设备的告警处置。\n"
        f"设备编号：{device_id}\n"
        f"异常指标：{label}（{hit['metric']}）\n"
        f"当前值：{hit['value']}{unit}\n"
        f"告警阈值：{hit['threshold']}{unit}\n"
        f"告警级别：{'严重' if hit['level'] == 'critical' else '预警'}\n\n"
        "请用中文输出一段面向运维人员的告警说明：先说明风险后果，再给出 1~2 条具体可执行的处置动作。"
        "要求 80 字以内，不要使用 Markdown，不要寒暄，直接输出正文。"
    )


async def generate_alert_text(device_id: str, hit: Dict) -> Tuple[str, bool]:
    """生成告警文案。

    返回 (文案, 是否由 LLM 生成)。任何异常都会降级到模板文案。
    """
    if not settings.llm_api_key:
        return _fallback_text(device_id, hit), False

    url = settings.llm_base_url.rstrip("/") + "/chat/completions"
    headers = {
        "Authorization": "Bearer " + settings.llm_api_key,
        "Content-Type": "application/json",
    }
    body = {
        "model": settings.llm_model,
        "messages": [
            {"role": "system", "content": "你是 IoT 设备运维专家，只输出告警文案正文。"},
            {"role": "user", "content": _build_prompt(device_id, hit)},
        ],
        "temperature": 0.4,
        "max_tokens": 200,
    }

    try:
        async with httpx.AsyncClient(timeout=settings.llm_timeout_seconds) as client:
            resp = await client.post(url, headers=headers, json=body)
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"].strip()
            if content:
                return content, True
            logger.warning("LLM 返回空文案，降级为模板")
    except Exception as exc:  # 网络超时、鉴权失败、返回结构异常等统一降级
        logger.warning("LLM 调用失败，降级为模板文案: %s", exc)

    return _fallback_text(device_id, hit), False
