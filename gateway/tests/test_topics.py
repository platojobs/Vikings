"""Topic 解析单测：验证设备 Topic 解析与非法 Topic 过滤。"""

import pytest

from app.topics import (
    ack_topic,
    command_topic,
    parse_topic,
    sensor_report_topic,
    status_topic,
)


def test_topic_builders():
    assert sensor_report_topic("dev-1") == "device/dev-1/sensor/report"
    assert status_topic("dev-1") == "device/dev-1/status"
    assert command_topic("dev-1") == "device/dev-1/cmd"
    assert ack_topic("dev-1") == "device/dev-1/cmd/ack"


@pytest.mark.parametrize(
    "topic, expected",
    [
        ("device/dev-1/sensor/report", ("report", "dev-1")),
        ("device/dev-1/status", ("status", "dev-1")),
        ("device/dev-1/cmd/ack", ("ack", "dev-1")),
        # 系统级告警 Topic 不应被解析为设备消息
        ("device/system/alert", (None, None)),
        # 非法结构
        ("device/dev-1", (None, None)),
        ("other/dev-1/status", (None, None)),
        ("device/dev-1/cmd", (None, None)),
    ],
)
def test_parse_topic(topic, expected):
    assert parse_topic(topic) == expected
