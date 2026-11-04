import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:iot_device_app/models/mobile_api_models.dart';
import 'package:iot_device_app/services/api_client.dart';
import 'package:iot_device_app/services/app_storage.dart';
import 'package:iot_device_app/services/session_refresh.dart';
import 'package:shared_preferences/shared_preferences.dart';

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

http.Response _json(Object body, [int status = 200]) => http.Response(
  jsonEncode(body),
  status,
  headers: <String, String>{
    'content-type': 'application/json; charset=utf-8',
  },
);

Map<String, dynamic> _ok(Object? data) => <String, dynamic>{
  'code': 0,
  'message': 'ok',
  'data': data,
};

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

  MockClient mockClient({bool refreshFails = false}) =>
      MockClient((request) async {
        if (request.url.path == '/api/auth/refresh') {
          refreshCalls += 1;
          if (refreshFails) {
            return _json(<String, String>{'detail': '刷新令牌已被吊销'}, 401);
          }
          return _json(
            _ok(<String, dynamic>{
              'token': 'new-access',
              'refresh_token': 'refresh-2',
              'user': <String, dynamic>{
                'username': 'admin',
                'role': 'admin',
              },
            }),
          );
        }
        if (request.headers['Authorization'] == 'Bearer expired-access') {
          return _json(<String, String>{'detail': '令牌已过期'}, 401);
        }
        if (request.headers['Authorization'] == 'Bearer new-access') {
          return _json(
            _ok(<String, dynamic>{
              'items': <dynamic>[],
              'total': 0,
              'page': 1,
              'page_size': 20,
            }),
          );
        }
        return _json(<String, String>{'detail': '无法识别令牌'}, 403);
      });

  test('并发 5 个 401 只触发 1 次 refresh，并写入安全存储', () async {
    final httpClient = mockClient();
    final coordinator = SessionRefreshCoordinator(
      storage: storage,
      baseUrl: () => 'http://api.example:8000',
      readSession: () => current,
      onRefreshed: (session) => current = session,
      clientBuilder: (url) =>
          ApiClient(baseUrl: url, httpClient: httpClient),
    );
    final client = ApiClient(
      baseUrl: 'http://api.example:8000',
      httpClient: httpClient,
      refresher: coordinator,
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
    final httpClient = mockClient(refreshFails: true);
    final coordinator = SessionRefreshCoordinator(
      storage: storage,
      baseUrl: () => 'http://api.example:8000',
      readSession: () => current,
      onRefreshed: (session) => current = session,
      clientBuilder: (url) =>
          ApiClient(baseUrl: url, httpClient: httpClient),
    );
    final client = ApiClient(
      baseUrl: 'http://api.example:8000',
      httpClient: httpClient,
      refresher: coordinator,
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
}
