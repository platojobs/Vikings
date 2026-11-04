import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:iot_device_app/models/mobile_api_models.dart';
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
  _FakeApiClient({this.fetchError, this.ackError})
    : super(baseUrl: 'http://192.168.1.8:8000');

  /// 仅在对应用例注入；为空时返回固定测试数据。
  final Object? fetchError;
  final Object? ackError;

  int loginCalls = 0;
  int fetchCalls = 0;
  int sendCalls = 0;
  int ackCalls = 0;

  @override
  Future<AuthSession> login({
    required String username,
    required String password,
  }) async {
    loginCalls += 1;
    return _adminSession;
  }

  @override
  Future<List<DeviceSummary>> fetchDevices({required String token}) async {
    fetchCalls += 1;
    final error = fetchError;
    if (error != null) {
      throw error;
    }
    return const <DeviceSummary>[_device];
  }

  @override
  Future<List<SensorReading>> fetchLatestReadings({
    required String token,
  }) async => const <SensorReading>[_reading];

  @override
  Future<List<AlertItem>> fetchOpenAlerts({required String token}) async =>
      const <AlertItem>[_openAlert];

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
  }) async => const <SensorReading>[];

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

  @override
  void start() {
    state.value = initialState;
  }

  @override
  Future<void> dispose() async {}
}

Future<AppStorage> _pumpBoard(
  WidgetTester tester, {
  required _FakeApiClient api,
  AuthSession? session,
  RealtimeState realtimeState = RealtimeState.connected,
}) async {
  final storage = AppStorage(secureStore: _FakeSecureStore());
  if (session != null) {
    await storage.saveSession(session);
  }
  await tester.pumpWidget(
    MaterialApp(
      home: MobileBoardPage(
        storage: storage,
        apiClientFactory: (_) => api,
        realtimeServiceFactory: (_) => _FakeRealtimeService(realtimeState),
      ),
    ),
  );
  return storage;
}

Future<void> _openDeviceSheet(WidgetTester tester) async {
  await tester.tap(find.text('一号机'));
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
    expect(find.text('温度过高'), findsOneWidget);
    expect(find.text('admin · admin'), findsOneWidget);
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
    expect(find.text('未处理告警（1）'), findsOneWidget);

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
    final ackButton = tester.widget<TextButton>(
      find.widgetWithText(TextButton, '确认告警'),
    );
    expect(ackButton.onPressed, isNull);

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
}
