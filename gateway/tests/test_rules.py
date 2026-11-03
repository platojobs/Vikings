"""规则引擎单测：覆盖电压/温度双向/湿度/信号四类指标的 warning 与 critical。"""

from types import SimpleNamespace

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
