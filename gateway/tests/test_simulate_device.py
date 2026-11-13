"""设备模拟器影子同步测试：不连接真实 MQTT Broker 或网关。"""

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "simulate_device.py"
SPEC = importlib.util.spec_from_file_location("simulate_device", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
simulate_device = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(simulate_device)


def _device(api_url: str = "http://gateway.local:8000"):
    return simulate_device.SimulatedDevice(
        SimpleNamespace(
            device_id="dev/0001",
            host="127.0.0.1",
            port=1883,
            username="dev/0001",
            password="mqtt-secret",
            count=1,
            interval=5,
            voltage=3.75,
            temperature=24.0,
            firmware="1.0.0",
            api_url=api_url,
            api_token="operator-token" if api_url else "",
            api_timeout=5.0,
        )
    )


def test_shadow_sync_disabled_without_api_url():
    device = _device(api_url="")
    device._api_request = Mock()

    asyncio.run(device._sync_shadow())

    device._api_request.assert_not_called()


def test_shadow_sync_applies_desired_and_reports_version():
    device = _device()
    device._api_request = Mock(
        side_effect=[
            {
                "desired": {"report_interval": 30, "led": True},
                "version": 7,
                "reported_version": 6,
                "diff_keys": ["report_interval"],
            },
            {"device_id": "dev/0001", "sync_status": "synced"},
        ]
    )

    asyncio.run(device._sync_shadow())

    assert device._api_request.call_args_list == [
        call("GET", "/api/devices/dev%2F0001/shadow"),
        call(
            "POST",
            "/api/devices/dev%2F0001/shadow/reported",
            {"reported": {"report_interval": 30, "led": True}, "version": 7},
        ),
    ]
    assert device.reported_state == {"report_interval": 30, "led": True}


def test_shadow_sync_clears_reported_keys_removed_by_exact_rollback():
    device = _device()
    device.reported_state = {"report_interval": 30, "led": True, "extra": "old"}
    device._api_request = Mock(
        side_effect=[
            {
                "desired": {"report_interval": 30, "led": True},
                "version": 8,
                "reported_version": 7,
                "diff_keys": ["extra"],
            },
            {"device_id": "dev/0001", "sync_status": "synced"},
        ]
    )

    asyncio.run(device._sync_shadow())

    assert device._api_request.call_args_list == [
        call("GET", "/api/devices/dev%2F0001/shadow"),
        call(
            "POST",
            "/api/devices/dev%2F0001/shadow/reported",
            {
                "reported": {"report_interval": 30, "led": True, "extra": None},
                "version": 8,
            },
        ),
    ]
    assert device.reported_state == {"report_interval": 30, "led": True}


def test_shadow_sync_skips_report_when_already_synced():
    device = _device()
    device._api_request = Mock(
        return_value={
            "desired": {"report_interval": 30},
            "version": 7,
            "reported_version": 7,
            "diff_keys": [],
        }
    )

    asyncio.run(device._sync_shadow())

    device._api_request.assert_called_once_with("GET", "/api/devices/dev%2F0001/shadow")
    assert device.reported_state == {}


def test_shadow_sync_failure_does_not_escape(capsys):
    device = _device()
    device._api_request = Mock(side_effect=RuntimeError("gateway unavailable"))

    asyncio.run(device._sync_shadow())

    assert "影子同步失败（继续本轮上报）" in capsys.readouterr().out
