"""告警规则引擎：确定性阈值判定。

规则与文案分离——本模块只负责「是否告警、告警级别、阈值」，
文案生成交给 app/ai/diagnosis.py，便于单独测试与调整。
"""

from typing import Dict, List

# 阈值表（与 docs/contracts.md 第 5 节一致）
# 结构：指标 -> 级别 -> (阈值, 比较方向)
THRESHOLDS = {
    "voltage": {"critical": (3.2, "lt"), "warning": (3.4, "lt")},
    "temperature": {"critical": (75.0, "gt_high"), "warning": (60.0, "gt_low")},
    "humidity": {"critical": (97.0, "gt"), "warning": (90.0, "gt")},
    "rssi": {"critical": (-110, "lt"), "warning": (-100, "lt")},
}

# 温度是双向指标（过热 / 过冷都异常），单独定义上下限
TEMPERATURE_LOW = {"critical": -20.0, "warning": -10.0}


def evaluate(report) -> List[Dict]:
    """对一条上报数据执行规则判定，返回命中的告警列表。

    返回元素结构：
        {"metric": "voltage", "level": "critical", "value": 2.91, "threshold": 3.2}
    critical 优先，命中 critical 后不再叠加同指标 warning。
    """
    hits: List[Dict] = []

    def add(metric: str, level: str, value: float, threshold: float):
        hits.append({"metric": metric, "level": level, "value": float(value), "threshold": float(threshold)})

    # ---- 电压 ----
    voltage = float(report.voltage)
    if voltage < THRESHOLDS["voltage"]["critical"][0]:
        add("voltage", "critical", voltage, THRESHOLDS["voltage"]["critical"][0])
    elif voltage < THRESHOLDS["voltage"]["warning"][0]:
        add("voltage", "warning", voltage, THRESHOLDS["voltage"]["warning"][0])

    # ---- 温度（双向）----
    temperature = float(report.temperature)
    if temperature > THRESHOLDS["temperature"]["critical"][0]:
        add("temperature", "critical", temperature, THRESHOLDS["temperature"]["critical"][0])
    elif temperature > THRESHOLDS["temperature"]["warning"][0]:
        add("temperature", "warning", temperature, THRESHOLDS["temperature"]["warning"][0])
    elif temperature < TEMPERATURE_LOW["critical"]:
        add("temperature", "critical", temperature, TEMPERATURE_LOW["critical"])
    elif temperature < TEMPERATURE_LOW["warning"]:
        add("temperature", "warning", temperature, TEMPERATURE_LOW["warning"])

    # ---- 湿度 ----
    humidity = float(report.humidity)
    if humidity > THRESHOLDS["humidity"]["critical"][0]:
        add("humidity", "critical", humidity, THRESHOLDS["humidity"]["critical"][0])
    elif humidity > THRESHOLDS["humidity"]["warning"][0]:
        add("humidity", "warning", humidity, THRESHOLDS["humidity"]["warning"][0])

    # ---- 信号强度 ----
    rssi = int(report.rssi)
    if rssi < THRESHOLDS["rssi"]["critical"][0]:
        add("rssi", "critical", rssi, THRESHOLDS["rssi"]["critical"][0])
    elif rssi < THRESHOLDS["rssi"]["warning"][0]:
        add("rssi", "warning", rssi, THRESHOLDS["rssi"]["warning"][0])

    return hits
