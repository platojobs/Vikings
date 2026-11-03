/// MQTT 报文模型。
///
/// 字段名与 docs/contracts.md 严格一致（camelCase），任何改动必须先改契约。
/// 所有报文统一通过 [toJson] + jsonEncode 序列化，禁止手拼 JSON 字符串。
library;

/// 读取任意 JSON 数值为 int（兼容 1 / 1.0 / "1" 三种下发形式）。
int _asInt(Object? value, [int fallback = 0]) {
  if (value is int) {
    return value;
  }
  if (value is num) {
    return value.toInt();
  }
  if (value is String) {
    return int.tryParse(value) ?? num.tryParse(value)?.toInt() ?? fallback;
  }
  return fallback;
}

/// 读取任意 JSON 数值为 double。
double _asDouble(Object? value, [double fallback = 0]) {
  if (value is num) {
    return value.toDouble();
  }
  if (value is String) {
    return double.tryParse(value) ?? fallback;
  }
  return fallback;
}

/// 契约 2.1 传感器上报 `device/{deviceId}/sensor/report`。
class SensorPayload {
  const SensorPayload({
    required this.deviceId,
    required this.temperature,
    required this.humidity,
    required this.voltage,
    required this.rssi,
    required this.battery,
    required this.seq,
    required this.ts,
  });

  final String deviceId;
  final double temperature;
  final double humidity;
  final double voltage;
  final int rssi;
  final int battery;
  final int seq;
  final int ts;

  Map<String, dynamic> toJson() => <String, dynamic>{
    'deviceId': deviceId,
    'temperature': temperature,
    'humidity': humidity,
    'voltage': voltage,
    'rssi': rssi,
    'battery': battery,
    'seq': seq,
    'ts': ts,
  };

  /// 从报文还原（用于本地展示/调试，网关侧来源的数据结构保持一致）。
  factory SensorPayload.fromJson(Map<String, dynamic> json) => SensorPayload(
    deviceId: json['deviceId']?.toString() ?? '',
    temperature: _asDouble(json['temperature']),
    humidity: _asDouble(json['humidity']),
    voltage: _asDouble(json['voltage']),
    rssi: _asInt(json['rssi']),
    battery: _asInt(json['battery']),
    seq: _asInt(json['seq']),
    ts: _asInt(json['ts']),
  );
}

/// 契约 2.2 状态报文 `device/{deviceId}/status`。
class DeviceStatusPayload {
  const DeviceStatusPayload({
    required this.deviceId,
    required this.status,
    required this.firmwareVersion,
    required this.reason,
    required this.ts,
  });

  final String deviceId;

  /// online / offline
  final String status;

  /// 当前固件版本，OTA 成功后需要携带新版本再次上线。
  final String firmwareVersion;

  /// boot（上电上线）/ sleep（主动休眠下线）/ lwt（Broker 遗嘱）。
  final String reason;

  final int ts;

  Map<String, dynamic> toJson() => <String, dynamic>{
    'deviceId': deviceId,
    'status': status,
    'firmwareVersion': firmwareVersion,
    'reason': reason,
    'ts': ts,
  };
}

/// 契约 2.3 云端下发指令 `device/{deviceId}/cmd`。
class DeviceCommand {
  const DeviceCommand({
    required this.cmdId,
    required this.type,
    required this.payload,
    required this.operator,
    required this.ts,
  });

  final String cmdId;

  /// calibrate / ota / reboot / config
  final String type;
  final Map<String, dynamic> payload;
  final String operator;
  final int ts;

  factory DeviceCommand.fromJson(Map<String, dynamic> json) {
    final rawPayload = json['payload'];
    return DeviceCommand(
      cmdId: json['cmdId']?.toString() ?? '',
      type: json['type']?.toString() ?? '',
      payload: rawPayload is Map
          ? Map<String, dynamic>.from(rawPayload)
          : <String, dynamic>{},
      operator: json['operator']?.toString() ?? '',
      ts: _asInt(json['ts']),
    );
  }
}

/// 契约 2.4 指令回执 `device/{deviceId}/cmd/ack`。
class CommandAckPayload {
  const CommandAckPayload({
    required this.cmdId,
    required this.type,
    required this.state,
    required this.progress,
    required this.message,
    required this.ts,
  });

  final String cmdId;
  final String type;

  /// received / progress / success / failed
  final String state;

  /// 0 ~ 100，仅 OTA 场景有意义。
  final int progress;
  final String message;
  final int ts;

  Map<String, dynamic> toJson() => <String, dynamic>{
    'cmdId': cmdId,
    'type': type,
    'state': state,
    'progress': progress,
    'message': message,
    'ts': ts,
  };
}
