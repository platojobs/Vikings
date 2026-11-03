"""Topic 规范：三端唯一事实来源，与 docs/contracts.md 严格对应。

禁止在业务代码里手写 topic 字符串，一律通过本模块构造与解析，
这样 Topic 规则变更只需改一处。
"""

# ---- 通配订阅（网关侧使用）----
TOPIC_REPORT_WILDCARD = "device/+/sensor/report"
TOPIC_STATUS_WILDCARD = "device/+/status"
TOPIC_ACK_WILDCARD = "device/+/cmd/ack"

# ---- 精确 Topic 模板（发布侧使用）----
TOPIC_SENSOR_REPORT = "device/{device_id}/sensor/report"
TOPIC_STATUS = "device/{device_id}/status"
TOPIC_COMMAND = "device/{device_id}/cmd"
TOPIC_COMMAND_ACK = "device/{device_id}/cmd/ack"
TOPIC_SYSTEM_ALERT = "device/system/alert"

# ---- 业务枚举 ----
DEVICE_STATUS_ONLINE = "online"
DEVICE_STATUS_OFFLINE = "offline"

COMMAND_TYPES = {"calibrate", "ota", "reboot", "config"}
ACK_STATES = {"received", "progress", "success", "failed"}


def sensor_report_topic(device_id: str) -> str:
    return TOPIC_SENSOR_REPORT.format(device_id=device_id)


def status_topic(device_id: str) -> str:
    return TOPIC_STATUS.format(device_id=device_id)


def command_topic(device_id: str) -> str:
    return TOPIC_COMMAND.format(device_id=device_id)


def ack_topic(device_id: str) -> str:
    return TOPIC_COMMAND_ACK.format(device_id=device_id)


def parse_topic(topic: str):
    """解析设备 Topic，返回 (kind, device_id)。

    kind 取值：report / status / ack；非法 Topic 返回 (None, None)。
    只接受 device/{id}/{...} 形式，避免把 device/system/alert 误判为设备消息。
    """
    parts = topic.split("/")
    if len(parts) < 3 or parts[0] != "device":
        return None, None

    device_id = parts[1]
    if device_id == "system":
        # device/system/alert 是系统级 Topic，不属于任何具体设备
        return None, None

    if len(parts) == 4 and parts[2] == "sensor" and parts[3] == "report":
        return "report", device_id
    if len(parts) == 3 and parts[2] == "status":
        return "status", device_id
    if len(parts) == 4 and parts[2] == "cmd" and parts[3] == "ack":
        return "ack", device_id
    return None, None
