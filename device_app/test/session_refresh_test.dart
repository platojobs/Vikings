import 'package:flutter_test/flutter_test.dart';
import 'package:iot_device_app/models/mobile_api_models.dart';
import 'package:iot_device_app/services/api_client.dart';
import 'package:iot_device_app/services/app_storage.dart';
import 'package:iot_device_app/services/session_refresh.dart';
import 'package:shared_preferences/shared_preferences.dart';

import 'support/dio_mock.dart';

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

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  late _FakeSecureStore secure;
  late AppStorage storage;
  late AuthSession current;
  late int refreshCalls;

  setUp(() {
    SharedPreferences.setMockInitialValues(<String, Object>{});
    secure = _FakeSecureStore();
    storage = AppStorage(secureStore: secure);
    current = const AuthSession(
      token: 'expired-access',
      refreshToken: 'refresh-1',
      username: 'admin',
      role: 'admin',
    );
    refreshCalls = 0;
  });

  /// 模拟网关：旧 access 一律 401，refresh 之后新 access 才能通过。
  MockDioAdapter gateway({bool refreshFails = false, bool alwaysReject = false}) {
    return MockDioAdapter((request) {
      if (request.uri.path == '/api/auth/refresh') {
        refreshCalls += 1;
        if (refreshFails) {
          return jsonResponse(<String, String>{'detail': '刷新令牌已被吊销'}, 401);
        }
        return jsonResponse(
          okEnvelope(<String, dynamic>{
            'token': 'new-access',
            'refresh_token': 'refresh-2',
            'user': <String, dynamic>{'username': 'admin', 'role': 'admin'},
          }),
        );
      }
      if (alwaysReject ||
          request.header('authorization') == 'Bearer expired-access') {
        return jsonResponse(<String, String>{'detail': '令牌已过期'}, 401);
      }
      if (request.header('authorization') == 'Bearer new-access') {
        return jsonResponse(
          okEnvelope(<String, dynamic>{
            'items': <dynamic>[],
            'total': 0,
            'page': 1,
            'page_size': 20,
          }),
        );
      }
      return jsonResponse(<String, String>{'detail': '无法识别令牌'}, 403);
    });
  }

  SessionRefreshCoordinator coordinatorFor(MockDioAdapter adapter) =>
      SessionRefreshCoordinator(
        storage: storage,
        baseUrl: () => 'http://api.example:8000',
        readSession: () => current,
        onRefreshed: (session) => current = session,
        clientBuilder: (url) => mockApiClient(adapter, baseUrl: url),
      );

  test('并发 5 个 401 只触发 1 次 refresh，并写入安全存储', () async {
    final adapter = gateway();
    final client = mockApiClient(
      adapter,
      refresher: coordinatorFor(adapter),
    );
    await storage.saveSession(current);

    final results = await Future.wait(
      List<Future<DevicePage>>.generate(
        5,
        (_) => client.fetchDevices(token: 'expired-access'),
      ),
    );

    expect(results.length, 5);
    expect(refreshCalls, 1, reason: '5 个并发 401 必须单飞成一次 refresh');
    expect(current.token, 'new-access');
    expect(current.refreshToken, 'refresh-2');
    final persisted = await storage.loadSession();
    expect(persisted?.token, 'new-access');
    expect(persisted?.refreshToken, 'refresh-2');
  });

  test('refresh 失败时保留 401 语义，交给页面清会话', () async {
    final adapter = gateway(refreshFails: true);
    final client = mockApiClient(
      adapter,
      refresher: coordinatorFor(adapter),
    );
    await storage.saveSession(current);

    await expectLater(
      client.fetchDevices(token: 'expired-access'),
      throwsA(
        isA<ApiException>().having(
          (error) => error.kind,
          'kind',
          ApiErrorKind.unauthorized,
        ),
      ),
    );
    expect(refreshCalls, 1);
    expect(current.token, 'expired-access');
  });

  test('刷新后仍 401 不会循环刷新，直接回 unauthorized', () async {
    final adapter = gateway(alwaysReject: true);
    final client = mockApiClient(
      adapter,
      refresher: coordinatorFor(adapter),
    );
    await storage.saveSession(current);

    await expectLater(
      client.fetchDevices(token: 'expired-access'),
      throwsA(
        isA<ApiException>().having(
          (error) => error.kind,
          'kind',
          ApiErrorKind.unauthorized,
        ),
      ),
    );
    expect(refreshCalls, 1, reason: '重放后的 401 不得再次触发 refresh');
  });
}
