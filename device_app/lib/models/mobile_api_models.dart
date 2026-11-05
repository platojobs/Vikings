/// 移动看板使用的 REST 视图模型。
///
/// 字段名与 `docs/contracts.md` 的 snake_case REST 出参一一对应；
/// MQTT 报文模型仍留在 [SensorPayload] 系列，两类协议不混用。
library;

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

double _asDouble(Object? value, [double fallback = 0]) {
  if (value is num) {
    return value.toDouble();
  }
  if (value is String) {
    return double.tryParse(value) ?? fallback;
  }
  return fallback;
}

String _asString(Object? value, [String fallback = '']) =>
    value?.toString() ?? fallback;

String? _asNullableString(Object? value) {
  final text = value?.toString().trim();
  return (text == null || text.isEmpty) ? null : text;
}

DateTime? _asTime(Object? value) =>
    value is String ? DateTime.tryParse(value) : null;

/// 登录成功后的会话，token 持久化在本地。
///
/// v1.24.0 起同时保存短 TTL access 与可轮换 refresh；旧版本本地会话没有
/// [refreshToken]，遇到 401 时无法自动续期，按原逻辑清会话回登录页。
class AuthSession {
  const AuthSession({
    required this.token,
    required this.username,
    required this.role,
    this.refreshToken = '',
  });

  /// 访问令牌（默认 20 分钟），随每次 REST 请求发送。
  final String token;

  /// 刷新令牌（默认 14 天），只在 /api/auth/refresh|logout 使用，单次有效。
  final String refreshToken;
  final String username;
  final String role;

  bool get canIssueCommand => role == 'admin' || role == 'operator';

  bool get canRefresh => refreshToken.isNotEmpty;

  Map<String, dynamic> toJson() => <String, dynamic>{
    'token': token,
    'refresh_token': refreshToken,
    'username': username,
    'role': role,
  };

  factory AuthSession.fromJson(Map<String, dynamic> json) => AuthSession(
    token: _asString(json['token']),
    refreshToken: _asString(json['refresh_token']),
    username: _asString(json['username']),
    role: _asString(json['role'], 'viewer'),
  );
}

/// 登录后由网关下发的 MQTT 只读凭证（9001 WebSocket）。
class MqttClientCredential {
  const MqttClientCredential({
    required this.username,
    required this.password,
    this.readOnly = true,
  });

  final String username;
  final String password;
  final bool readOnly;

  factory MqttClientCredential.fromJson(Map<String, dynamic> json) =>
      MqttClientCredential(
        username: _asString(json['username']),
        password: _asString(json['password']),
        readOnly: json['read_only'] != false,
      );
}

/// `GET /api/devices` 的设备条目。
class DeviceSummary {
  const DeviceSummary({
    required this.deviceId,
    required this.name,
    required this.model,
    required this.firmwareVersion,
    required this.groupName,
    required this.status,
    this.lastReportAt,
    this.lastOnlineAt,
    this.openAlertCount = 0,
    this.mqttUsername,
    this.mqttCredentialVersion = 0,
    this.mqttCredentialIssuedAt,
    this.mqttCredentialRotatedAt,
    this.mqttCredentialRevokedAt,
    this.mqttCredentialStatus = 'missing',
    this.mqttCredentialFingerprintPrefix,
  });

  final String deviceId;
  final String name;
  final String model;
  final String firmwareVersion;
  final String groupName;

  /// online / offline，由网关根据上报与离线巡检维护。
  final String status;
  final DateTime? lastReportAt;
  final DateTime? lastOnlineAt;

  /// 该设备未处理告警数（服务端聚合，`withAlertCount=true` 时返回）。
  final int openAlertCount;

  /// MQTT 凭证元数据：服务端只返回状态与指纹短前缀，绝不返回口令/完整指纹。
  final String? mqttUsername;
  final int mqttCredentialVersion;
  final DateTime? mqttCredentialIssuedAt;
  final DateTime? mqttCredentialRotatedAt;
  final DateTime? mqttCredentialRevokedAt;

  /// missing / active / revoked
  final String mqttCredentialStatus;
  final String? mqttCredentialFingerprintPrefix;

  bool get online => status == 'online';
  bool get credentialIssued => mqttCredentialStatus != 'missing';
  bool get credentialRevoked => mqttCredentialStatus == 'revoked';

  DeviceSummary copyWith({
    int? openAlertCount,
    String? groupName,
    int? mqttCredentialVersion,
    DateTime? mqttCredentialIssuedAt,
    DateTime? mqttCredentialRotatedAt,
    DateTime? mqttCredentialRevokedAt,
    String? mqttCredentialStatus,
    String? mqttCredentialFingerprintPrefix,
  }) => DeviceSummary(
    deviceId: deviceId,
    name: name,
    model: model,
    firmwareVersion: firmwareVersion,
    groupName: groupName ?? this.groupName,
    status: status,
    lastReportAt: lastReportAt,
    lastOnlineAt: lastOnlineAt,
    openAlertCount: openAlertCount ?? this.openAlertCount,
    mqttUsername: mqttUsername,
    mqttCredentialVersion: mqttCredentialVersion ?? this.mqttCredentialVersion,
    mqttCredentialIssuedAt: mqttCredentialIssuedAt ?? this.mqttCredentialIssuedAt,
    mqttCredentialRotatedAt:
        mqttCredentialRotatedAt ?? this.mqttCredentialRotatedAt,
    mqttCredentialRevokedAt:
        mqttCredentialRevokedAt ?? this.mqttCredentialRevokedAt,
    mqttCredentialStatus: mqttCredentialStatus ?? this.mqttCredentialStatus,
    mqttCredentialFingerprintPrefix:
        mqttCredentialFingerprintPrefix ?? this.mqttCredentialFingerprintPrefix,
  );

  factory DeviceSummary.fromJson(Map<String, dynamic> json) => DeviceSummary(
    deviceId: _asString(json['device_id']),
    name: _asString(json['name']),
    model: _asString(json['model']),
    firmwareVersion: _asString(json['firmware_version']),
    groupName: _asString(json['group_name'], '默认分组'),
    status: _asString(json['status'], 'offline'),
    lastReportAt: _asTime(json['last_report_at']),
    lastOnlineAt: _asTime(json['last_online_at']),
    openAlertCount: _asInt(json['open_alert_count']),
    mqttUsername: json['mqtt_username']?.toString(),
    mqttCredentialVersion: _asInt(json['mqtt_credential_version']),
    mqttCredentialIssuedAt: _asTime(json['mqtt_credential_issued_at']),
    mqttCredentialRotatedAt: _asTime(json['mqtt_credential_rotated_at']),
    mqttCredentialRevokedAt: _asTime(json['mqtt_credential_revoked_at']),
    mqttCredentialStatus: _asString(json['mqtt_credential_status'], 'missing'),
    mqttCredentialFingerprintPrefix:
        json['mqtt_credential_fingerprint_prefix']?.toString(),
  );
}

/// `POST /api/devices/{id}/mqtt-credential`：口令只在本次响应出现一次。
class DeviceCredentialResult {
  const DeviceCredentialResult({
    required this.deviceId,
    required this.username,
    required this.password,
    required this.version,
    required this.operation,
    required this.reloaded,
    required this.reloadDetail,
    required this.rotatedAt,
  });

  final String deviceId;
  final String username;
  final String password;

  /// issue（首次签发）/ rotate（覆盖已有有效凭证）
  final String operation;
  final int version;
  final bool reloaded;
  final String reloadDetail;
  final DateTime? rotatedAt;

  bool get isRotation => operation == 'rotate';

  factory DeviceCredentialResult.fromJson(Map<String, dynamic> json) =>
      DeviceCredentialResult(
        deviceId: _asString(json['device_id']),
        username: _asString(json['username']),
        password: _asString(json['password']),
        version: _asInt(json['version']),
        operation: _asString(json['operation'], 'issue'),
        reloaded: json['reloaded'] == true,
        reloadDetail: _asString(json['reload_detail']),
        rotatedAt: _asTime(json['rotated_at']),
      );
}

/// `DELETE /api/devices/{id}/mqtt-credential`：吊销结果不含口令与完整指纹。
class DeviceCredentialRevokeResult {
  const DeviceCredentialRevokeResult({
    required this.deviceId,
    required this.username,
    required this.version,
    required this.revokedAt,
    required this.reloaded,
    required this.reloadDetail,
  });

  final String deviceId;
  final String username;
  final int version;
  final DateTime? revokedAt;
  final bool reloaded;
  final String reloadDetail;

  factory DeviceCredentialRevokeResult.fromJson(Map<String, dynamic> json) =>
      DeviceCredentialRevokeResult(
        deviceId: _asString(json['device_id']),
        username: _asString(json['username']),
        version: _asInt(json['version']),
        revokedAt: _asTime(json['revoked_at']),
        reloaded: json['reloaded'] == true,
        reloadDetail: _asString(json['reload_detail']),
      );
}

/// `GET /api/devices` 的分页结果：`data.items` + `total/page/page_size`。
class DevicePage {
  const DevicePage({
    required this.items,
    required this.total,
    required this.page,
    required this.pageSize,
  });

  final List<DeviceSummary> items;
  final int total;
  final int page;
  final int pageSize;

  bool get hasMore => page * pageSize < total;

  factory DevicePage.fromJson(Map<String, dynamic> json) => DevicePage(
    items: (json['items'] is List)
        ? (json['items'] as List)
              .whereType<Map>()
              .map(
                (item) =>
                    DeviceSummary.fromJson(Map<String, dynamic>.from(item)),
              )
              .toList(growable: false)
        : const <DeviceSummary>[],
    total: _asInt(json['total']),
    page: _asInt(json['page'], 1),
    pageSize: _asInt(json['page_size'], 20),
  );
}

/// `GET /api/alerts` 的分页结果。
class AlertPage {
  const AlertPage({
    required this.items,
    required this.total,
    required this.page,
    required this.pageSize,
  });

  final List<AlertItem> items;
  final int total;
  final int page;
  final int pageSize;

  factory AlertPage.fromJson(Map<String, dynamic> json) => AlertPage(
    items: (json['items'] is List)
        ? (json['items'] as List)
              .whereType<Map>()
              .map(
                (item) => AlertItem.fromJson(Map<String, dynamic>.from(item)),
              )
              .toList(growable: false)
        : const <AlertItem>[],
    total: _asInt(json['total']),
    page: _asInt(json['page'], 1),
    pageSize: _asInt(json['page_size'], 20),
  );
}

/// `GET /api/devices/groups` 的分组概览。
class DeviceGroup {
  const DeviceGroup({
    required this.groupName,
    required this.total,
    required this.online,
    required this.offline,
  });

  final String groupName;
  final int total;
  final int online;
  final int offline;

  factory DeviceGroup.fromJson(Map<String, dynamic> json) => DeviceGroup(
    groupName: _asString(json['group_name'], '未分组'),
    total: _asInt(json['total']),
    online: _asInt(json['online']),
    offline: _asInt(json['offline']),
  );
}

/// 概览页 24 小时趋势的小时桶（服务端已按真实数据填充，空桶为 0）。
class OverviewTrendPoint {
  const OverviewTrendPoint({
    required this.hourStart,
    required this.reports,
    required this.alerts,
  });

  final DateTime hourStart;
  final int reports;
  final int alerts;

  factory OverviewTrendPoint.fromJson(Map<String, dynamic> json) =>
      OverviewTrendPoint(
        hourStart:
            DateTime.tryParse(_asString(json['hour_start'], ''))?.toLocal() ??
            DateTime.fromMillisecondsSinceEpoch(0),
        reports: _asInt(json['reports']),
        alerts: _asInt(json['alerts']),
      );
}

/// 概览页异常设备：有未处理告警或离线。
class AbnormalDevice {
  const AbnormalDevice({
    required this.deviceId,
    required this.name,
    required this.status,
    required this.openAlertCount,
    required this.reason,
  });

  final String deviceId;
  final String name;
  final String status;
  final int openAlertCount;

  /// alert / offline / alert+offline，由服务端给出，客户端只做文案映射。
  final String reason;

  factory AbnormalDevice.fromJson(Map<String, dynamic> json) => AbnormalDevice(
    deviceId: _asString(json['device_id'], ''),
    name: _asString(json['name'], ''),
    status: _asString(json['status'], 'offline'),
    openAlertCount: _asInt(json['open_alert_count']),
    reason: _asString(json['reason'], 'offline'),
  );
}

/// `GET /api/stats/overview`：看板汇总卡与概览页使用的服务端全量统计。
class StatsOverview {
  const StatsOverview({
    required this.deviceTotal,
    required this.deviceOnline,
    required this.deviceOffline,
    required this.reportToday,
    required this.alertOpen,
    this.credentialMissing = 0,
    this.warningOpen = 0,
    this.criticalOpen = 0,
    this.trend = const <OverviewTrendPoint>[],
    this.trendComplete = false,
    this.firstRecordAt,
    this.abnormalDevices = const <AbnormalDevice>[],
  });

  final int deviceTotal;
  final int deviceOnline;
  final int deviceOffline;
  final int reportToday;
  final int alertOpen;
  final int credentialMissing;
  final int warningOpen;
  final int criticalOpen;
  final List<OverviewTrendPoint> trend;
  final bool trendComplete;
  final DateTime? firstRecordAt;
  final List<AbnormalDevice> abnormalDevices;

  static const StatsOverview empty = StatsOverview(
    deviceTotal: 0,
    deviceOnline: 0,
    deviceOffline: 0,
    reportToday: 0,
    alertOpen: 0,
  );

  StatsOverview copyWith({int? alertOpen}) => StatsOverview(
    deviceTotal: deviceTotal,
    deviceOnline: deviceOnline,
    deviceOffline: deviceOffline,
    reportToday: reportToday,
    alertOpen: alertOpen ?? this.alertOpen,
    credentialMissing: credentialMissing,
    warningOpen: warningOpen,
    criticalOpen: criticalOpen,
    trend: trend,
    trendComplete: trendComplete,
    firstRecordAt: firstRecordAt,
    abnormalDevices: abnormalDevices,
  );

  factory StatsOverview.fromJson(Map<String, dynamic> json) => StatsOverview(
    deviceTotal: _asInt(json['device_total']),
    deviceOnline: _asInt(json['device_online']),
    deviceOffline: _asInt(json['device_offline']),
    reportToday: _asInt(json['report_today']),
    alertOpen: _asInt(json['alert_open']),
    credentialMissing: _asInt(json['device_credential_missing']),
    warningOpen: _asInt(json['alert_warning_open']),
    criticalOpen: _asInt(json['alert_critical_open']),
    trend: json['trend'] is List
        ? (json['trend'] as List)
              .whereType<Map<String, dynamic>>()
              .map(OverviewTrendPoint.fromJson)
              .toList(growable: false)
        : const <OverviewTrendPoint>[],
    trendComplete: json['trend_complete'] == true,
    firstRecordAt: json['first_record_at'] == null
        ? null
        : DateTime.tryParse(_asString(json['first_record_at'], ''))?.toLocal(),
    abnormalDevices: json['abnormal_devices'] is List
        ? (json['abnormal_devices'] as List)
              .whereType<Map<String, dynamic>>()
              .map(AbnormalDevice.fromJson)
              .toList(growable: false)
        : const <AbnormalDevice>[],
  );
}

/// `GET /api/records/latest` 的每设备最新一条上报。
class SensorReading {
  const SensorReading({
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
  final DateTime? ts;

  factory SensorReading.fromJson(Map<String, dynamic> json) => SensorReading(
    deviceId: _asString(json['device_id']),
    temperature: _asDouble(json['temperature']),
    humidity: _asDouble(json['humidity']),
    voltage: _asDouble(json['voltage']),
    rssi: _asInt(json['rssi']),
    battery: _asInt(json['battery']),
    seq: _asInt(json['seq']),
    ts: _asTime(json['ts']),
  );
}

/// `GET /api/alerts` 的告警条目。
class AlertItem {
  const AlertItem({
    required this.alertId,
    required this.deviceId,
    required this.level,
    required this.metric,
    required this.value,
    required this.threshold,
    required this.message,
    required this.aiGenerated,
    required this.status,
    this.createdAt,
    this.ackNote,
    this.ackedBy,
    this.ackedAt,
  });

  final String alertId;
  final String deviceId;

  /// warning / critical
  final String level;
  final String metric;
  final double value;
  final double threshold;
  final String message;
  final bool aiGenerated;

  /// open / acked
  final String status;
  final DateTime? createdAt;

  /// 处置备注（v1.21.0，可选）
  final String? ackNote;

  /// 确认人；v1.21.0 之前的历史确认行为 null
  final String? ackedBy;

  /// 确认时间；历史确认行为 null
  final DateTime? ackedAt;

  bool get critical => level == 'critical';

  factory AlertItem.fromJson(Map<String, dynamic> json) => AlertItem(
    alertId: _asString(json['alert_id']),
    deviceId: _asString(json['device_id']),
    level: _asString(json['level'], 'warning'),
    metric: _asString(json['metric']),
    value: _asDouble(json['value']),
    threshold: _asDouble(json['threshold']),
    message: _asString(json['message']),
    aiGenerated: json['ai_generated'] == true,
    status: _asString(json['status'], 'open'),
    createdAt: _asTime(json['created_at']),
    ackNote: _asNullableString(json['ack_note']),
    ackedBy: _asNullableString(json['acked_by']),
    ackedAt: _asTime(json['acked_at']),
  );
}

/// `GET /api/alerts/summary`：告警中心角标与概览使用。
class AlertSummary {
  const AlertSummary({
    required this.open,
    required this.criticalOpen,
    required this.today,
  });

  final int open;
  final int criticalOpen;
  final int today;

  static const AlertSummary empty = AlertSummary(
    open: 0,
    criticalOpen: 0,
    today: 0,
  );

  factory AlertSummary.fromJson(Map<String, dynamic> json) => AlertSummary(
    open: _asInt(json['open']),
    criticalOpen: _asInt(json['critical_open']),
    today: _asInt(json['today']),
  );
}

/// `POST /api/alerts/bulk-ack`：逐类返回，部分失败对调用方可见。
class BulkAckResult {
  const BulkAckResult({
    required this.acked,
    required this.alreadyAcked,
    required this.notFound,
    required this.requested,
  });

  /// 本次由 open 变为 acked 的告警 ID。
  final List<String> acked;

  /// 此前已确认（幂等跳过，不算失败）。
  final List<String> alreadyAcked;

  /// 不存在的告警 ID。
  final List<String> notFound;

  /// 去重后的请求条数。
  final int requested;

  factory BulkAckResult.fromJson(Map<String, dynamic> json) => BulkAckResult(
    acked: _stringList(json['acked']),
    alreadyAcked: _stringList(json['already_acked']),
    notFound: _stringList(json['not_found']),
    requested: _asInt(json['requested']),
  );

  static List<String> _stringList(Object? value) => value is List
      ? value.map((item) => item.toString()).toList(growable: false)
      : const <String>[];
}

/// `GET /api/devices/{id}/commands` 的指令回执。
class CommandItem {
  const CommandItem({
    required this.cmdId,
    required this.type,
    required this.state,
    required this.progress,
    this.message,
    required this.operator,
    this.createdAt,
  });

  final String cmdId;

  /// calibrate / reboot / config / ota
  final String type;

  /// received / progress / success / failed
  final String state;
  final int progress;
  final String? message;
  final String operator;
  final DateTime? createdAt;

  factory CommandItem.fromJson(Map<String, dynamic> json) => CommandItem(
    cmdId: _asString(json['cmd_id']),
    type: _asString(json['type']),
    state: _asString(json['state']),
    progress: _asInt(json['progress']),
    message: json['message']?.toString(),
    operator: _asString(json['operator']),
    createdAt: _asTime(json['created_at']),
  );
}
