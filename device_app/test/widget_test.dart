import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:iot_device_app/models/mobile_api_models.dart';
import 'package:iot_device_app/pages/home_shell.dart';
import 'package:iot_device_app/services/api_client.dart';
import 'package:iot_device_app/services/app_storage.dart';
import 'package:shared_preferences/shared_preferences.dart';

class _MemorySecureStore implements SecureStore {
  final Map<String, String> values = <String, String>{};

  @override
  Future<String?> read(String key) async => values[key];

  @override
  Future<void> write(String key, String value) async => values[key] = value;

  @override
  Future<void> delete(String key) async => values.remove(key);
}

class _StubApi extends ApiClient {
  _StubApi() : super(baseUrl: 'http://127.0.0.1:8000');

  @override
  Future<StatsOverview> fetchStatsOverview({required String token}) async =>
      const StatsOverview(
        deviceTotal: 0,
        deviceOnline: 0,
        deviceOffline: 0,
        reportToday: 0,
        alertOpen: 0,
      );
}

void main() {
  testWidgets('默认首页为概览；未登录时显示登录页且工具入口可达', (tester) async {
    SharedPreferences.setMockInitialValues(<String, Object>{});
    final storage = AppStorage(secureStore: _MemorySecureStore());
    await tester.pumpWidget(
      MaterialApp(
        home: HomeShell(
          storage: storage,
          apiClientFactory: (_) => _StubApi(),
        ),
      ),
    );
    // 概览页启动时会读取本地会话；设备模拟器 tab 仍有周期定时器，
    // 因此用有限 pump 而不是 pumpAndSettle。
    await tester.pump();
    await tester.pump(const Duration(seconds: 1));

    // v1.27.0：首页默认进入概览；无会话时概览页展示登录页
    expect(find.text('移动看板 · 登录'), findsOneWidget);
    expect(find.text('概览'), findsOneWidget);
    expect(find.text('工具'), findsOneWidget);
    expect(find.text('告警中心'), findsOneWidget);
  });
}
