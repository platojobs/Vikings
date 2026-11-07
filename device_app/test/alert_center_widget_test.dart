import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:iot_device_app/models/mobile_api_models.dart';
import 'package:iot_device_app/models/realtime_payload.dart';
import 'package:iot_device_app/pages/alert_center_page.dart';
import 'package:iot_device_app/pages/home_shell.dart';
import 'package:iot_device_app/services/api_client.dart';
import 'package:iot_device_app/services/app_storage.dart';
import 'package:iot_device_app/services/board_realtime_service.dart';
import 'package:shared_preferences/shared_preferences.dart';

const AuthSession _adminSession = AuthSession(
  token: 'jwt-admin',
  username: 'admin',
  role: 'admin',
);
const AuthSession _viewerSession = AuthSession(
  token: 'jwt-viewer',
  username: 'viewer',
  role: 'viewer',
);

const DeviceSummary _deviceA = DeviceSummary(
  deviceId: 'dev-0001',
  name: '一号机',
  model: 'Battery-Sensor',
  firmwareVersion: '1.0.0',
  groupName: '默认分组',
  status: 'online',
);

AlertItem _alert(
  String id,
  String level,
  String status, {
  String deviceId = 'dev-0001',
  double value = 80,
  String metric = 'temperature',
  String? ackNote,
  String? ackedBy,
  DateTime? ackedAt,
}) => AlertItem(
  alertId: id,
  deviceId: deviceId,
  level: level,
  metric: metric,
  value: value,
  threshold: 75,
  message: '$metric 越界',
  aiGenerated: false,
  status: status,
  createdAt: DateTime.utc(2026, 11, 4, 8),
  ackNote: ackNote,
  ackedBy: ackedBy,
  ackedAt: ackedAt,
);

/// 内存版安全存储，测试不触碰 Keychain / Keystore。
class _FakeSecureStore implements SecureStore {
  final Map<String, String> values = <String, String>{};

  @override
  Future<String?> read(String key) async => values[key];

  @override
  Future<void> write(String key, String value) async {
    values[key] = value;
  }

  @override
  Future<void> delete(String key) async {
    values.remove(key);
  }
}

/// 假 REST 客户端：模拟网关过滤 / 分页 / 批量确认语义，不发真实请求。
class _FakeApiClient extends ApiClient {
  _FakeApiClient({
    List<AlertItem>? alerts,
    this.bulkError,
    this.failBulkCall,
    this.singlePageAlerts = false,
    List<DeviceSummary>? devices,
  }) : alerts = List<AlertItem>.of(alerts ?? const <AlertItem>[]),
       devices = List<DeviceSummary>.of(
         devices ?? const <DeviceSummary>[_deviceA],
       ),
       super(baseUrl: 'http://192.168.1.8:8000');

  final List<AlertItem> alerts;
  final List<DeviceSummary> devices;
  final Object? bulkError;

  /// 指定第几次批量请求失败（从 1 开始），用于验证单批失败不拖累已成功批次。
  final int? failBulkCall;

  /// 测试用：让告警列表一次返回全部结果，便于构造 150 条多选场景。
  final bool singlePageAlerts;

  int alertFetchCalls = 0;
  int bulkCalls = 0;
  int summaryCalls = 0;
  int deviceFetchCalls = 0;
  int? lastPage;
  String? lastLevel;
  String? lastStatus;
  String? lastDeviceId;
  List<String>? lastTags;
  String? lastDeviceKeyword;
  int? lastDevicePage;
  int? lastDevicePageSize;
  List<String> lastBulkIds = const <String>[];
  String? lastBulkNote;
  final List<List<String>> bulkBatches = <List<String>>[];

  @override
  Future<AlertPage> fetchAlerts({
    required String token,
    int page = 1,
    int pageSize = 20,
    String? deviceId,
    String? level,
    String? status,
    List<String>? tags,
  }) async {
    alertFetchCalls += 1;
    lastPage = page;
    lastLevel = level;
    lastStatus = status;
    lastDeviceId = deviceId;
    lastTags = tags;
    var filtered = alerts;
    if (tags != null && tags.isNotEmpty) {
      final taggedIds = devices
          .where((device) => device.tags.any(tags.contains))
          .map((device) => device.deviceId)
          .toSet();
      filtered = filtered
          .where((item) => taggedIds.contains(item.deviceId))
          .toList();
    }
    if (level != null && level.isNotEmpty) {
      filtered = filtered.where((item) => item.level == level).toList();
    }
    if (status != null && status.isNotEmpty) {
      filtered = filtered.where((item) => item.status == status).toList();
    }
    if (deviceId != null && deviceId.isNotEmpty) {
      filtered = filtered.where((item) => item.deviceId == deviceId).toList();
    }
    final total = filtered.length;
    if (singlePageAlerts) {
      return AlertPage(
        items: filtered,
        total: total,
        page: 1,
        pageSize: total == 0 ? pageSize : total,
      );
    }
    final start = (page - 1) * pageSize;
    final items = start >= total
        ? const <AlertItem>[]
        : filtered.sublist(
            start,
            start + pageSize > total ? total : start + pageSize,
          );
    return AlertPage(
      items: items,
      total: total,
      page: page,
      pageSize: pageSize,
    );
  }

  @override
  Future<AlertSummary> fetchAlertSummary({required String token}) async {
    summaryCalls += 1;
    final open = alerts.where((item) => item.status == 'open').toList();
    return AlertSummary(
      open: open.length,
      criticalOpen: open.where((item) => item.level == 'critical').length,
      today: open.length,
    );
  }

  @override
  Future<BulkAckResult> bulkAckAlerts({
    required String token,
    required List<String> alertIds,
    String? note,
  }) async {
    bulkCalls += 1;
    lastBulkIds = List<String>.of(alertIds);
    lastBulkNote = note;
    bulkBatches.add(List<String>.of(alertIds));
    final error = bulkError;
    if (error != null) {
      throw error;
    }
    if (failBulkCall != null && bulkCalls == failBulkCall) {
      throw const ApiException(ApiErrorKind.network, '网络中断');
    }
    final acked = <String>[];
    final already = <String>[];
    final missing = <String>[];
    for (final id in alertIds) {
      final index = alerts.indexWhere((item) => item.alertId == id);
      if (index < 0) {
        missing.add(id);
      } else if (alerts[index].status == 'acked') {
        already.add(id);
      } else {
        acked.add(id);
        final row = alerts[index];
        alerts[index] = AlertItem(
          alertId: row.alertId,
          deviceId: row.deviceId,
          level: row.level,
          metric: row.metric,
          value: row.value,
          threshold: row.threshold,
          message: row.message,
          aiGenerated: row.aiGenerated,
          status: 'acked',
          createdAt: row.createdAt,
          ackNote: note,
          ackedBy: 'admin',
          ackedAt: DateTime.utc(2026, 11, 5, 4, 15),
        );
      }
    }
    return BulkAckResult(
      acked: acked,
      alreadyAcked: already,
      notFound: missing,
      requested: alertIds.length,
    );
  }

  @override
  Future<DevicePage> fetchDevices({
    required String token,
    int page = 1,
    int pageSize = 20,
    String? status,
    String? keyword,
    String? group,
    List<String>? tags,
    bool hasAlert = false,
    bool withAlertCount = false,
  }) async {
    deviceFetchCalls += 1;
    lastDeviceKeyword = keyword;
    lastDevicePage = page;
    lastDevicePageSize = pageSize;
    var filtered = devices;
    final text = (keyword ?? '').trim();
    if (text.isNotEmpty) {
      filtered = filtered
          .where(
            (device) =>
                device.deviceId.contains(text) || device.name.contains(text),
          )
          .toList(growable: false);
    }
    final total = filtered.length;
    final start = (page - 1) * pageSize;
    final items = start >= total
        ? const <DeviceSummary>[]
        : filtered.sublist(
            start,
            start + pageSize > total ? total : start + pageSize,
          );
    return DevicePage(
      items: items,
      total: total,
      page: page,
      pageSize: pageSize,
    );
  }

  // 以下为 HomeShell 中移动看板 tab 启动所需的最小返回，避免真实网络请求。
  @override
  Future<List<DeviceGroup>> fetchGroups({required String token}) async =>
      const <DeviceGroup>[];

  @override
  Future<List<DeviceTagStat>> fetchDeviceTags({required String token}) async {
    final counts = <String, int>{};
    for (final device in devices) {
      for (final tag in device.tags) {
        counts[tag] = (counts[tag] ?? 0) + 1;
      }
    }
    final stats = counts.entries
        .map((entry) => DeviceTagStat(tag: entry.key, deviceCount: entry.value))
        .toList(growable: false);
    stats.sort((a, b) => a.tag.compareTo(b.tag));
    return stats;
  }

  @override
  Future<List<SensorReading>> fetchLatestReadings({
    required String token,
    List<String>? deviceIds,
  }) async => const <SensorReading>[];

  @override
  Future<StatsOverview> fetchStatsOverview({required String token}) async =>
      StatsOverview.empty;

  @override
  Future<AlertPage> fetchOpenAlerts({
    required String token,
    int page = 1,
    int pageSize = 20,
  }) async =>
      const AlertPage(items: <AlertItem>[], total: 0, page: 1, pageSize: 20);

  @override
  void close() {}
}

class _FakeRealtimeService extends BoardRealtimeService {
  _FakeRealtimeService([this.initialState = RealtimeState.connected])
    : super(
        url: 'ws://192.168.1.8:9001',
        transportFactory: (_, _) => throw StateError('测试不应创建真实连接'),
      );

  final RealtimeState initialState;
  final StreamController<RealtimeEvent> _controller =
      StreamController<RealtimeEvent>.broadcast();

  @override
  Stream<RealtimeEvent> get events => _controller.stream;

  void emit(RealtimeEvent event) {
    if (!_controller.isClosed) {
      _controller.add(event);
    }
  }

  @override
  void start() {
    state.value = initialState;
  }

  @override
  Future<void> dispose() async {
    if (_disposed) {
      return;
    }
    _disposed = true;
    await _controller.close();
  }

  bool _disposed = false;
}

Future<AppStorage> _storageWith(AuthSession session) async {
  final storage = AppStorage(secureStore: _FakeSecureStore());
  await storage.saveSession(session);
  return storage;
}

Future<void> _pumpAlertCenter(
  WidgetTester tester, {
  required _FakeApiClient api,
  AuthSession? session,
  _FakeRealtimeService? realtime,
  bool active = true,
  void Function(int)? onUnreadChanged,
}) async {
  final storage = await _storageWith(session ?? _adminSession);
  await tester.pumpWidget(
    MaterialApp(
      home: AlertCenterPage(
        active: active,
        storage: storage,
        apiClientFactory: (_) => api,
        realtimeServiceFactory: (_) => realtime ?? _FakeRealtimeService(),
        onUnreadChanged: onUnreadChanged,
      ),
    ),
  );
}

Finder _levelChip(String level) => find.byKey(ValueKey('level-$level'));
Finder _statusChip(String status) => find.byKey(ValueKey('status-$status'));
Finder _tagChip(String tag) => find.byKey(ValueKey('tag-$tag'));

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUp(() {
    SharedPreferences.setMockInitialValues(<String, Object>{});
  });

  testWidgets('筛选切换：级别与状态分别转为服务端参数并刷新列表', (tester) async {
    final api = _FakeApiClient(
      alerts: <AlertItem>[
        _alert('alt-c1', 'critical', 'open'),
        _alert('alt-w1', 'warning', 'open', deviceId: 'dev-0002'),
        _alert('alt-c2', 'critical', 'acked', metric: 'voltage'),
      ],
    );
    await _pumpAlertCenter(tester, api: api);
    await tester.pumpAndSettle();

    expect(api.lastStatus, 'open');
    expect(find.byKey(const ValueKey('alert-row-alt-c1')), findsOneWidget);
    expect(find.byKey(const ValueKey('alert-row-alt-w1')), findsOneWidget);

    await tester.tap(_levelChip('critical'));
    await tester.pumpAndSettle();
    expect(api.lastLevel, 'critical');
    expect(find.byKey(const ValueKey('alert-row-alt-c1')), findsOneWidget);
    expect(find.byKey(const ValueKey('alert-row-alt-w1')), findsNothing);

    await tester.tap(_statusChip('acked'));
    await tester.pumpAndSettle();
    expect(api.lastStatus, 'acked');
    expect(find.byKey(const ValueKey('alert-row-alt-c2')), findsOneWidget);
    expect(find.byKey(const ValueKey('alert-row-alt-c1')), findsNothing);
  });

  testWidgets('标签筛选与级别/状态组合生效并重置分页', (tester) async {
    const taggedA = DeviceSummary(
      deviceId: 'dev-0001',
      name: '一号机',
      model: 'Battery-Sensor',
      firmwareVersion: '1.0.0',
      groupName: '默认分组',
      status: 'online',
      tags: <String>['关键'],
    );
    const taggedB = DeviceSummary(
      deviceId: 'dev-0002',
      name: '二号机',
      model: 'Battery-Sensor',
      firmwareVersion: '1.0.0',
      groupName: '默认分组',
      status: 'online',
      tags: <String>['普通'],
    );
    final api = _FakeApiClient(
      alerts: <AlertItem>[
        _alert('alt-t1', 'critical', 'open'),
        _alert('alt-t2', 'warning', 'open', deviceId: 'dev-0002'),
      ],
      devices: const <DeviceSummary>[taggedA, taggedB],
    );
    await _pumpAlertCenter(tester, api: api);
    await tester.pumpAndSettle();

    // 标签 chips 来自 GET /api/devices/tags
    expect(find.widgetWithText(ChoiceChip, '关键（1）'), findsOneWidget);
    expect(find.widgetWithText(ChoiceChip, '普通（1）'), findsOneWidget);

    await tester.tap(_tagChip('关键'));
    await tester.pumpAndSettle();
    expect(api.lastPage, 1);
    expect(api.lastTags, const <String>['关键']);
    expect(find.byKey(const ValueKey('alert-row-alt-t1')), findsOneWidget);
    expect(find.byKey(const ValueKey('alert-row-alt-t2')), findsNothing);

    // 叠加状态筛选：标签 + 状态同时下发
    await tester.tap(_statusChip('acked'));
    await tester.pumpAndSettle();
    expect(api.lastStatus, 'acked');
    expect(api.lastTags, const <String>['关键']);
    expect(find.byKey(const ValueKey('alert-row-alt-t1')), findsNothing);

    // 再叠加级别筛选：标签 + 状态 + 级别三者同时生效
    await tester.tap(_levelChip('critical'));
    await tester.pumpAndSettle();
    expect(api.lastLevel, 'critical');
    expect(api.lastStatus, 'acked');
    expect(api.lastTags, const <String>['关键']);
  });

  testWidgets('滚动分页：第一页 20 条，滑到底追加第二页并展示没有更多', (tester) async {
    final api = _FakeApiClient(
      alerts: List<AlertItem>.generate(
        25,
        (index) => _alert(
          'alt-${index.toString().padLeft(2, '0')}',
          'warning',
          'open',
        ),
      ),
    );
    await _pumpAlertCenter(tester, api: api);
    await tester.pumpAndSettle();

    expect(api.lastPage, 1);

    await tester.drag(find.byType(ListView), const Offset(0, -2000));
    await tester.pump();
    await tester.pumpAndSettle();

    expect(api.lastPage, 2);
    // 列表懒加载，页脚需滚动到底才会构建
    await tester.scrollUntilVisible(
      find.byKey(const ValueKey('alert-footer-end')),
      400,
      scrollable: find.byType(Scrollable).last,
    );
    expect(find.text('没有更多了'), findsOneWidget);

    // 第二页数据确实追加进列表
    await tester.scrollUntilVisible(
      find.byKey(const ValueKey('alert-row-alt-24')),
      400,
      scrollable: find.byType(Scrollable).last,
    );
    expect(find.byKey(const ValueKey('alert-row-alt-24')), findsOneWidget);
  });

  testWidgets('空态：无未处理告警时给出可读提示', (tester) async {
    final api = _FakeApiClient();
    await _pumpAlertCenter(tester, api: api);
    await tester.pumpAndSettle();
    expect(find.text('当前筛选下没有未处理告警'), findsOneWidget);
    expect(find.byType(ListView), findsNothing);
  });

  testWidgets('批量确认成功：一次请求提交多选，行移除且部分失败可见', (tester) async {
    final api = _FakeApiClient(
      alerts: <AlertItem>[
        _alert('alt-a', 'critical', 'open'),
        _alert('alt-b', 'warning', 'open'),
        _alert('alt-c', 'warning', 'open'),
      ],
    );
    await _pumpAlertCenter(tester, api: api);
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const ValueKey('select-alt-a')));
    await tester.tap(find.byKey(const ValueKey('select-alt-b')));
    await tester.pumpAndSettle();
    expect(find.text('已选择 2 条'), findsOneWidget);

    await tester.tap(find.byKey(const ValueKey('bulk-ack-button')));
    await tester.pumpAndSettle();
    expect(find.byType(AlertDialog), findsOneWidget);
    await tester.tap(find.byKey(const ValueKey('bulk-ack-confirm')));
    await tester.pumpAndSettle();

    expect(api.bulkCalls, 1);
    expect(api.lastBulkIds, <String>['alt-a', 'alt-b']);
    expect(find.byKey(const ValueKey('alert-row-alt-a')), findsNothing);
    expect(find.byKey(const ValueKey('alert-row-alt-b')), findsNothing);
    expect(find.byKey(const ValueKey('alert-row-alt-c')), findsOneWidget);
    expect(find.textContaining('已确认 2 条告警'), findsOneWidget);
  });

  testWidgets('批量确认备注：请求携带备注，确认后展示操作人/时间/备注', (tester) async {
    final api = _FakeApiClient(
      alerts: <AlertItem>[_alert('alt-n1', 'warning', 'open')],
    );
    await _pumpAlertCenter(tester, api: api);
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const ValueKey('select-alt-n1')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('bulk-ack-button')));
    await tester.pumpAndSettle();

    await tester.enterText(
      find.byKey(const ValueKey('bulk-ack-note')),
      '已联系现场，重启设备后恢复',
    );
    await tester.tap(find.byKey(const ValueKey('bulk-ack-confirm')));
    await tester.pumpAndSettle();

    expect(api.lastBulkNote, '已联系现场，重启设备后恢复');

    // 切到已确认视图，处置信息应完整展示（与 Web 列语义一致）
    await tester.tap(_statusChip('acked'));
    await tester.pumpAndSettle();
    expect(find.byKey(const ValueKey('ack-meta-alt-n1')), findsOneWidget);
    expect(find.textContaining('确认人 admin'), findsOneWidget);
    expect(find.byKey(const ValueKey('ack-note-alt-n1')), findsOneWidget);
    expect(find.textContaining('已联系现场，重启设备后恢复'), findsOneWidget);
  });

  testWidgets('历史已确认行：无处置元数据时显示可读说明', (tester) async {
    final api = _FakeApiClient(
      alerts: <AlertItem>[
        _alert('alt-hist', 'critical', 'acked', metric: 'voltage'),
      ],
    );
    await _pumpAlertCenter(tester, api: api);
    await tester.pumpAndSettle();

    await tester.tap(_statusChip('acked'));
    await tester.pumpAndSettle();
    expect(find.byKey(const ValueKey('ack-meta-alt-hist')), findsOneWidget);
    expect(find.text('历史确认（无处置记录）'), findsOneWidget);
    expect(find.byKey(const ValueKey('ack-note-alt-hist')), findsNothing);
  });

  testWidgets('级别口径：只呈现规则引擎真实产出的 warning / critical', (tester) async {
    final api = _FakeApiClient(
      alerts: <AlertItem>[_alert('alt-l1', 'warning', 'open')],
    );
    await _pumpAlertCenter(tester, api: api);
    await tester.pumpAndSettle();

    expect(_levelChip('warning'), findsOneWidget);
    expect(_levelChip('critical'), findsOneWidget);
    expect(_levelChip('info'), findsNothing);
    expect(_levelChip('notice'), findsNothing);
  });

  test('AlertItem.fromJson 解析处置元数据', () {
    final alert = AlertItem.fromJson(<String, dynamic>{
      'alert_id': 'alt-json',
      'device_id': 'dev-0001',
      'level': 'critical',
      'metric': 'voltage',
      'value': 2.9,
      'threshold': 3.2,
      'message': '电压过低',
      'ai_generated': false,
      'status': 'acked',
      'created_at': '2026-11-04T08:00:00Z',
      'ack_note': '  已校准探头  ',
      'acked_by': 'operator1',
      'acked_at': '2026-11-05T02:30:00Z',
    });
    expect(alert.ackNote, '已校准探头');
    expect(alert.ackedBy, 'operator1');
    expect(alert.ackedAt, isNotNull);
  });

  testWidgets('viewer 只读：可勾选但批量确认按钮禁用并说明原因', (tester) async {
    final api = _FakeApiClient(
      alerts: <AlertItem>[_alert('alt-v', 'warning', 'open')],
    );
    await _pumpAlertCenter(tester, api: api, session: _viewerSession);
    await tester.pumpAndSettle();

    expect(find.text('当前账号为只读（viewer），批量确认已禁用。'), findsOneWidget);
    await tester.tap(find.byKey(const ValueKey('select-alt-v')));
    await tester.pumpAndSettle();
    expect(find.text('已选择 1 条'), findsOneWidget);
    expect(find.text('只读账号不可确认'), findsOneWidget);

    final button = tester.widget<FilledButton>(
      find.byKey(const ValueKey('bulk-ack-button')),
    );
    expect(button.onPressed, isNull);
    expect(api.bulkCalls, 0);
  });

  testWidgets('服务端 403：提示并禁用本轮写操作，不崩溃', (tester) async {
    final api = _FakeApiClient(
      alerts: <AlertItem>[_alert('alt-403', 'warning', 'open')],
      bulkError: const ApiException(
        ApiErrorKind.forbidden,
        '当前账号无权访问该数据',
        statusCode: 403,
      ),
    );
    await _pumpAlertCenter(tester, api: api);
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('select-alt-403')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('bulk-ack-button')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('bulk-ack-confirm')));
    await tester.pumpAndSettle();

    expect(find.textContaining('本轮写操作已禁用'), findsWidgets);
    final button = tester.widget<FilledButton>(
      find.byKey(const ValueKey('bulk-ack-button')),
    );
    expect(button.onPressed, isNull);
  });

  testWidgets('设备筛选远程搜索：250 台设备仍能搜到第 200 台之后并提示缩小范围', (tester) async {
    final devices = List<DeviceSummary>.generate(250, (index) {
      final seq = (index + 1).toString().padLeft(4, '0');
      return DeviceSummary(
        deviceId: 'dev-$seq',
        name: '设备${index + 1}',
        model: 'VG-S100',
        firmwareVersion: '1.0.0',
        groupName: '默认分组',
        status: 'online',
      );
    });
    final api = _FakeApiClient(
      devices: devices,
      alerts: <AlertItem>[
        _alert('alt-x', 'warning', 'open', deviceId: 'dev-0200'),
      ],
    );
    await _pumpAlertCenter(tester, api: api);
    await tester.pumpAndSettle();

    // 首屏只为告警行名称缓存取 20 台，不再一次性拉 200 台。
    expect(api.lastDevicePageSize, 20);
    expect(api.lastDeviceKeyword, isNull);

    // 关键词命中第 200 台之后的设备。
    await tester.enterText(
      find.byKey(const ValueKey('device-search-field')),
      'dev-0200',
    );
    await tester.pump(const Duration(milliseconds: 350));
    await tester.pumpAndSettle();

    expect(api.lastDeviceKeyword, 'dev-0200');
    expect(api.lastDevicePageSize, 20);
    expect(find.byKey(const ValueKey('device-result-dev-0200')), findsOneWidget);
    expect(find.text('共 1 台匹配，已全部加载'), findsOneWidget);
    expect(find.byKey(const ValueKey('device-search-load-more')), findsNothing);

    await tester.tap(find.byKey(const ValueKey('device-result-dev-0200')));
    await tester.pumpAndSettle();
    expect(api.lastDeviceId, 'dev-0200');
    expect(find.byKey(const ValueKey('device-filter-active')), findsOneWidget);
  });

  testWidgets('设备搜索宽关键词：可继续加载更多，直到第 200 台之后', (tester) async {
    final devices = List<DeviceSummary>.generate(250, (index) {
      final seq = (index + 1).toString().padLeft(4, '0');
      return DeviceSummary(
        deviceId: 'dev-$seq',
        name: '设备${index + 1}',
        model: 'VG-S100',
        firmwareVersion: '1.0.0',
        groupName: '默认分组',
        status: 'online',
      );
    });
    final api = _FakeApiClient(devices: devices);
    await _pumpAlertCenter(tester, api: api);
    await tester.pumpAndSettle();

    await tester.enterText(
      find.byKey(const ValueKey('device-search-field')),
      'dev-',
    );
    await tester.pump(const Duration(milliseconds: 350));
    await tester.pumpAndSettle();

    // 第一页 20 台：如实展示总数与已加载数，并提供继续加载入口
    expect(
      find.text('共 250 台匹配，已加载 20 台；点击「加载更多」继续，也可输入更精确的关键词'),
      findsOneWidget,
    );
    expect(find.byKey(const ValueKey('device-result-dev-0250')), findsNothing);
    expect(
      find.text('加载更多（还有 230 台）'),
      findsOneWidget,
    );

    // 连续加载更多，能跨过服务端单页上限并到达第 200 台之后
    for (var i = 0; i < 12; i++) {
      final loadMore = find.byKey(const ValueKey('device-search-load-more'));
      if (loadMore.evaluate().isEmpty ||
          find.byKey(const ValueKey('device-result-dev-0201')).evaluate().isNotEmpty) {
        break;
      }
      await tester.ensureVisible(loadMore);
      await tester.pumpAndSettle();
      await tester.tap(loadMore);
      await tester.pumpAndSettle();
    }

    expect(find.byKey(const ValueKey('device-result-dev-0201')), findsOneWidget);
    expect(api.lastDeviceKeyword, 'dev-');
    expect(api.lastDevicePage, greaterThan(1));
    expect(
      find.textContaining('已加载 220 台'),
      findsOneWidget,
    );
  });

  testWidgets('批量确认 150 条：按 100 串行拆成 2 批并累加结果', (tester) async {
    tester.view.physicalSize = const Size(900, 20000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    final alerts = List<AlertItem>.generate(
      150,
      (index) => _alert(
        'alt-${index.toString().padLeft(3, '0')}',
        'warning',
        'open',
      ),
    );
    final api = _FakeApiClient(alerts: alerts, singlePageAlerts: true);
    await _pumpAlertCenter(tester, api: api);
    await tester.pumpAndSettle();

    for (var index = 0; index < 150; index += 1) {
      final id = 'alt-${index.toString().padLeft(3, '0')}';
      await tester.tap(find.byKey(ValueKey('select-$id')));
    }
    await tester.pumpAndSettle();
    expect(find.text('已选择 150 条'), findsOneWidget);

    await tester.tap(find.byKey(const ValueKey('bulk-ack-button')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('bulk-ack-confirm')));
    await tester.pumpAndSettle();

    expect(api.bulkCalls, 2);
    expect(api.bulkBatches.map((batch) => batch.length).toList(), <int>[100, 50]);
    expect(api.bulkBatches.first.first, 'alt-000');
    expect(api.bulkBatches.last.first, 'alt-100');
    expect(find.textContaining('已确认 150 条告警'), findsOneWidget);
    expect(find.textContaining('共分 2 批串行提交'), findsOneWidget);
  });

  testWidgets('批量确认某批失败：保留已成功部分并标出失败批次', (tester) async {
    tester.view.physicalSize = const Size(900, 20000);
    tester.view.devicePixelRatio = 1.0;
    addTearDown(tester.view.resetPhysicalSize);
    addTearDown(tester.view.resetDevicePixelRatio);

    final alerts = List<AlertItem>.generate(
      150,
      (index) => _alert(
        'alt-${index.toString().padLeft(3, '0')}',
        'warning',
        'open',
      ),
    );
    final api = _FakeApiClient(
      alerts: alerts,
      failBulkCall: 2,
      singlePageAlerts: true,
    );
    await _pumpAlertCenter(tester, api: api);
    await tester.pumpAndSettle();

    for (var index = 0; index < 150; index += 1) {
      final id = 'alt-${index.toString().padLeft(3, '0')}';
      await tester.tap(find.byKey(ValueKey('select-$id')));
    }
    await tester.pumpAndSettle();

    await tester.tap(find.byKey(const ValueKey('bulk-ack-button')));
    await tester.pumpAndSettle();
    await tester.tap(find.byKey(const ValueKey('bulk-ack-confirm')));
    await tester.pumpAndSettle();

    expect(api.bulkCalls, 2);
    expect(find.textContaining('已确认 100 条告警'), findsOneWidget);
    expect(find.textContaining('第 2 批共 50 条提交失败'), findsOneWidget);
    expect(find.textContaining('已成功部分保留'), findsOneWidget);
    // 第一批成功行已移除，第二批失败的 50 条仍在列表中（不回滚、不谎报）。
    expect(find.byKey(const ValueKey('alert-row-alt-000')), findsNothing);
    expect(find.byKey(const ValueKey('alert-row-alt-100')), findsOneWidget);
  });

  testWidgets('实时新告警插入列表顶部并累加未读回调', (tester) async {
    final realtime = _FakeRealtimeService();
    final api = _FakeApiClient(
      alerts: <AlertItem>[_alert('alt-old', 'warning', 'open')],
    );
    final unreadChanges = <int>[];
    await _pumpAlertCenter(
      tester,
      api: api,
      realtime: realtime,
      onUnreadChanged: unreadChanges.add,
    );
    await tester.pumpAndSettle();

    final live = _alert('alt-live', 'critical', 'open', value: 99);
    realtime.emit(AlertEvent(live));
    await tester.pumpAndSettle();

    expect(find.byKey(const ValueKey('alert-row-alt-live')), findsOneWidget);
    expect(find.byKey(const ValueKey('alert-row-alt-old')), findsOneWidget);
    expect(unreadChanges.last, greaterThan(0));

    // 新告警应位于旧告警之前（列表顶部）
    final liveTop = tester.getTopLeft(
      find.byKey(const ValueKey('alert-row-alt-live')),
    );
    final oldTop = tester.getTopLeft(
      find.byKey(const ValueKey('alert-row-alt-old')),
    );
    expect(liveTop.dy, lessThan(oldTop.dy));
  });

  testWidgets('HomeShell 角标：新告警累加，进入告警中心后清零', (tester) async {
    final realtime = _FakeRealtimeService();
    final api = _FakeApiClient();
    final storage = await _storageWith(_adminSession);
    await tester.pumpWidget(
      MaterialApp(
        home: HomeShell(
          storage: storage,
          apiClientFactory: (_) => api,
          realtimeServiceFactory: (_) => realtime,
        ),
      ),
    );
    await tester.pumpAndSettle();

    // v1.27.0：默认 tab 为「概览」，工具 tab 保留设备模拟器入口
    expect(find.text('工具'), findsOneWidget);
    expect(find.text('概览'), findsWidgets);
    realtime.emit(AlertEvent(_alert('alt-badge', 'critical', 'open')));
    await tester.pumpAndSettle();
    final badge = tester.widget<Badge>(
      find.byKey(const ValueKey('alert-tab-badge')),
    );
    expect(badge.isLabelVisible, isTrue);
    expect((badge.label as Text?)?.data, '1');

    await tester.tap(find.text('告警中心'));
    await tester.pumpAndSettle();
    final selectedBadge = tester.widget<Badge>(
      find.ancestor(
        of: find.byIcon(Icons.notifications),
        matching: find.byType(Badge),
      ),
    );
    expect(selectedBadge.isLabelVisible, isFalse);
  });
}
