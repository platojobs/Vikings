"""告警规则引擎：确定性阈值判定。

阈值全部从 Settings 读取，便于通过环境变量调整，不再硬编码。
规则与文案分离——本模块只负责「是否告警、告警级别、阈值」，
文案生成交给 app/ai/diagnosis.py，便于单独测试与调整。
"""

from typing import Dict, List

from app.config import get_settings


def _thresholds() -> Dict:
    """从配置动态取阈值，支持通过环境变量调整。"""
    s = get_settings()
    return {
        "voltage": {
            "warning": (s.threshold_voltage_warning, "lt"),
            "critical": (s.threshold_voltage_critical, "lt"),
        },
        "temperature": {
            "critical_high": (s.threshold_temperature_critical_high, "gt"),
            "warning_high": (s.threshold_temperature_warning_high, "gt"),
            "warning_low": (s.threshold_temperature_warning_low, "lt"),
            "critical_low": (s.threshold_temperature_critical_low, "lt"),
        },
        "humidity": {
            "warning": (s.threshold_humidity_warning, "gt"),
            "critical": (s.threshold_humidity_critical, "gt"),
        },
        "rssi": {
            "warning": (s.threshold_rssi_warning, "lt"),
            "critical": (s.threshold_rssi_critical, "lt"),
        },
    }


def thresholds_snapshot() -> Dict:
    """对外暴露当前生效阈值，供 /api/rules/thresholds 接口返回。"""
    return _thresholds()


def evaluate(report) -> List[Dict]:
    """对一条上报数据执行规则判定，返回命中的告警列表。

    返回元素结构：
        {"metric": "voltage", "level": "critical", "value": 2.91, "threshold": 3.2}
    critical 优先，命中 critical 后不再叠加同指标 warning。
    """
    t = _thresholds()
    hits: List[Dict] = []

    def add(metric: str, level: str, value: float, threshold: float):
        hits.append({"metric": metric, "level": level, "value": float(value), "threshold": float(threshold)})

    # ---- 电压 ----
    voltage = float(report.voltage)
    if voltage < t["voltage"]["critical"][0]:
        add("voltage", "critical", voltage, t["voltage"]["critical"][0])
    elif voltage < t["voltage"]["warning"][0]:
        add("voltage", "warning", voltage, t["voltage"]["warning"][0])

    # ---- 温度（双向）----
    temperature = float(report.temperature)
    if temperature > t["temperature"]["critical_high"][0]:
        add("temperature", "critical", temperature, t["temperature"]["critical_high"][0])
    elif temperature > t["temperature"]["warning_high"][0]:
        add("temperature", "warning", temperature, t["temperature"]["warning_high"][0])
    elif temperature < t["temperature"]["critical_low"][0]:
        add("temperature", "critical", temperature, t["temperature"]["critical_low"][0])
    elif temperature < t["temperature"]["warning_low"][0]:
        add("temperature", "warning", temperature, t["temperature"]["warning_low"][0])

    # ---- 湿度 ----
    humidity = float(report.humidity)
    if humidity > t["humidity"]["critical"][0]:
        add("humidity", "critical", humidity, t["humidity"]["critical"][0])
    elif humidity > t["humidity"]["warning"][0]:
        add("humidity", "warning", humidity, t["humidity"]["warning"][0])

    # ---- 信号强度 ----
    rssi = int(report.rssi)
    if rssi < t["rssi"]["critical"][0]:
        add("rssi", "critical", rssi, t["rssi"]["critical"][0])
    elif rssi < t["rssi"]["warning"][0]:
        add("rssi", "warning", rssi, t["rssi"]["warning"][0])

    return hits
