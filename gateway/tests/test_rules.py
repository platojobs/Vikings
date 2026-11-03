"""规则引擎单测：覆盖电压/温度双向/湿度/信号四类指标的 warning 与 critical，
以及 rule_config 在线覆盖的生效优先级。"""

from types import SimpleNamespace

import pytest

from app import rules
from app.rules import evaluate


def make_report(**overrides):
    base = dict(temperature=25.0, humidity=55.0, voltage=3.7, rssi=-60)
    base.update(overrides)
    return SimpleNamespace(**base)


def test_normal_no_alert():
    assert evaluate(make_report()) == []


def test_voltage_warning():
    hits = evaluate(make_report(voltage=3.3))
    assert len(hits) == 1
    assert hits[0]["metric"] == "voltage"
    assert hits[0]["level"] == "warning"


def test_voltage_critical():
    hits = evaluate(make_report(voltage=2.9))
    assert hits[0]["level"] == "critical"
    # critical 不再叠加 warning
    assert len(hits) == 1


def test_temperature_high_critical():
    hits = evaluate(make_report(temperature=80))
    assert hits[0] == {"metric": "temperature", "level": "critical", "value": 80.0, "threshold": 75.0}


def test_temperature_low_warning():
    hits = evaluate(make_report(temperature=-15))
    assert hits[0]["metric"] == "temperature"
    assert hits[0]["level"] == "warning"


def test_humidity_warning():
    hits = evaluate(make_report(humidity=95))
    assert hits[0]["metric"] == "humidity"
    assert hits[0]["level"] == "warning"


def test_rssi_critical():
    hits = evaluate(make_report(rssi=-115))
    assert hits[0]["metric"] == "rssi"
    assert hits[0]["level"] == "critical"


def test_multiple_metrics():
    hits = evaluate(make_report(voltage=2.9, temperature=80))
    metrics = {h["metric"] for h in hits}
    assert metrics == {"voltage", "temperature"}


# ==================== 在线覆盖（rule_config → 进程内缓存） ====================


@pytest.fixture(autouse=False)
def clean_overrides():
    """每个覆盖用例前后清空进程内缓存，避免用例间串扰。"""
    rules.set_overrides({})
    yield
    rules.set_overrides({})


def test_override_takes_effect(clean_overrides):
    # 3.3V 默认触发 warning（阈值 3.4）
    assert evaluate(make_report(voltage=3.3))[0]["level"] == "warning"
    # 在线把 warning 阈值下调到 3.0 后，3.3V 不再告警
    rules.patch_override("threshold_voltage_warning", 3.0)
    assert evaluate(make_report(voltage=3.3)) == []


def test_override_stricter(clean_overrides):
    # 在线把 critical 阈值上调到 3.5，3.4V 直接命中 critical
    rules.patch_override("threshold_voltage_critical", 3.5)
    hits = evaluate(make_report(voltage=3.4))
    assert hits[0]["level"] == "critical"


def test_override_snapshot_and_clear(clean_overrides):
    rules.patch_override("threshold_humidity_warning", 80.0)
    assert rules.overrides_snapshot() == {"threshold_humidity_warning": 80.0}
    rules.clear_override("threshold_humidity_warning")
    assert rules.overrides_snapshot() == {}
