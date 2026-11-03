"""Pydantic 报文校验单测：验证字段范围、别名与时间转换。"""

import pytest

from app.schemas import (
    CommandAckMessage,
    DeviceStatusMessage,
    SensorReportMessage,
    to_utc_datetime,
)


class TestSensorReport:
    def test_valid(self):
        msg = SensorReportMessage.model_validate(
            {
                "deviceId": "dev-1",
                "temperature": 25,
                "humidity": 55,
                "voltage": 3.7,
                "rssi": -60,
                "battery": 80,
                "seq": 1,
                "ts": 1762137600000,
            }
        )
        assert msg.device_id == "dev-1"
        assert msg.ts.tzinfo is not None

    def test_temperature_out_of_range(self):
        with pytest.raises(Exception):
            SensorReportMessage.model_validate(
                {"deviceId": "dev-1", "temperature": 200, "humidity": 55, "voltage": 3.7, "rssi": -60, "battery": 80, "seq": 1, "ts": 0}
            )

    def test_voltage_out_of_range(self):
        with pytest.raises(Exception):
            SensorReportMessage.model_validate(
                {"deviceId": "dev-1", "temperature": 25, "humidity": 55, "voltage": 10, "rssi": -60, "battery": 80, "seq": 1, "ts": 0}
            )


class TestStatus:
    def test_online(self):
        msg = DeviceStatusMessage.model_validate(
            {"deviceId": "dev-1", "status": "online", "firmwareVersion": "1.0.0", "ts": 0}
        )
        assert msg.status == "online"

    def test_invalid_status(self):
        with pytest.raises(Exception):
            DeviceStatusMessage.model_validate({"deviceId": "dev-1", "status": "unknown", "ts": 0})


class TestAck:
    @pytest.mark.parametrize("state", ["received", "progress", "success", "failed"])
    def test_valid_states(self, state):
        msg = CommandAckMessage.model_validate(
            {"cmdId": "cmd-1", "type": "ota", "state": state, "ts": 0}
        )
        assert msg.state == state

    def test_progress_out_of_range(self):
        with pytest.raises(Exception):
            CommandAckMessage.model_validate(
                {"cmdId": "cmd-1", "type": "ota", "state": "progress", "progress": 150, "ts": 0}
            )


def test_to_utc_datetime_millis():
    dt = to_utc_datetime(1762137600000)
    assert dt.tzinfo is not None
    assert int(dt.timestamp() * 1000) == 1762137600000
