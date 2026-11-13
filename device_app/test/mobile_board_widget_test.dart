import 'dart:async';

import 'package:easy_refresh/easy_refresh.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:iot_device_app/models/mobile_api_models.dart';
import 'package:iot_device_app/models/realtime_payload.dart';
import 'package:iot_device_app/pages/mobile_board_page.dart';
import 'package:iot_device_app/services/api_client.dart';
import 'package:iot_device_app/services/app_storage.dart';
import 'package:iot_device_app/services/board_realtime_service.dart';
import 'package:shared_preferences/shared_preferences.dart';

const DeviceSummary _device = DeviceSummary(
  deviceId: 'dev-0001',
  name: '一号机',
  model: 'Battery-Sensor',
  firmwareVersion: '1.0.0',
  groupName: '默认分组',
  status: 'online',
);

const SensorReading _reading = SensorReading(
  deviceId: 'dev-0001',
  temperature: 26.5,
  humidity: 55,
  voltage: 3.8,
  rssi: -60,
  battery: 88,
  seq: 1,
  ts: null,
);

const AlertItem _openAlert = AlertItem(
  alertId: 'alt-0001',
  deviceId: 'dev-0001',
  level: 'critical',
  metric: 'temperature',
  value: 80,
  threshold: 75,
  message: '温度过高',
  aiGenerated: false,
  status: 'open',
);

const AlertItem _secondAlert = AlertItem(
  alertId: 'alt-0002',
  deviceId: 'dev-0002',
  level: 'warning',
  metric: 'humidity',
  value: 90,
  threshold: 85,
  message: '湿度过高',
  aiGenerated: false,
  status: 'open',
);

const AlertItem _ackedAlert = AlertItem(
  alertId: 'alt-0001',
  deviceId: 'dev-0001',
  level: 'critical',
  metric: 'temperature',
  value: 80,
  threshold: 75,
  message: '温度过高',
  aiGenerated: false,
  status: 'acked',
);

const CommandItem _issuedCommand = CommandItem(
  cmdId: 'cmd-0001',
  type: 'config',
  state: 'issued',
  progress: 0,
  message: '指令已下发，等待设备回执',
  operator: 'admin',
);

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

const DeviceSummary _secondDevice = DeviceSummary(
  deviceId: 'dev-0002',
  name: '二号机',
  model: 'Battery-Sensor',
  firmwareVersion: '1.0.0',
  groupName: '默认分组',
  status: 'online',
);

const DeviceSummary _offlineDevice = DeviceSummary(
  deviceId: 'dev-0003',
  name: '三号机',
  model: 'Battery-Sensor',
  firmwareVersion: '1.0.0',
  groupName: '默认分组',
  status: 'offline',
);

const SensorReading _secondReading = SensorReading(
  deviceId: 'dev-0002',
  temperature: 21.0,
  humidity: 48,
  voltage: 3.7,
  rssi: -62,
  battery: 79,
  seq: 2,
  ts: null,
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

/// 记录调用次数并可注入失败的 REST 客户端，避免测试发出真实网络请求。
class _FakeApiClient extends ApiClient {
  _FakeApiClient({
    this.fetchError,
    this.ackError,
    List<DeviceSummary>? devices,
    List<SensorReading>? readings,
    List<AlertItem>? alerts,
    List<SensorReading>? records,
    List<DeviceShadowRevision>? shadowRevisions,
    this.stats,
  }) : devices = List<DeviceSummary>.of(
         devices ?? const <DeviceSummary>[_device],
       ),
       readings = readings ?? const <SensorReading>[_reading],
       alerts = alerts ?? const <AlertItem>[_openAlert],
       records = records ?? const <SensorReading>[],
       shadowRevisions =
           shadowRevisions ??
           <DeviceShadowRevision>[
             DeviceShadowRevision(
               id: 8,
               deviceId: 'dev-0001',
               version: 7,
               desired: const <String, dynamic>{
                 'report_interval': 30,
                 'led': true,
               },
               changedKeys: const <String>['led'],
               action: 'rollback',
               sourceVersion: 6,
               createdAt: null,
               createdBy: 'operator',
             ),
             DeviceShadowRevision(
               id: 7,
               deviceId: 'dev-0001',
               version: 6,
               desired: const <String, dynamic>{
                 'report_interval': 30,
                 'led': false,
               },
               changedKeys: const <String>['report_interval'],
               action: 'merge',
               createdBy: 'operator',
             ),
           ],
       super(baseUrl: 'http://192.168.1.8:8000');

  /// 仅在对应用例注入；为空时返回固定测试数据。
  final Object? fetchError;
  final Object? ackError;
  final List<DeviceSummary> devices;
  final List<SensorReading> readings;
  final List<AlertItem> alerts;

  /// 详情页历史上报：按网关语义（时间倒序取切片后翻转正序）返回。
  final List<SensorReading> records;
  final List<DeviceShadowRevision> shadowRevisions;
  final StatsOverview? stats;

  int loginCalls = 0;
  int fetchCalls = 0;
  int groupFetchCalls = 0;
  int sendCalls = 0;
  int ackCalls = 0;
  int credentialIssueCalls = 0;
  int credentialRevokeCalls = 0;
  bool credentialReloaded = true;
  String credentialPassword = 'cred-one-time-password';
  Object? credentialError;
  String lastCredentialOperation = '';

  /// 最近一次设备请求参数，用于断言搜索/筛选已转为服务端参数。
  int lastPage = 0;
  String? lastKeyword;
  String? lastStatus;
  String? lastGroup;
  List<String>? lastTags;
  bool lastHasAlert = false;
  int alertFetchCalls = 0;
  int recordFetchCalls = 0;
  int lastRecordLimit = 0;
  int lastRecordOffset = 0;
  final List<int> recordOffsets = <int>[];
  int shadowHistoryFetchCalls = 0;
  int lastShadowHistoryLimit = 0;
  int lastShadowHistoryOffset = 0;

  @override
  Future<AuthSession> login({
    required String username,
    required String password,
  }) async {
    loginCalls += 1;
    return _adminSession;
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
    fetchCalls += 1;
    lastPage = page;
    lastKeyword = keyword;
    lastStatus = status;
    lastGroup = group;
    lastTags = tags;
    lastHasAlert = hasAlert;
    final error = fetchError;
    if (error != null) {
      throw error;
    }

    // 模拟网关：关键词 / 状态 / 有告警均在服务端过滤，再按 page/pageSize 切片
    var filtered = devices;
    if (keyword != null && keyword.isNotEmpty) {
      final needle = keyword.toLowerCase();
      filtered = filtered
          .where(
            (item) =>
                item.deviceId.toLowerCase().contains(needle) ||
                item.name.toLowerCase().contains(needle),
          )
          .toList(growable: false);
    }
    if (status == 'online') {
      filtered = filtered.where((item) => item.status == 'online').toList();
    } else if (status == 'offline') {
      filtered = filtered.where((item) => item.status != 'online').toList();
    }
    if (group != null && group.isNotEmpty) {
      filtered = filtered
          .where((item) => _groupNameOf(item) == group)
          .toList(growable: false);
    }
    if (tags != null && tags.isNotEmpty) {
      // 网关语义：多个标签之间为「或」
      filtered = filtered
          .where((item) => item.tags.any(tags.contains))
          .toList(growable: false);
    }
    if (hasAlert) {
      final alertIds = alerts
          .where((item) => item.status == 'open')
          .map((item) => item.deviceId)
          .toSet();
      filtered = filtered
          .where(
            (item) =>
                item.openAlertCount > 0 || alertIds.contains(item.deviceId),
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
    final stamped = withAlertCount
        ? items
              .map((item) {
                final count = alerts
                    .where(
                      (alert) =>
                          alert.status == 'open' &&
                          alert.deviceId == item.deviceId,
                    )
                    .length;
                return item.copyWith(
                  openAlertCount: item.openAlertCount > count
                      ? item.openAlertCount
                      : count,
                );
              })
              .toList(growable: false)
        : items;
    return DevicePage(
      items: stamped,
      total: total,
      page: page,
      pageSize: pageSize,
    );
  }

  /// 模拟后台改分组：先改服务端数据，再让看板通过实时事件/刷新看到结果。
  void setDeviceGroup(String deviceId, String groupName) {
    final index = devices.indexWhere((item) => item.deviceId == deviceId);
    if (index >= 0) {
      devices[index] = devices[index].copyWith(groupName: groupName);
    }
  }

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
  Future<List<DeviceGroup>> fetchGroups({required String token}) async {
    groupFetchCalls += 1;
    final byName = <String, DeviceGroup>{};
    for (final device in devices) {
      final name = _groupNameOf(device);
      final previous = byName[name];
      final online = device.status == 'online' ? 1 : 0;
      byName[name] = DeviceGroup(
        groupName: name,
        total: (previous?.total ?? 0) + 1,
        online: (previous?.online ?? 0) + online,
        offline: (previous?.offline ?? 0) + (1 - online),
      );
    }
    final groups = byName.values.toList();
    groups.sort((a, b) => a.groupName.compareTo(b.groupName));
    return groups;
  }

  @override
  Future<List<SensorReading>> fetchLatestReadings({
    required String token,
    List<String>? deviceIds,
  }) async {
    if (deviceIds == null) {
      return readings;
    }
    if (deviceIds.isEmpty) {
      return const <SensorReading>[];
    }
    return readings
        .where((item) => deviceIds.contains(item.deviceId))
        .toList(growable: false);
  }

  @override
  Future<AlertPage> fetchOpenAlerts({
    required String token,
    int page = 1,
    int pageSize = 20,
  }) async {
    alertFetchCalls += 1;
    final open = alerts.where((item) => item.status == 'open').toList();
    final total = open.length;
    final start = (page - 1) * pageSize;
    final items = start >= total
        ? const <AlertItem>[]
        : open.sublist(
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

  String _groupNameOf(DeviceSummary device) {
    final name = device.groupName.trim();
    return name.isEmpty ? '未分组' : name;
  }

  @override
  Future<StatsOverview> fetchStatsOverview({required String token}) async =>
      stats ??
      StatsOverview(
        deviceTotal: devices.length,
        deviceOnline: devices.where((item) => item.status == 'online').length,
        deviceOffline: devices.where((item) => item.status != 'online').length,
        reportToday: 0,
        alertOpen: alerts.where((item) => item.status == 'open').length,
      );

  @override
  Future<DeviceShadow> fetchDeviceShadow({
    required String token,
    required String deviceId,
  }) async => const DeviceShadow(
    deviceId: 'dev-0001',
    desired: <String, dynamic>{'report_interval': 30, 'led': true},
    reported: <String, dynamic>{'report_interval': 10, 'led': true},
    version: 7,
    reportedVersion: 6,
    syncStatus: 'pending',
    diffKeys: <String>['report_interval'],
    pendingDesired: <String, dynamic>{'report_interval': 30},
  );

  @override
  Future<DeviceShadowHistory> fetchDeviceShadowHistory({
    required String token,
    required String deviceId,
    int limit = 20,
    int offset = 0,
  }) async {
    shadowHistoryFetchCalls += 1;
    lastShadowHistoryLimit = limit;
    lastShadowHistoryOffset = offset;
    final end = offset + limit > shadowRevisions.length
        ? shadowRevisions.length
        : offset + limit;
    return DeviceShadowHistory(
      items: offset >= shadowRevisions.length
          ? const <DeviceShadowRevision>[]
          : shadowRevisions.sublist(offset, end),
      total: shadowRevisions.length,
      limit: limit,
      offset: offset,
    );
  }

  @override
  Future<List<CommandItem>> fetchCommands({
    required String token,
    required String deviceId,
  }) async => const <CommandItem>[];

  @override
  Future<List<SensorReading>> fetchDeviceRecords({
    required String token,
    required String deviceId,
    int limit = 10,
    int offset = 0,
  }) async {
    recordFetchCalls += 1;
    lastRecordLimit = limit;
    lastRecordOffset = offset;
    recordOffsets.add(offset);
    // 模拟网关：按时间倒序跳过 offset 取 limit 条，再翻转成时间正序
    final owned = records
        .where((item) => item.deviceId == deviceId)
        .toList(growable: false);
    final page = owned.reversed.skip(offset).take(limit).toList();
    return page.reversed.toList(growable: false);
  }

  @override
  Future<CommandItem> sendCommand({
    required String token,
    required String deviceId,
    required String type,
    required Map<String, dynamic> payload,
  }) async {
    sendCalls += 1;
    return _issuedCommand;
  }

  @override
  Future<AlertItem> ackAlert({
    required String token,
    required String alertId,
  }) async {
    ackCalls += 1;
    final error = ackError;
    if (error != null) {
      throw error;
    }
    return _ackedAlert;
  }

  @override
  Future<DeviceCredentialResult> issueDeviceCredential({
    required String token,
    required String deviceId,
  }) async {
    credentialIssueCalls += 1;
    final error = credentialError;
    if (error != null) {
      throw error;
    }
    final index = devices.indexWhere((item) => item.deviceId == deviceId);
    final currentlyActive =
        index >= 0 && devices[index].mqttCredentialStatus == 'active';
    final operation = currentlyActive ? 'rotate' : 'issue';
    lastCredentialOperation = operation;
    if (index >= 0) {
      final current = devices[index];
      devices[index] = current.copyWith(
        mqttCredentialVersion: current.mqttCredentialVersion + 1,
        mqttCredentialIssuedAt:
            current.mqttCredentialIssuedAt ?? DateTime.utc(2026, 11, 5),
        mqttCredentialRotatedAt: DateTime.utc(2026, 11, 5, 9, 30),
        mqttCredentialStatus: 'active',
        mqttCredentialFingerprintPrefix: 'abc12345',
      );
    }
    return DeviceCredentialResult(
      deviceId: deviceId,
      username: deviceId,
      password: credentialPassword,
      version: index >= 0 ? devices[index].mqttCredentialVersion : 1,
      operation: operation,
      reloaded: credentialReloaded,
      reloadDetail: credentialReloaded
          ? 'Broker reloaded via SIGHUP'
          : 'reload command failed: exit code 1',
      rotatedAt: DateTime.utc(2026, 11, 5, 9, 30),
    );
  }

  @override
  Future<DeviceCredentialRevokeResult> revokeDeviceCredential({
    required String token,
    required String deviceId,
  }) async {
    credentialRevokeCalls += 1;
    final error = credentialError;
    if (error != null) {
      throw error;
    }
    final index = devices.indexWhere((item) => item.deviceId == deviceId);
    if (index >= 0) {
      devices[index] = devices[index].copyWith(
        mqttCredentialStatus: 'revoked',
        mqttCredentialRevokedAt: DateTime.utc(2026, 11, 5, 9, 40),
      );
    }
    return DeviceCredentialRevokeResult(
      deviceId: deviceId,
      username: deviceId,
      version: index >= 0 ? devices[index].mqttCredentialVersion : 1,
      revokedAt: DateTime.utc(2026, 11, 5, 9, 40),
      reloaded: credentialReloaded,
      reloadDetail: credentialReloaded
          ? 'Broker reloaded via SIGHUP'
          : 'reload command failed: exit code 1',
    );
  }

  @override
  void close() {}
}

/// 假实时服务：不发起 MQTT 连接，只按指定状态驱动看板 UI。
class _FakeRealtimeService extends BoardRealtimeService {
  _FakeRealtimeService(this.initialState)
    : super(
        url: 'ws://192.168.1.8:9001',
        transportFactory: (_, _) => throw StateError('测试不应创建真实连接'),
      );

  final RealtimeState initialState;
  final StreamController<RealtimeEvent> _controller =
      StreamController<RealtimeEvent>.broadcast();

  @override
  Stream<RealtimeEvent> get events => _controller.stream;

  /// 下拉刷新不应重建订阅；测试据此断言实时流没被打断。
  bool get hasSubscriber => _controller.hasListener;

  /// 模拟从 MQTT 收到事件，用于验证筛选期间实时数据仍然落盘。
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
    await _controller.close();
  }
}

Future<AppStorage> _pumpBoard(
  WidgetTester tester, {
  required _FakeApiClient api,
  AuthSession? session,
  RealtimeState realtimeState = RealtimeState.connected,
  _FakeRealtimeService? realtime,
}) async {
  final storage = AppStorage(secureStore: _FakeSecureStore());
  if (session != null) {
    await storage.saveSession(session);
  }
  final service = realtime ?? _FakeRealtimeService(realtimeState);
  await tester.pumpWidget(
    MaterialApp(
      home: MobileBoardPage(
        storage: storage,
        apiClientFactory: (_) => api,
        realtimeServiceFactory: (_) => service,
      ),
    ),
  );
  return storage;
}

Future<void> _openDeviceSheet(WidgetTester tester) async {
  await _scrollTo(tester, find.text('一号机'));
  await tester.tap(find.text('一号机'));
  await tester.pumpAndSettle();
}

/// 列表较长（新增筛选面板后告警卡常在首屏之外），先滚动到目标再操作。
Future<void> _scrollTo(WidgetTester tester, Finder finder) async {
  await tester.scrollUntilVisible(
    finder,
    240,
    scrollable: find.byType(Scrollable).first,
  );
  await tester.ensureVisible(finder);
  await tester.pumpAndSettle();
}

/// 构造历史上报：温度逐条递增（文案唯一），时间随序号递增。
List<SensorReading> _recordBatch(int count) => List<SensorReading>.generate(
  count,
  (index) => SensorReading(
    deviceId: 'dev-0001',
    temperature: 20.0 + index,
    humidity: 50,
    voltage: 3.8,
    rssi: -60,
    battery: 80,
    seq: index + 1,
    ts: DateTime.utc(2026, 11, 4).add(Duration(minutes: index)),
  ),
);

/// 详情弹层的 ListView（看板列表 padding 为 12，弹层为 16/0/16/16，可据此唯一定位）。
final Finder _sheetList = find.byWidgetPredicate(
  (widget) =>
      widget is ListView &&
      widget.padding == const EdgeInsets.fromLTRB(16, 0, 16, 16),
);

/// 单独滚动详情弹层到目标控件，避免误滚看板列表。
Future<void> _scrollSheetTo(WidgetTester tester, Finder finder) async {
  await tester.scrollUntilVisible(
    finder,
    200,
    scrollable: find.descendant(
      of: _sheetList,
      matching: find.byType(Scrollable),
    ),
  );
  await tester.pumpAndSettle();
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUp(() {
    SharedPreferences.setMockInitialValues(<String, Object>{});
  });

  testWidgets('未登录显示登录页，登录成功后渲染设备与告警', (tester) async {
    final api = _FakeApiClient();
    await _pumpBoard(tester, api: api);
    await tester.pumpAndSettle();

    expect(find.text('移动看板 · 登录'), findsOneWidget);
    expect(find.text('一号机'), findsNothing);

    await tester.enterText(find.byType(TextField).last, 'admin123');
    await tester.tap(find.text('登录并进入看板'));
    await tester.pumpAndSettle();

    expect(api.loginCalls, 1);
    expect(find.text('移动看板'), findsOneWidget);
    expect(find.text('一号机'), findsOneWidget);
    expect(find.text('admin · admin'), findsOneWidget);
    // 分页页脚后告警区可能落在首屏之外，滚动后再断言
    await _scrollTo(tester, find.text('温度过高'));
    expect(find.text('温度过高'), findsOneWidget);
  });

  testWidgets('指令下发：填写 → 二次确认 → 展示回执', (tester) async {
    final api = _FakeApiClient();
    await _pumpBoard(tester, api: api, session: _adminSession);
    await tester.pumpAndSettle();
    await _openDeviceSheet(tester);

    // 弹窗打开期间详情页按钮处于「提交中」动画，使用定长 pump 而非 settle
    await tester.tap(find.text('下发指令'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));
    await tester.tap(find.text('下一步'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 200));

    // 二次确认弹窗必须展示目标设备与参数摘要
    expect(find.byType(AlertDialog), findsOneWidget);
    expect(find.widgetWithText(FilledButton, '确认下发'), findsOneWidget);
    expect(find.text('一号机（dev-0001）'), findsOneWidget);
    expect(find.text('上报间隔 30 秒 · 休眠 15 秒'), findsOneWidget);

    await tester.tap(find.widgetWithText(FilledButton, '确认下发'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 200));

    expect(api.sendCalls, 1);
    expect(find.text('指令已提交'), findsOneWidget);
    expect(find.text('cmd-0001'), findsOneWidget);
    expect(find.text('指令已下发，等待设备回执'), findsOneWidget);

    await tester.tap(find.text('完成'));
    await tester.pumpAndSettle();
    expect(find.text('指令已下发：cmd-0001（issued）'), findsOneWidget);

    // 清掉 SnackBar 计时器，避免测试结束时留下 pending timer
    await tester.pump(const Duration(seconds: 5));
  });

  testWidgets('指令二次确认点取消不发起请求', (tester) async {
    final api = _FakeApiClient();
    await _pumpBoard(tester, api: api, session: _adminSession);
    await tester.pumpAndSettle();
    await _openDeviceSheet(tester);

    await tester.tap(find.text('下发指令'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 400));
    await tester.tap(find.text('下一步'));
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 200));
    expect(find.widgetWithText(FilledButton, '确认下发'), findsOneWidget);

    await tester.tap(find.widgetWithText(TextButton, '取消'));
    await tester.pumpAndSettle();

    expect(api.sendCalls, 0);
    expect(find.byType(AlertDialog), findsNothing);
    expect(find.text('一号机'), findsWidgets);
  });

  testWidgets('告警确认成功后从未处理列表移除', (tester) async {
    final api = _FakeApiClient();
    await _pumpBoard(tester, api: api, session: _adminSession);
    await tester.pumpAndSettle();
    // 分组筛选行使告警区下移，先滚动到告警区再断言
    await _scrollTo(tester, find.text('未处理告警（1）'));
    expect(find.text('未处理告警（1）'), findsOneWidget);

    await _scrollTo(tester, find.widgetWithText(TextButton, '确认告警'));
    await tester.tap(find.widgetWithText(TextButton, '确认告警'));
    await tester.pumpAndSettle();
    expect(find.widgetWithText(AlertDialog, '确认告警'), findsOneWidget);

    await tester.tap(find.widgetWithText(FilledButton, '确认'));
    await tester.pumpAndSettle();

    expect(api.ackCalls, 1);
    expect(find.text('温度过高'), findsNothing);
    expect(find.text('未处理告警（0）'), findsOneWidget);
    expect(find.text('告警已确认'), findsOneWidget);

    await tester.pump(const Duration(seconds: 5));
  });

  testWidgets('viewer 角色禁用写按钮并给出只读提示', (tester) async {
    final api = _FakeApiClient();
    await _pumpBoard(tester, api: api, session: _viewerSession);
    await tester.pumpAndSettle();

    expect(find.text('当前账号为只读（viewer），指令下发与告警确认已禁用'), findsOneWidget);
    await _scrollTo(tester, find.widgetWithText(TextButton, '确认告警'));
    final ackButton = tester.widget<TextButton>(
      find.widgetWithText(TextButton, '确认告警'),
    );
    expect(ackButton.onPressed, isNull);

    // 回到顶部再打开设备详情，避免列表滚动导致设备卡不可点击
    await tester.drag(find.byType(ListView).first, const Offset(0, 800));
    await tester.pumpAndSettle();
    await _openDeviceSheet(tester);
    expect(find.text('当前无写权限'), findsOneWidget);
    final sendButton = tester.widget<FilledButton>(
      find.widgetWithText(FilledButton, '下发指令'),
    );
    expect(sendButton.onPressed, isNull);
  });

  testWidgets('服务端 403 时提示可读文案并禁用写操作', (tester) async {
    final api = _FakeApiClient(
      ackError: const ApiException(
        ApiErrorKind.forbidden,
        '当前账号无权访问该数据',
        statusCode: 403,
      ),
    );
    await _pumpBoard(tester, api: api, session: _adminSession);
    await tester.pumpAndSettle();

    await _scrollTo(tester, find.widgetWithText(TextButton, '确认告警'));
    await tester.tap(find.widgetWithText(TextButton, '确认告警'));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(FilledButton, '确认'));
    await tester.pumpAndSettle();

    expect(find.text('温度过高'), findsOneWidget);
    expect(find.text('服务端返回 403：当前账号无权确认告警'), findsWidgets);
    final ackButton = tester.widget<TextButton>(
      find.widgetWithText(TextButton, '确认告警'),
    );
    expect(ackButton.onPressed, isNull);

    await tester.pump(const Duration(seconds: 5));
  });

  testWidgets('实时断开时显示已切换轮询', (tester) async {
    final api = _FakeApiClient();
    await _pumpBoard(
      tester,
      api: api,
      session: _adminSession,
      realtimeState: RealtimeState.reconnecting,
    );
    await tester.pump();
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 50));

    expect(find.textContaining('已切换轮询'), findsOneWidget);
    expect(find.textContaining('实时重连中'), findsOneWidget);

    // 主动卸载页面，取消 15 秒轮询定时器
    await tester.pumpWidget(const SizedBox());
    await tester.pump();
  });

  testWidgets('401 清除 token 并回到登录页', (tester) async {
    final api = _FakeApiClient(
      fetchError: const ApiException(
        ApiErrorKind.unauthorized,
        '登录已失效',
        statusCode: 401,
      ),
    );
    final storage = await _pumpBoard(tester, api: api, session: _adminSession);
    await tester.pumpAndSettle();

    expect(find.text('移动看板 · 登录'), findsOneWidget);
    expect(find.text('登录已失效，请重新登录'), findsOneWidget);
    expect(await storage.loadSession(), isNull);
  });

  testWidgets('搜索按设备名或 ID 过滤，汇总卡展示筛选结果', (tester) async {
    final api = _FakeApiClient(devices: const [_device, _secondDevice]);
    await _pumpBoard(tester, api: api, session: _adminSession);
    await tester.pumpAndSettle();
    expect(find.text('一号机'), findsOneWidget);
    expect(find.text('二号机'), findsOneWidget);

    // 按设备 ID 搜索：输入防抖 300ms 后只剩命中设备
    await tester.enterText(find.byType(TextField), 'dev-0002');
    await tester.pump(const Duration(milliseconds: 400));
    await tester.pumpAndSettle();

    expect(find.text('一号机'), findsNothing);
    expect(find.text('二号机'), findsOneWidget);
    expect(find.text('筛选结果：1 台 / 全部 2 台'), findsOneWidget);
    // 汇总卡仍按全量口径统计，不因筛选清空
    expect(find.text('设备总数'), findsOneWidget);
    expect(find.text('未处理告警'), findsOneWidget);

    // 再按名称搜索，验证名称也可命中
    await tester.enterText(find.byType(TextField), '一号');
    await tester.pump(const Duration(milliseconds: 400));
    await tester.pumpAndSettle();
    expect(find.text('一号机'), findsOneWidget);
    expect(find.text('二号机'), findsNothing);
  });

  testWidgets('状态筛选联动列表与汇总卡', (tester) async {
    final api = _FakeApiClient(devices: const [_device, _offlineDevice]);
    await _pumpBoard(tester, api: api, session: _adminSession);
    await tester.pumpAndSettle();

    await tester.tap(find.widgetWithText(ChoiceChip, '离线'));
    await tester.pumpAndSettle();
    expect(find.text('三号机'), findsOneWidget);
    expect(find.text('一号机'), findsNothing);
    expect(find.text('筛选结果：1 台 / 全部 2 台'), findsOneWidget);

    await tester.tap(find.widgetWithText(ChoiceChip, '有告警'));
    await tester.pumpAndSettle();
    expect(find.text('一号机'), findsOneWidget);
    expect(find.text('三号机'), findsNothing);

    await tester.tap(find.widgetWithText(ChoiceChip, '全部'));
    await tester.pumpAndSettle();
    expect(find.text('一号机'), findsOneWidget);
    expect(find.text('三号机'), findsOneWidget);
    expect(find.text('全部 2 台设备，实时值随 MQTT 增量更新'), findsOneWidget);
  });

  testWidgets('筛选无结果展示可读空状态且不清空汇总卡', (tester) async {
    final api = _FakeApiClient(devices: const [_device, _secondDevice]);
    await _pumpBoard(tester, api: api, session: _adminSession);
    await tester.pumpAndSettle();

    await tester.enterText(find.byType(TextField), '不存在的设备');
    await tester.pump(const Duration(milliseconds: 400));
    await tester.pumpAndSettle();

    expect(find.text('没有匹配的设备'), findsOneWidget);
    expect(find.textContaining('可清空关键词或切回「全部」'), findsOneWidget);
    expect(find.text('一号机'), findsNothing);
    expect(find.text('二号机'), findsNothing);
    expect(find.text('筛选结果：0 台 / 全部 2 台'), findsOneWidget);
    // 汇总卡三项统计仍在，避免误以为设备被清空
    expect(find.text('设备总数'), findsOneWidget);
    expect(find.text('在线'), findsWidgets);
  });

  testWidgets('服务端分页：滚动到底自动加载下一页并显示没有更多了', (tester) async {
    final devices = List<DeviceSummary>.generate(25, (index) {
      final id = 'dev-${index.toString().padLeft(2, '0')}';
      return DeviceSummary(
        deviceId: id,
        name: '设备 $id',
        model: 'Battery-Sensor',
        firmwareVersion: '1.0.0',
        groupName: '默认分组',
        status: 'online',
      );
    });
    final api = _FakeApiClient(devices: devices);
    await _pumpBoard(tester, api: api, session: _adminSession);
    await tester.pumpAndSettle();

    // 首屏只请求第 1 页（20 台），不再一次拉 100 台
    expect(api.fetchCalls, 1);
    expect(api.lastPage, 1);
    expect(find.text('设备 dev-20'), findsNothing);

    // 页脚进入视口即自动请求第 2 页，无需手动点击
    for (var i = 0; i < 12 && api.lastPage == 1; i++) {
      await tester.drag(find.byType(ListView).first, const Offset(0, -400));
      await tester.pumpAndSettle();
    }

    expect(api.lastPage, 2);
    expect(api.fetchCalls, greaterThanOrEqualTo(2));

    // 第二页追加后页脚下移，继续下拉到末页
    for (var i = 0; i < 6; i++) {
      await tester.drag(find.byType(ListView).first, const Offset(0, -400));
      await tester.pumpAndSettle();
    }
    expect(find.text('没有更多了'), findsOneWidget);
    expect(find.text('设备 dev-24'), findsOneWidget);
  });

  testWidgets('搜索重置分页：已翻页后回到第 1 页', (tester) async {
    final devices = List<DeviceSummary>.generate(25, (index) {
      final id = 'dev-${index.toString().padLeft(2, '0')}';
      return DeviceSummary(
        deviceId: id,
        name: '设备 $id',
        model: 'Battery-Sensor',
        firmwareVersion: '1.0.0',
        groupName: '默认分组',
        status: 'online',
      );
    });
    final api = _FakeApiClient(devices: devices);
    await _pumpBoard(tester, api: api, session: _adminSession);
    await tester.pumpAndSettle();

    for (var i = 0; i < 12 && api.lastPage == 1; i++) {
      await tester.drag(find.byType(ListView).first, const Offset(0, -400));
      await tester.pumpAndSettle();
    }
    expect(api.lastPage, 2);

    // 回到顶部搜索：请求参数变为 keyword，且 page 重置为 1
    await tester.drag(find.byType(ListView).first, const Offset(0, 3000));
    await tester.pumpAndSettle();
    await tester.enterText(find.byType(TextField), 'dev-24');
    await tester.pump(const Duration(milliseconds: 400));
    await tester.pumpAndSettle();

    expect(api.lastPage, 1);
    expect(api.lastKeyword, 'dev-24');
    expect(find.text('设备 dev-24'), findsOneWidget);
    expect(find.text('设备 dev-00'), findsNothing);
    expect(find.text('筛选结果：1 台 / 全部 25 台'), findsOneWidget);
    expect(find.text('没有更多了'), findsOneWidget);
  });

  testWidgets('下拉刷新：重置到第 1 页并保留搜索关键词，实时流不重建', (tester) async {
    final devices = List<DeviceSummary>.generate(25, (index) {
      final id = 'dev-${index.toString().padLeft(2, '0')}';
      return DeviceSummary(
        deviceId: id,
        name: '设备 $id',
        model: 'Battery-Sensor',
        firmwareVersion: '1.0.0',
        groupName: '默认分组',
        status: 'online',
      );
    });
    final realtime = _FakeRealtimeService(RealtimeState.connected);
    final api = _FakeApiClient(devices: devices);
    await _pumpBoard(
      tester,
      api: api,
      session: _adminSession,
      realtime: realtime,
    );
    await tester.pumpAndSettle();
    expect(realtime.hasSubscriber, isTrue);

    // 关键词命中全部设备，便于先翻页再验证筛选被保留
    await tester.enterText(find.byType(TextField), '设备');
    await tester.pump(const Duration(milliseconds: 400));
    await tester.pumpAndSettle();
    expect(api.lastKeyword, '设备');

    for (var i = 0; i < 12 && api.lastPage == 1; i++) {
      await tester.drag(find.byType(ListView).first, const Offset(0, -400));
      await tester.pumpAndSettle();
    }
    expect(api.lastPage, 2);

    await tester.drag(find.byType(ListView).first, const Offset(0, 3000));
    await tester.pumpAndSettle();
    unawaited(
      tester
          .widget<EasyRefresh>(find.byType(EasyRefresh))
          .controller!
          .callRefresh(),
    );
    await tester.pumpAndSettle();
    // easy_refresh 收尾计时器，避免测试结束时仍挂起
    await tester.pump(const Duration(seconds: 1));
    await tester.pumpAndSettle();

    expect(api.lastPage, 1);
    expect(api.lastKeyword, '设备');
    expect(realtime.hasSubscriber, isTrue);
    expect(find.text('设备 dev-00'), findsOneWidget);
    expect(find.text('设备 dev-24'), findsNothing);
  });

  testWidgets('有告警筛选使用服务端聚合计数，角标与结果一致', (tester) async {
    final api = _FakeApiClient(
      devices: [_device, _secondDevice.copyWith(openAlertCount: 3)],
      alerts: const [_secondAlert],
      // 汇总卡使用服务端全量统计：即使只加载 1 页 2 台，总量仍为 10 台
      stats: const StatsOverview(
        deviceTotal: 10,
        deviceOnline: 8,
        deviceOffline: 2,
        reportToday: 0,
        alertOpen: 1,
      ),
    );
    await _pumpBoard(tester, api: api, session: _adminSession);
    await tester.pumpAndSettle();

    await tester.tap(find.widgetWithText(ChoiceChip, '有告警'));
    await tester.pumpAndSettle();

    // 筛选转为服务端参数，不依赖告警列表前 50 条
    expect(api.lastHasAlert, isTrue);
    expect(api.lastStatus, isNull);
    expect(api.lastPage, 1);
    expect(find.text('二号机'), findsOneWidget);
    expect(find.text('一号机'), findsNothing);
    // 卡片角标来自服务端聚合的 open_alert_count
    expect(find.text('告警 3'), findsOneWidget);
    expect(find.text('筛选结果：1 台 / 全部 10 台'), findsOneWidget);

    // 汇总卡同样使用服务端全量统计
    expect(find.text('10'), findsOneWidget);
    expect(find.text('8'), findsOneWidget);

    // 告警总数来自服务端 total，而不是已加载列表长度
    await _scrollTo(tester, find.text('未处理告警（1）'));
    expect(find.text('未处理告警（1）'), findsOneWidget);
  });

  testWidgets('末页显示没有更多了，空结果展示空状态', (tester) async {
    final api = _FakeApiClient(devices: const [_device, _secondDevice]);
    await _pumpBoard(tester, api: api, session: _adminSession);
    await tester.pumpAndSettle();

    await _scrollTo(tester, find.text('没有更多了'));
    expect(find.text('没有更多了'), findsOneWidget);
    expect(find.textContaining('上拉加载更多'), findsNothing);

    // 回到顶部再输入，避免搜索框不在视口内
    await tester.drag(find.byType(ListView).first, const Offset(0, 3000));
    await tester.pumpAndSettle();
    await tester.enterText(find.byType(TextField), '不存在的设备');
    await tester.pump(const Duration(milliseconds: 400));
    await tester.pumpAndSettle();

    expect(find.text('没有匹配的设备'), findsOneWidget);
    expect(find.text('没有更多了'), findsNothing);
  });

  testWidgets('筛选生效期间实时事件仍落到设备并驱动筛选结果', (tester) async {
    final realtime = _FakeRealtimeService(RealtimeState.connected);
    final api = _FakeApiClient(
      devices: const [_device, _secondDevice],
      readings: const [_reading, _secondReading],
    );
    await _pumpBoard(
      tester,
      api: api,
      session: _adminSession,
      realtime: realtime,
    );
    await tester.pumpAndSettle();

    await tester.tap(find.widgetWithText(ChoiceChip, '有告警'));
    await tester.pumpAndSettle();
    expect(find.text('一号机'), findsOneWidget);
    expect(find.text('二号机'), findsNothing);

    // 筛选期间收到上报：仍更新对应设备的实时值
    realtime.emit(
      const ReportEvent(
        SensorReading(
          deviceId: 'dev-0001',
          temperature: 31.2,
          humidity: 50,
          voltage: 3.9,
          rssi: -58,
          battery: 87,
          seq: 9,
          ts: null,
        ),
      ),
    );
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 20));
    expect(find.textContaining('温度 31.2℃'), findsOneWidget);

    // 切换筛选会重拉首屏（fetchCalls 增加）
    await tester.tap(find.widgetWithText(ChoiceChip, '在线'));
    await tester.pumpAndSettle();
    expect(api.fetchCalls, greaterThanOrEqualTo(2));

    // 筛选生效时收到上下线事件，列表按实时状态立即更新
    realtime.emit(
      const StatusEvent(deviceId: 'dev-0001', status: 'offline', reason: 'lwt'),
    );
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 20));
    expect(find.text('一号机'), findsNothing);
    expect(find.text('二号机'), findsOneWidget);
    // easy_refresh 在内容尺寸变化时会排零延迟弹道计时器；多泵几帧让其跑完
    await tester.pumpWidget(const SizedBox.shrink());
    for (var i = 0; i < 3; i++) {
      await tester.pump(const Duration(milliseconds: 10));
    }
  });

  testWidgets('切换分组重置分页并只展示该分组设备', (tester) async {
    final devices = List<DeviceSummary>.generate(25, (index) {
      final id = 'dev-${index.toString().padLeft(2, '0')}';
      return DeviceSummary(
        deviceId: id,
        name: index == 24 ? '巡检设备' : '设备 $id',
        model: 'Battery-Sensor',
        firmwareVersion: '1.0.0',
        groupName: index == 24 ? '巡检组' : '生产组',
        status: 'online',
      );
    });
    final api = _FakeApiClient(devices: devices);
    await _pumpBoard(tester, api: api, session: _adminSession);
    await tester.pumpAndSettle();

    // 先翻到第 2 页
    for (var i = 0; i < 12 && api.lastPage == 1; i++) {
      await tester.drag(find.byType(ListView).first, const Offset(0, -400));
      await tester.pumpAndSettle();
    }
    expect(api.lastPage, 2);

    await tester.drag(find.byType(ListView).first, const Offset(0, 3000));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(ChoiceChip, '巡检组'));
    await tester.pumpAndSettle();

    // 切组后重置到第 1 页，且 group 作为服务端参数下发
    expect(api.lastPage, 1);
    expect(api.lastGroup, '巡检组');
    expect(find.text('巡检设备'), findsOneWidget);
    expect(find.text('设备 dev-00'), findsNothing);
    expect(find.text('筛选结果：1 台 / 全部 25 台'), findsOneWidget);
  });

  testWidgets('分组与搜索组合筛选同时生效', (tester) async {
    const production = DeviceSummary(
      deviceId: 'dev-prod-1',
      name: '一号机',
      model: 'Battery-Sensor',
      firmwareVersion: '1.0.0',
      groupName: '生产组',
      status: 'online',
    );
    const production2 = DeviceSummary(
      deviceId: 'dev-prod-2',
      name: '二号机',
      model: 'Battery-Sensor',
      firmwareVersion: '1.0.0',
      groupName: '生产组',
      status: 'online',
    );
    const warehouse = DeviceSummary(
      deviceId: 'dev-wh-1',
      name: '三号机',
      model: 'Battery-Sensor',
      firmwareVersion: '1.0.0',
      groupName: '仓储组',
      status: 'online',
    );
    final api = _FakeApiClient(
      devices: const [production, production2, warehouse],
    );
    await _pumpBoard(tester, api: api, session: _adminSession);
    await tester.pumpAndSettle();

    await tester.tap(find.widgetWithText(ChoiceChip, '生产组'));
    await tester.pumpAndSettle();
    expect(api.lastGroup, '生产组');

    await tester.enterText(find.byType(TextField), 'dev-prod-2');
    await tester.pump(const Duration(milliseconds: 400));
    await tester.pumpAndSettle();

    expect(api.lastPage, 1);
    expect(api.lastGroup, '生产组');
    expect(api.lastKeyword, 'dev-prod-2');
    expect(find.text('二号机'), findsOneWidget);
    expect(find.text('一号机'), findsNothing);
    expect(find.text('三号机'), findsNothing);
    expect(find.text('筛选结果：1 台 / 全部 3 台'), findsOneWidget);
  });

  testWidgets('无分组设备归入未分组并可筛选', (tester) async {
    const ungrouped = DeviceSummary(
      deviceId: 'dev-ungrouped',
      name: '未分组设备',
      model: 'Battery-Sensor',
      firmwareVersion: '1.0.0',
      groupName: '',
      status: 'online',
    );
    final api = _FakeApiClient(devices: const [_device, ungrouped]);
    await _pumpBoard(tester, api: api, session: _adminSession);
    await tester.pumpAndSettle();

    // 空分组在分组概览中显示为「未分组」
    expect(find.widgetWithText(ChoiceChip, '未分组'), findsOneWidget);

    await tester.tap(find.widgetWithText(ChoiceChip, '未分组'));
    await tester.pumpAndSettle();

    expect(api.lastPage, 1);
    expect(api.lastGroup, '未分组');
    expect(find.text('未分组设备'), findsOneWidget);
    expect(find.text('一号机'), findsNothing);
    expect(find.text('筛选结果：1 台 / 全部 2 台'), findsOneWidget);
  });

  testWidgets('设备详情历史：首屏一页，滚到底自动加载更早', (tester) async {
    final api = _FakeApiClient(records: _recordBatch(30));
    await _pumpBoard(tester, api: api, session: _adminSession);
    await tester.pumpAndSettle();
    await _openDeviceSheet(tester);

    // 首屏只取一页：limit=pageSize+1 作为「是否还有更早」的探测
    expect(api.recordFetchCalls, 1);
    expect(api.lastRecordLimit, 11);
    expect(api.lastRecordOffset, 0);

    // 最新一条（seq 30）排在历史上报区顶部
    await _scrollSheetTo(tester, find.text('49.0℃ / 50% / 80%'));
    expect(find.text('49.0℃ / 50% / 80%'), findsOneWidget);

    // 滚到底触发下一页：offset 按页大小递增，不会漏取或重取
    await tester.drag(_sheetList, const Offset(0, -900));
    await tester.pumpAndSettle();
    expect(api.recordFetchCalls, greaterThanOrEqualTo(2));
    expect(api.recordOffsets, contains(10));
    expect(api.recordOffsets.every((value) => value % 10 == 0), isTrue);

    // 第二页（seq 11..20）已追加到列表
    await _scrollSheetTo(tester, find.text('39.0℃ / 50% / 80%'));
    expect(find.text('39.0℃ / 50% / 80%'), findsOneWidget);
  });

  testWidgets('设备详情历史：取完最后一页展示「没有更早数据」', (tester) async {
    final api = _FakeApiClient(records: _recordBatch(10));
    await _pumpBoard(tester, api: api, session: _adminSession);
    await tester.pumpAndSettle();
    await _openDeviceSheet(tester);

    // 10 条不足一页 + 探测条，说明已到底，不再发第二次请求
    expect(api.recordFetchCalls, 1);
    expect(find.text('加载更早'), findsNothing);
    await _scrollSheetTo(tester, find.text('没有更早数据'));
    expect(find.text('没有更早数据'), findsOneWidget);
    expect(api.recordFetchCalls, 1);
  });

  testWidgets('设备详情历史：实时新上报插到顶部且不打断当前浏览位置', (tester) async {
    final api = _FakeApiClient(records: _recordBatch(10));
    final realtime = _FakeRealtimeService(RealtimeState.connected);
    await _pumpBoard(
      tester,
      api: api,
      session: _adminSession,
      realtime: realtime,
    );
    await tester.pumpAndSettle();
    await _openDeviceSheet(tester);

    // 向下浏览一段，模拟用户正在看较早的历史数据（凭证区在历史上报之上）
    await _scrollSheetTo(tester, find.text('24.0℃ / 50% / 80%'));
    final sheetScrollable = find.descendant(
      of: _sheetList,
      matching: find.byType(Scrollable),
    );
    final positionBefore = tester
        .state<ScrollableState>(sheetScrollable)
        .position
        .pixels;
    expect(positionBefore, greaterThan(0));

    // 选一条已渲染的历史记录作为锚点，记录其屏幕位置
    final anchor = find.text('24.0℃ / 50% / 80%');
    final anchorBefore = tester.getTopLeft(anchor).dy;

    realtime.emit(
      const ReportEvent(
        SensorReading(
          deviceId: 'dev-0001',
          temperature: 30.0,
          humidity: 50,
          voltage: 3.9,
          rssi: -58,
          battery: 88,
          seq: 99,
          ts: null,
        ),
      ),
    );
    await tester.pump();
    await tester.pumpAndSettle();

    // 旧数据锚点仍在原屏幕位置，滚动偏移随插入内容增大
    expect(tester.getTopLeft(anchor).dy, closeTo(anchorBefore, 1.0));
    final positionAfter = tester
        .state<ScrollableState>(sheetScrollable)
        .position
        .pixels;
    expect(positionAfter, greaterThan(positionBefore));

    // 回到顶部确认新上报已插入列表首位
    tester.state<ScrollableState>(sheetScrollable).position.jumpTo(0);
    await tester.pumpAndSettle();
    await tester.scrollUntilVisible(
      find.text('30.0℃ / 50% / 88%'),
      200,
      scrollable: sheetScrollable,
    );
    expect(find.text('30.0℃ / 50% / 88%'), findsOneWidget);
  });

  testWidgets('实时分组变更：改组后设备移出当前筛选，total 与服务端口径一致', (tester) async {
    const production1 = DeviceSummary(
      deviceId: 'dev-prod-1',
      name: '一号机',
      model: 'Battery-Sensor',
      firmwareVersion: '1.0.0',
      groupName: '生产组',
      status: 'online',
    );
    const production2 = DeviceSummary(
      deviceId: 'dev-prod-2',
      name: '二号机',
      model: 'Battery-Sensor',
      firmwareVersion: '1.0.0',
      groupName: '生产组',
      status: 'online',
    );
    final api = _FakeApiClient(devices: const [production1, production2]);
    final realtime = _FakeRealtimeService(RealtimeState.connected);
    await _pumpBoard(
      tester,
      api: api,
      session: _adminSession,
      realtime: realtime,
    );
    await tester.pumpAndSettle();

    await tester.tap(find.widgetWithText(ChoiceChip, '生产组'));
    await tester.pumpAndSettle();
    expect(find.text('筛选结果：2 台 / 全部 2 台'), findsOneWidget);
    final groupsBefore = api.groupFetchCalls;

    // 后台把一号机改到仓储组：先更新服务端数据，再推送实时事件
    api.setDeviceGroup('dev-prod-1', '仓储组');
    realtime.emit(
      const GroupChangedEvent(
        deviceId: 'dev-prod-1',
        groupName: '仓储组',
        previousGroupName: '生产组',
        operator: 'admin',
      ),
    );
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 20));

    // 改组后已不属于「生产组」：立即移出列表，total 同步减一
    expect(find.text('一号机'), findsNothing);
    expect(find.text('二号机'), findsOneWidget);
    expect(find.text('筛选结果：1 台 / 全部 2 台'), findsOneWidget);

    // 防抖后重拉 REST：chips 出现新分组，已空的分组自然不会保留多余条目
    await tester.pump(const Duration(milliseconds: 300));
    await tester.pumpAndSettle();
    expect(api.groupFetchCalls, greaterThan(groupsBefore));
    expect(find.widgetWithText(ChoiceChip, '仓储组'), findsOneWidget);
    expect(find.widgetWithText(ChoiceChip, '生产组'), findsOneWidget);
    expect(find.text('筛选结果：1 台 / 全部 2 台'), findsOneWidget);
    expect(find.text('没有更多了'), findsOneWidget);
  });

  testWidgets('实时分组变更：列表就地更新分组名并刷新 chips', (tester) async {
    final api = _FakeApiClient(devices: const [_device, _secondDevice]);
    final realtime = _FakeRealtimeService(RealtimeState.connected);
    await _pumpBoard(
      tester,
      api: api,
      session: _adminSession,
      realtime: realtime,
    );
    await tester.pumpAndSettle();
    expect(find.widgetWithText(ChoiceChip, '默认分组'), findsOneWidget);
    expect(find.widgetWithText(ChoiceChip, '巡检组'), findsNothing);

    api.setDeviceGroup('dev-0001', '巡检组');
    realtime.emit(
      const GroupChangedEvent(
        deviceId: 'dev-0001',
        groupName: '巡检组',
        previousGroupName: '默认分组',
        operator: 'admin',
      ),
    );
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 20));

    // 未筛选分组时设备仍在列表，仅就地更新分组名
    expect(find.textContaining('dev-0001 · 巡检组'), findsOneWidget);
    expect(find.text('一号机'), findsOneWidget);

    // 服务端刷新后 chips 增加「巡检组」，默认分组仍有二号机
    await tester.pump(const Duration(milliseconds: 300));
    await tester.pumpAndSettle();
    expect(find.widgetWithText(ChoiceChip, '巡检组'), findsOneWidget);
    expect(find.widgetWithText(ChoiceChip, '默认分组'), findsOneWidget);
    expect(find.textContaining('dev-0001 · 巡检组'), findsOneWidget);
  });

  testWidgets('设备详情：影子展示期望、上报、版本与待同步差异', (tester) async {
    final api = _FakeApiClient();
    await _pumpBoard(tester, api: api, session: _viewerSession);
    await tester.pumpAndSettle();
    await _openDeviceSheet(tester);
    await _scrollSheetTo(
      tester,
      find.byKey(const Key('device-shadow-key-report_interval')),
    );

    expect(find.byKey(const Key('device-shadow-section')), findsOneWidget);
    expect(find.text('设备影子'), findsOneWidget);
    expect(find.text('待同步'), findsWidgets);
    expect(find.text('期望版本 v7 · 设备已应用 v6'), findsOneWidget);
    expect(find.text('差异 1 项'), findsOneWidget);
    expect(find.text('report_interval'), findsOneWidget);
    expect(find.text('期望：30'), findsOneWidget);
    expect(find.text('上报：10'), findsOneWidget);
    expect(find.text('只读视图；期望状态请在 Web 管理端修改或回滚。'), findsOneWidget);
    expect(find.text('保存期望'), findsNothing);
  });

  testWidgets('设备详情：影子历史按需加载且只读展示快照与变化键', (tester) async {
    final api = _FakeApiClient();
    await _pumpBoard(tester, api: api, session: _viewerSession);
    await tester.pumpAndSettle();
    await _openDeviceSheet(tester);

    expect(api.shadowHistoryFetchCalls, 0);
    await _scrollSheetTo(
      tester,
      find.byKey(const Key('device-shadow-history-toggle')),
    );
    await tester.tap(find.byKey(const Key('device-shadow-history-toggle')));
    await tester.pumpAndSettle();
    await _scrollSheetTo(
      tester,
      find.byKey(const Key('device-shadow-revision-6')),
    );

    expect(api.shadowHistoryFetchCalls, 1);
    expect(api.lastShadowHistoryLimit, 20);
    expect(api.lastShadowHistoryOffset, 0);
    expect(find.text('v7 · 回滚 · 当前'), findsOneWidget);
    expect(find.text('来源版本 v6'), findsOneWidget);
    expect(find.text('v6 · 单台修改'), findsOneWidget);
    expect(find.text('变化键：led'), findsOneWidget);
    expect(find.text('变化键：report_interval'), findsOneWidget);
    expect(find.textContaining('"led": true'), findsOneWidget);
    expect(find.textContaining('"led": false'), findsOneWidget);
    expect(find.text('回滚到此版本'), findsNothing);
    expect(find.text('保存期望'), findsNothing);
  });

  testWidgets('设备详情：未签发凭证时明确提示并提供签发入口', (tester) async {
    final api = _FakeApiClient();
    await _pumpBoard(tester, api: api, session: _adminSession);
    await tester.pumpAndSettle();
    await _openDeviceSheet(tester);
    await _scrollSheetTo(
      tester,
      find.byKey(const Key('device-credential-section')),
    );

    expect(find.text('MQTT 凭证'), findsOneWidget);
    expect(find.text('未签发'), findsOneWidget);
    expect(find.text('该设备尚未签发凭证，无法接入 Broker'), findsOneWidget);
    expect(find.byKey(const Key('credential-status-chip')), findsOneWidget);
    expect(find.byKey(const Key('credential-issue-button')), findsOneWidget);
  });

  testWidgets('设备详情：签发需二次确认，口令只展示一次且关闭后不可再查', (tester) async {
    final api = _FakeApiClient();
    await _pumpBoard(tester, api: api, session: _adminSession);
    await tester.pumpAndSettle();
    await _openDeviceSheet(tester);
    await _scrollSheetTo(
      tester,
      find.byKey(const Key('device-credential-section')),
    );

    await tester.tap(find.byKey(const Key('credential-issue-button')));
    await tester.pumpAndSettle();

    // 二次确认：写清影响后才能继续
    expect(find.text('签发设备凭证'), findsOneWidget);
    expect(find.textContaining('确认为设备 dev-0001 签发 MQTT 凭证'), findsOneWidget);
    expect(api.credentialIssueCalls, 0);

    await tester.tap(find.text('确认签发'));
    await tester.pumpAndSettle();
    expect(api.credentialIssueCalls, 1);
    expect(api.lastCredentialOperation, 'issue');

    // 一次性口令可见，复制入口存在
    expect(
      find.byKey(const Key('one-time-credential-password')),
      findsOneWidget,
    );
    expect(find.text(api.credentialPassword), findsOneWidget);
    expect(find.text('复制口令'), findsOneWidget);

    await tester.tap(find.byKey(const Key('one-time-credential-ack')));
    await tester.pumpAndSettle();

    // 关闭后口令不可再查，状态刷新为已签发
    expect(find.byKey(const Key('one-time-credential-password')), findsNothing);
    expect(find.text(api.credentialPassword), findsNothing);
    expect(find.text('已签发'), findsOneWidget);
  });

  testWidgets('设备详情：轮换需二次确认，Broker 重载失败时提示旧凭证可能仍有效', (tester) async {
    final activeDevice = _device.copyWith(
      mqttCredentialVersion: 1,
      mqttCredentialStatus: 'active',
      mqttCredentialIssuedAt: DateTime.utc(2026, 11, 4, 8),
      mqttCredentialRotatedAt: DateTime.utc(2026, 11, 4, 8),
      mqttCredentialFingerprintPrefix: '1234abcd',
    );
    final api = _FakeApiClient(devices: [activeDevice])
      ..credentialReloaded = false
      ..credentialPassword = 'rotated-one-time-password';
    await _pumpBoard(tester, api: api, session: _adminSession);
    await tester.pumpAndSettle();
    await _openDeviceSheet(tester);
    await _scrollSheetTo(
      tester,
      find.byKey(const Key('device-credential-section')),
    );

    expect(find.text('已签发'), findsOneWidget);
    expect(find.text('1234abcd…'), findsOneWidget);
    expect(find.byKey(const Key('credential-rotate-button')), findsOneWidget);

    await tester.tap(find.byKey(const Key('credential-rotate-button')));
    await tester.pumpAndSettle();
    expect(find.text('轮换设备凭证'), findsOneWidget);
    expect(find.textContaining('旧凭证对新连接立即失效'), findsOneWidget);
    expect(find.textContaining('设备必须更新配置中的口令'), findsOneWidget);
    expect(find.textContaining('Broker 重载失败'), findsOneWidget);
    expect(api.credentialIssueCalls, 0);

    await tester.tap(find.text('确认轮换'));
    await tester.pumpAndSettle();
    expect(api.credentialIssueCalls, 1);
    expect(api.lastCredentialOperation, 'rotate');

    // 口令一次性展示 + 未重载的真实状态提示
    expect(find.text('rotated-one-time-password'), findsOneWidget);
    expect(find.byKey(const Key('credential-reload-failed')), findsOneWidget);
    expect(
      find.textContaining('已写入新凭证，但 Broker 未重载，旧凭证可能仍然有效'),
      findsOneWidget,
    );

    await tester.tap(find.byKey(const Key('one-time-credential-ack')));
    await tester.pumpAndSettle();
    expect(find.text('rotated-one-time-password'), findsNothing);
    expect(find.text('已签发'), findsOneWidget);
  });

  testWidgets('operator 可签发但不可轮换/吊销，界面给出原因', (tester) async {
    final activeDevice = _device.copyWith(
      mqttCredentialVersion: 1,
      mqttCredentialStatus: 'active',
      mqttCredentialIssuedAt: DateTime.utc(2026, 11, 4, 8),
      mqttCredentialRotatedAt: DateTime.utc(2026, 11, 4, 8),
    );
    final api = _FakeApiClient(devices: [activeDevice]);
    const operatorSession = AuthSession(
      token: 'jwt-operator',
      username: 'operator',
      role: 'operator',
    );
    await _pumpBoard(tester, api: api, session: operatorSession);
    await tester.pumpAndSettle();
    await _openDeviceSheet(tester);
    await _scrollSheetTo(
      tester,
      find.byKey(const Key('device-credential-section')),
    );

    expect(find.text('轮换/吊销仅管理员可执行'), findsOneWidget);
    final rotate = tester.widget<FilledButton>(
      find.byKey(const Key('credential-rotate-button')),
    );
    expect(rotate.onPressed, isNull);
    final revoke = tester.widget<TextButton>(
      find.byKey(const Key('credential-revoke-button')),
    );
    expect(revoke.onPressed, isNull);
    expect(api.credentialIssueCalls, 0);
  });

  testWidgets('admin 吊销凭证：确认后状态变为已吊销', (tester) async {
    final activeDevice = _device.copyWith(
      mqttCredentialVersion: 1,
      mqttCredentialStatus: 'active',
      mqttCredentialIssuedAt: DateTime.utc(2026, 11, 4, 8),
      mqttCredentialRotatedAt: DateTime.utc(2026, 11, 4, 8),
    );
    final api = _FakeApiClient(devices: [activeDevice]);
    await _pumpBoard(tester, api: api, session: _adminSession);
    await tester.pumpAndSettle();
    await _openDeviceSheet(tester);
    await _scrollSheetTo(
      tester,
      find.byKey(const Key('device-credential-section')),
    );

    await tester.tap(find.byKey(const Key('credential-revoke-button')));
    await tester.pumpAndSettle();
    expect(find.textContaining('确认吊销设备 dev-0001'), findsOneWidget);
    await tester.tap(find.text('确认吊销'));
    await tester.pumpAndSettle();

    expect(api.credentialRevokeCalls, 1);
    expect(find.text('已吊销'), findsOneWidget);
    expect(find.byKey(const Key('credential-revoked-tip')), findsOneWidget);
  });

  testWidgets('标签筛选与关键词/状态组合生效并重置分页', (tester) async {
    final devices = List<DeviceSummary>.generate(25, (index) {
      final id = 'dev-${index.toString().padLeft(2, '0')}';
      return DeviceSummary(
        deviceId: id,
        name: '设备 $id',
        model: 'Battery-Sensor',
        firmwareVersion: '1.0.0',
        groupName: '冷库组',
        status: index == 1 ? 'offline' : 'online',
        tags: index <= 2 ? const <String>['冷库'] : const <String>['常温'],
      );
    });
    final api = _FakeApiClient(devices: devices);
    await _pumpBoard(tester, api: api, session: _adminSession);
    await tester.pumpAndSettle();

    // 标签 chips 来自 GET /api/devices/tags，带设备数
    expect(find.widgetWithText(ChoiceChip, '冷库（3）'), findsOneWidget);
    expect(find.widgetWithText(ChoiceChip, '常温（22）'), findsOneWidget);

    // 先翻到第 2 页，验证切换标签会重置回第 1 页
    for (var i = 0; i < 12 && api.lastPage == 1; i++) {
      await tester.drag(find.byType(ListView).first, const Offset(0, -400));
      await tester.pumpAndSettle();
    }
    expect(api.lastPage, 2);

    await tester.drag(find.byType(ListView).first, const Offset(0, 3000));
    await tester.pumpAndSettle();
    await tester.tap(find.widgetWithText(ChoiceChip, '冷库（3）'));
    await tester.pumpAndSettle();

    expect(api.lastPage, 1);
    expect(api.lastTags, const <String>['冷库']);
    expect(find.text('设备 dev-00'), findsOneWidget);
    expect(find.text('设备 dev-03'), findsNothing);
    expect(find.text('筛选结果：3 台 / 全部 25 台'), findsOneWidget);

    // 与关键词组合：标签 + 关键词同时作为服务端参数
    await tester.enterText(find.byType(TextField), 'dev-01');
    await tester.pump(const Duration(milliseconds: 400));
    await tester.pumpAndSettle();
    expect(api.lastPage, 1);
    expect(api.lastTags, const <String>['冷库']);
    expect(api.lastKeyword, 'dev-01');
    expect(find.text('设备 dev-01'), findsOneWidget);
    expect(find.text('设备 dev-00'), findsNothing);

    // 再叠加状态筛选：标签 + 关键词 + 状态三者同时生效
    await tester.tap(find.widgetWithText(ChoiceChip, '离线'));
    await tester.pumpAndSettle();
    expect(api.lastStatus, 'offline');
    expect(api.lastTags, const <String>['冷库']);
    expect(api.lastKeyword, 'dev-01');
    expect(find.text('设备 dev-01'), findsOneWidget);
    expect(find.text('筛选结果：1 台 / 全部 25 台'), findsOneWidget);
  });
}
