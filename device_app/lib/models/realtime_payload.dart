import 'dart:convert';

import 'mobile_api_models.dart';

/// 移动看板实时事件：由 MQTT 报文解析而来，只承载展示所需的最小信息。
sealed class RealtimeEvent {
  const RealtimeEvent();
}

/// `device/system/alert`：网关规则引擎产生的新告警。
class AlertEvent extends RealtimeEvent {
  const AlertEvent(this.alert);

  final AlertItem alert;
}

/// `device/{id}/sensor/report`：设备最新一次上报。
class ReportEvent extends RealtimeEvent {
  const ReportEvent(this.reading);

  final SensorReading reading;
}

/// `device/{id}/status`：设备上下线（含 LWT 遗嘱）。
class StatusEvent extends RealtimeEvent {
  const StatusEvent({
    required this.deviceId,
    required this.status,
    required this.reason,
  });

  final String deviceId;

  /// online / offline
  final String status;

  /// boot / sleep / lwt，仅用于排障展示。
  final String reason;

  bool get online => status == 'online';
}

/// MQTT 报文解析器。
///
/// 注意：MQTT 报文使用 camelCase（见 `docs/contracts.md` 第 2 节），
/// 与 REST 的 snake_case 不同，因此不能复用 [AlertItem.fromJson]。
/// 解析失败（非 JSON 对象、Topic 不匹配、缺少关键字段）一律返回 null，
/// 由调用方丢弃，避免一条脏报文影响整个实时链路。
class RealtimeParser {
  const RealtimeParser();

  static const String alertTopic = 'device/system/alert';

  RealtimeEvent? parse(String topic, String payload) {
    if (payload.isEmpty) {
      return null;
    }
    Map<String, dynamic>? json;
    try {
      final decoded = jsonDecode(payload);
      if (decoded is Map<String, dynamic>) {
        json = decoded;
      }
    } catch (_) {
      return null;
    }
    if (json == null) {
      return null;
    }

    final parts = topic.split('/');
    if (topic == alertTopic) {
      return _parseAlert(json);
    }
    if (parts.length == 4 && parts[0] == 'device' && parts[2] == 'sensor') {
      final deviceId = parts[1];
      return _parseReport(json, deviceId);
    }
    if (parts.length == 3 && parts[0] == 'device' && parts[2] == 'status') {
      return _parseStatus(json, parts[1]);
    }
    return null;
  }

  /// 允许经 `utf8.decode` 后的文本，也兼容直接传入原始字符串。
  RealtimeEvent? _parseAlert(Map<String, dynamic> json) {
    final alertId = _str(json['alertId']);
    final deviceId = _str(json['deviceId']);
    if (alertId.isEmpty || deviceId.isEmpty) {
      return null;
    }
    return AlertEvent(
      AlertItem(
        alertId: alertId,
        deviceId: deviceId,
        level: _str(json['level'], 'warning'),
        metric: _str(json['metric']),
        value: _num(json['value']),
        threshold: _num(json['threshold']),
        message: _str(json['message']),
        aiGenerated: json['aiGenerated'] == true,
        status: 'open',
        createdAt: _time(json['ts']),
      ),
    );
  }

  RealtimeEvent? _parseReport(Map<String, dynamic> json, String topicDeviceId) {
    final deviceId = _str(json['deviceId'], topicDeviceId);
    if (deviceId.isEmpty) {
      return null;
    }
    final battery = json['battery'];
    if (battery == null) {
      return null;
    }
    return ReportEvent(
      SensorReading(
        deviceId: deviceId,
        temperature: _num(json['temperature']),
        humidity: _num(json['humidity']),
        voltage: _num(json['voltage']),
        rssi: _int(json['rssi']),
        battery: _int(battery),
        seq: _int(json['seq']),
        ts: _time(json['ts']),
      ),
    );
  }

  RealtimeEvent? _parseStatus(Map<String, dynamic> json, String topicDeviceId) {
    final deviceId = _str(json['deviceId'], topicDeviceId);
    final status = _str(json['status']);
    if (deviceId.isEmpty || (status != 'online' && status != 'offline')) {
      return null;
    }
    return StatusEvent(
      deviceId: deviceId,
      status: status,
      reason: _str(json['reason']),
    );
  }

  String _str(Object? value, [String fallback = '']) {
    final text = value?.toString() ?? fallback;
    return text;
  }

  double _num(Object? value) {
    if (value is num) {
      return value.toDouble();
    }
    if (value is String) {
      return double.tryParse(value) ?? 0;
    }
    return 0;
  }

  int _int(Object? value) {
    if (value is int) {
      return value;
    }
    if (value is num) {
      return value.toInt();
    }
    if (value is String) {
      return int.tryParse(value) ?? 0;
    }
    return 0;
  }

  /// MQTT 契约里 ts 是毫秒时间戳，也兼容 ISO8601 字符串。
  DateTime? _time(Object? value) {
    if (value is num) {
      return DateTime.fromMillisecondsSinceEpoch(value.toInt(), isUtc: true);
    }
    if (value is String) {
      final parsed = int.tryParse(value);
      if (parsed != null) {
        return DateTime.fromMillisecondsSinceEpoch(parsed, isUtc: true);
      }
      return DateTime.tryParse(value);
    }
    return null;
  }
}
