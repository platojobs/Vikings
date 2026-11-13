import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:iot_device_app/models/mobile_api_models.dart';
import 'package:iot_device_app/models/realtime_payload.dart';
import 'package:iot_device_app/pages/overview_page.dart';
import 'package:iot_device_app/services/api_client.dart';
import 'package:iot_device_app/services/app_storage.dart';
import 'package:iot_device_app/services/board_realtime_service.dart';
import 'package:shared_preferences/shared_preferences.dart';

const AuthSession _adminSession = AuthSession(
  token: 'jwt-admin',
  username: 'admin',
  role: 'admin',
);

class _FakeSecureStore implements SecureStore {
  final Map<String, String> values = <String, String>{};

  @override
  Future<String?> read(String key) async => values[key];

  @override
  Future<void> write(String key, String value) async => values[key] = value;

  @override
  Future<void> delete(String key) async => values.remove(key);
}

Future<AppStorage> _storage({AuthSession? session}) async {
  SharedPreferences.setMockInitialValues(<String, Object>{});
  final storage = AppStorage(secureStore: _FakeSecureStore());
  if (session != null) {
    await storage.saveSession(session);
  }
  return storage;
}

class _FakeOverviewApi extends ApiClient {
  _FakeOverviewApi({required this.overview, this.error})
    : super(baseUrl: 'http://192.168.1.8:8000');

  StatsOverview overview;
  Object? error;
  int calls = 0;

  @override
  Future<StatsOverview> fetchStatsOverview({required String token}) async {
    calls += 1;
    final failure = error;
    if (failure is ApiException) {
      throw failure;
    }
    return overview;
  }

  @override
  Future<List<SensorReading>> fetchLatestReadings({
    required String token,
    List<String>? deviceIds,
  }) async => const <SensorReading>[];
}

List<OverviewTrendPoint> _trend({int reportsPerHour = 0}) {
  final now = DateTime.now();
  final start = DateTime(
    now.year,
    now.month,
    now.day,
    now.hour,
  ).subtract(const Duration(hours: 23));
  return List<OverviewTrendPoint>.generate(24, (index) {
    return OverviewTrendPoint(
      hourStart: start.add(Duration(hours: index)),
      reports: reportsPerHour,
      alerts: index == 23 ? 1 : 0,
    );
  });
}

StatsOverview _stats({
  int total = 3,
  int online = 2,
  int credentialMissing = 1,
  int alertOpen = 2,
  int warning = 1,
  int critical = 1,
  bool trendComplete = true,
}) {
  return StatsOverview(
    deviceTotal: total,
    deviceOnline: online,
    deviceOffline: total - online,
    reportToday: 35,
    alertOpen: alertOpen,
    credentialMissing: credentialMissing,
    warningOpen: warning,
    criticalOpen: critical,
    trend: _trend(reportsPerHour: 1),
    trendComplete: trendComplete,
    firstRecordAt: DateTime.now().subtract(const Duration(hours: 2)),
    abnormalDevices: const <AbnormalDevice>[
      AbnormalDevice(
        deviceId: 'dev-0002',
        name: '二号机',
        status: 'offline',
        openAlertCount: 2,
        reason: 'alert+offline',
      ),
      AbnormalDevice(
        deviceId: 'dev-0001',
        name: '一号机',
        status: 'online',
        openAlertCount: 1,
        reason: 'alert',
      ),
    ],
  );
}

AlertItem _alert(String id, String level) => AlertItem(
  alertId: id,
  deviceId: 'dev-0001',
  level: level,
  metric: 'temperature',
  value: 40,
  threshold: 35,
  message: '温度超限',
  aiGenerated: false,
  status: 'open',
  createdAt: DateTime.now(),
);

Future<void> _pumpOverview(
  WidgetTester tester, {
  required _FakeOverviewApi api,
  AuthSession? session = _adminSession,
  StreamController<RealtimeEvent>? events,
  ValueNotifier<RealtimeState>? feedState,
  ValueNotifier<bool>? feedPolling,
  VoidCallback? onOpenBoard,
}) async {
  final storage = await _storage(session: session);
  await tester.pumpWidget(
    MaterialApp(
      home: OverviewPage(
        storage: storage,
        apiClientFactory: (_) => api,
        liveEvents: events?.stream,
        feedState:
            feedState ?? ValueNotifier<RealtimeState>(RealtimeState.connected),
        feedPolling: feedPolling ?? ValueNotifier<bool>(false),
        onOpenDeviceInBoard: onOpenBoard == null ? null : (_) => onOpenBoard(),
      ),
    ),
  );
  await tester.pumpAndSettle();
}

void main() {
  testWidgets('概览渲染：设备、在线率、告警、凭证缺口、趋势与异常设备', (tester) async {
    final api = _FakeOverviewApi(overview: _stats());
    await _pumpOverview(tester, api: api);

    expect(find.text('概览'), findsOneWidget);
    expect(find.text('3'), findsWidgets);
    expect(find.text('67%'), findsOneWidget);
    expect(find.text('2'), findsWidgets);
    expect(find.text('在线 2 · 离线 1'), findsOneWidget);
    expect(find.text('warning 1 · critical 1'), findsOneWidget);
    expect(find.text('这些设备无法接入 Broker'), findsOneWidget);
    expect(find.text('近 24 小时共 24 条上报 · 1 条新增告警'), findsOneWidget);
    expect(find.byKey(const ValueKey('overview-trend-chart')), findsOneWidget);
    expect(
      find.byKey(const ValueKey('overview-abnormal-dev-0002')),
      findsOneWidget,
    );
    expect(find.text('二号机'), findsOneWidget);
    expect(find.textContaining('未处理告警 2'), findsWidgets);
  });

  testWidgets('下拉刷新：重新拉取 REST 快照并刷新页面数据', (tester) async {
    final api = _FakeOverviewApi(overview: _stats());
    await _pumpOverview(tester, api: api);
    expect(api.calls, 1);

    api.overview = _stats(total: 4, online: 3);
    unawaited(
      tester.state<RefreshIndicatorState>(find.byType(RefreshIndicator)).show(),
    );
    await tester.pumpAndSettle();

    expect(api.calls, 2);
    expect(find.text('75%'), findsOneWidget);
  });

  testWidgets('数据不足 24 小时时如实提示，不补假点', (tester) async {
    final api = _FakeOverviewApi(overview: _stats(trendComplete: false));
    await _pumpOverview(tester, api: api);

    expect(find.byKey(const ValueKey('overview-trend-note')), findsOneWidget);
    expect(find.textContaining('数据不足 24 小时'), findsOneWidget);
    expect(find.textContaining('仅展示已有真实数据'), findsOneWidget);
  });

  testWidgets('空数据：无设备时展示空状态而不是零值报表', (tester) async {
    final api = _FakeOverviewApi(overview: StatsOverview.empty);
    await _pumpOverview(tester, api: api);

    expect(find.byKey(const ValueKey('overview-empty')), findsOneWidget);
    expect(find.text('暂无设备数据'), findsOneWidget);
    expect(find.byKey(const ValueKey('overview-device-total')), findsNothing);
  });

  testWidgets('实时事件到达时告警数字立即变化，并由 REST 快照校正', (tester) async {
    final events = StreamController<RealtimeEvent>.broadcast();
    final api = _FakeOverviewApi(overview: _stats());
    final feedState = ValueNotifier<RealtimeState>(RealtimeState.connected);
    final feedPolling = ValueNotifier<bool>(false);
    addTearDown(() {
      events.close();
      feedState.dispose();
      feedPolling.dispose();
    });

    await _pumpOverview(
      tester,
      api: api,
      events: events,
      feedState: feedState,
      feedPolling: feedPolling,
    );
    expect(find.text('warning 1 · critical 1'), findsOneWidget);

    // 服务端最终也会返回 3 条未处理告警，验证增量与快照不会长期偏离
    api.overview = _stats(alertOpen: 3, warning: 1, critical: 2);
    events.add(AlertEvent(_alert('alt-live', 'critical')));
    await tester.pump(const Duration(milliseconds: 50));
    expect(find.text('warning 1 · critical 2'), findsOneWidget);

    await tester.pump(const Duration(milliseconds: 900));
    await tester.pumpAndSettle();
    expect(find.text('warning 1 · critical 2'), findsOneWidget);
    expect(api.calls, greaterThanOrEqualTo(2));
    expect(find.textContaining('已切换轮询'), findsNothing);
  });

  testWidgets('实时不可用时顶部状态条明确显示轮询降级', (tester) async {
    final events = StreamController<RealtimeEvent>.broadcast();
    final feedState = ValueNotifier<RealtimeState>(RealtimeState.connecting);
    final feedPolling = ValueNotifier<bool>(true);
    addTearDown(() {
      events.close();
      feedState.dispose();
      feedPolling.dispose();
    });

    await _pumpOverview(
      tester,
      api: _FakeOverviewApi(overview: _stats()),
      events: events,
      feedState: feedState,
      feedPolling: feedPolling,
    );
    expect(find.textContaining('已切换轮询（每 15 秒）'), findsOneWidget);
  });

  testWidgets('点击异常设备进入只读详情，可跳转看板并聚焦', (tester) async {
    var openedBoard = false;
    await _pumpOverview(
      tester,
      api: _FakeOverviewApi(overview: _stats()),
      onOpenBoard: () => openedBoard = true,
    );

    final tile = find.byKey(const ValueKey('overview-abnormal-dev-0002'));
    await tester.ensureVisible(tile);
    await tester.pumpAndSettle();
    await tester.tap(tile);
    await tester.pumpAndSettle();
    expect(find.text('dev-0002 · offline'), findsNothing);
    expect(find.textContaining('offline'), findsWidgets);
    expect(find.textContaining('未处理告警 2'), findsWidgets);
    expect(find.text('该设备暂无最新上报数据'), findsOneWidget);

    await tester.tap(find.text('在移动看板查看该设备'));
    await tester.pumpAndSettle();
    expect(openedBoard, isTrue);
  });

  testWidgets('未登录时显示登录页', (tester) async {
    final api = _FakeOverviewApi(overview: StatsOverview.empty);
    await _pumpOverview(tester, api: api, session: null);

    expect(find.text('移动看板 · 登录'), findsOneWidget);
    expect(find.byKey(const ValueKey('overview-empty')), findsNothing);
  });

  testWidgets('401 时清除会话并回到登录页，给出失效提示', (tester) async {
    final api = _FakeOverviewApi(
      overview: StatsOverview.empty,
      error: const ApiException(ApiErrorKind.unauthorized, '登录已失效'),
    );
    await _pumpOverview(tester, api: api, session: _adminSession);

    expect(find.text('移动看板 · 登录'), findsOneWidget);
    expect(find.text('登录已失效，请重新登录'), findsOneWidget);
  });
}
