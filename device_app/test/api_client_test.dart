import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:http/http.dart' as http;
import 'package:http/testing.dart';
import 'package:iot_device_app/services/api_client.dart';

Map<String, dynamic> _ok(Object? data) => <String, dynamic>{
  'code': 0,
  'message': 'ok',
  'data': data,
};

/// MockClient 返回中文时必须声明 UTF-8，否则 http.Response 默认按 latin1 编码会抛错
http.Response _jsonResponse(Object body, [int statusCode = 200]) =>
    http.Response(
      jsonEncode(body),
      statusCode,
      headers: <String, String>{
        'content-type': 'application/json; charset=utf-8',
      },
    );

void main() {
  test('login 解析 token / 角色，并正确拼接地址与请求体', () async {
    late http.Request captured;
    final httpClient = MockClient((request) async {
      captured = request;
      return _jsonResponse(
        _ok(<String, dynamic>{
          'token': 'jwt-token',
          'user': <String, dynamic>{'username': 'operator', 'role': 'operator'},
        }),
      );
    });
    final client = ApiClient(
      baseUrl: 'http://api.example:8000',
      httpClient: httpClient,
    );

    final session = await client.login(
      username: 'operator',
      password: 'operator123',
    );

    expect(session.token, 'jwt-token');
    expect(session.username, 'operator');
    expect(session.role, 'operator');
    expect(captured.url.toString(), 'http://api.example:8000/api/auth/login');
    expect(captured.headers['Content-Type'], 'application/json');
    expect(jsonDecode(captured.body)['password'], 'operator123');
  });

  test('401 统一映射为 unauthorized，便于看板引导重新登录', () async {
    final client = ApiClient(
      baseUrl: 'http://api.example:8000',
      httpClient: MockClient(
        (_) async => _jsonResponse(<String, String>{'detail': '登录已失效'}, 401),
      ),
    );

    await expectLater(
      client.fetchDevices(token: 'expired'),
      throwsA(
        isA<ApiException>().having(
          (error) => error.kind,
          'kind',
          ApiErrorKind.unauthorized,
        ),
      ),
    );
  });

  test('超时映射为 timeout，并携带可读提示', () async {
    final client = ApiClient(
      baseUrl: 'http://api.example:8000',
      timeout: const Duration(milliseconds: 50),
      httpClient: MockClient((_) async {
        await Future<void>.delayed(const Duration(seconds: 2));
        return _jsonResponse(_ok(const <dynamic>[]));
      }),
    );

    await expectLater(
      client.fetchLatestReadings(token: 'token'),
      throwsA(
        isA<ApiException>().having(
          (error) => error.kind,
          'kind',
          ApiErrorKind.timeout,
        ),
      ),
    );
  });

  test('网络不可达映射为 network，不把底层异常抛给 UI', () async {
    final client = ApiClient(
      baseUrl: 'http://api.example:8000',
      httpClient: MockClient(
        (_) async => throw http.ClientException('connection refused'),
      ),
    );

    await expectLater(
      client.fetchOpenAlerts(token: 'token'),
      throwsA(
        isA<ApiException>().having(
          (error) => error.kind,
          'kind',
          ApiErrorKind.network,
        ),
      ),
    );
  });

  test('响应未声明 charset 时仍按 UTF-8 解码中文', () async {
    final client = ApiClient(
      baseUrl: 'http://api.example:8000',
      httpClient: MockClient(
        (_) async => http.Response.bytes(
          utf8.encode(jsonEncode(<String, dynamic>{'detail': '设备不存在'})),
          404,
          // 故意不带 content-type，模拟部分 FastAPI/代理响应
        ),
      ),
    );

    await expectLater(
      client.fetchDevices(token: 'token'),
      throwsA(
        isA<ApiException>()
            .having((error) => error.kind, 'kind', ApiErrorKind.server)
            .having((error) => error.message, 'message', '设备不存在'),
      ),
    );
  });

  test('单设备历史记录拼接正确路径与 limit 参数', () async {
    late http.Request captured;
    final client = ApiClient(
      baseUrl: 'http://api.example:8000',
      httpClient: MockClient((request) async {
        captured = request;
        return _jsonResponse(
          _ok(<dynamic>[
            <String, dynamic>{
              'device_id': 'dev-0001',
              'temperature': 25.5,
              'humidity': 60,
              'voltage': 3.7,
              'rssi': -70,
              'battery': 88,
              'seq': 12,
              'ts': '2026-11-04T02:00:00+00:00',
            },
          ]),
        );
      }),
    );

    final records = await client.fetchDeviceRecords(
      token: 'token',
      deviceId: 'dev-0001',
      limit: 5,
    );

    expect(records, hasLength(1));
    expect(records.first.seq, 12);
    expect(captured.url.path, '/api/devices/dev-0001/records');
    expect(captured.url.queryParameters['limit'], '5');
    expect(captured.headers['Authorization'], 'Bearer token');
  });

  test('sendCommand 使用 POST 并携带 type/payload，解析回执', () async {
    late http.Request captured;
    final client = ApiClient(
      baseUrl: 'http://api.example:8000',
      httpClient: MockClient((request) async {
        captured = request;
        return _jsonResponse(
          _ok(<String, dynamic>{
            'cmd_id': 'cmd-abc',
            'device_id': 'dev-0001',
            'type': 'calibrate',
            'state': 'issued',
            'progress': 0,
            'message': '指令已下发，等待设备回执',
            'operator': 'operator',
            'created_at': '2026-11-04T02:00:00+00:00',
          }),
        );
      }),
    );

    final command = await client.sendCommand(
      token: 'token',
      deviceId: 'dev-0001',
      type: 'calibrate',
      payload: <String, dynamic>{'temperatureOffset': 1.5, 'humidityOffset': 0},
    );

    expect(captured.method, 'POST');
    expect(captured.url.path, '/api/devices/dev-0001/commands');
    expect(captured.headers['Authorization'], 'Bearer token');
    expect(jsonDecode(captured.body), <String, dynamic>{
      'type': 'calibrate',
      'payload': <String, dynamic>{
        'temperatureOffset': 1.5,
        'humidityOffset': 0,
      },
    });
    expect(command.cmdId, 'cmd-abc');
    expect(command.state, 'issued');
  });

  test('ackAlert 409 映射为 conflict 并保留服务端文案', () async {
    final client = ApiClient(
      baseUrl: 'http://api.example:8000',
      httpClient: MockClient(
        (_) async => _jsonResponse(<String, String>{'detail': '告警已被处理'}, 409),
      ),
    );

    await expectLater(
      client.ackAlert(token: 'token', alertId: 'alert-001'),
      throwsA(
        isA<ApiException>()
            .having((error) => error.kind, 'kind', ApiErrorKind.conflict)
            .having((error) => error.message, 'message', '告警已被处理'),
      ),
    );
  });

  test('ackAlert 403 映射为 forbidden，供看板禁用写按钮', () async {
    final client = ApiClient(
      baseUrl: 'http://api.example:8000',
      httpClient: MockClient(
        (_) async => _jsonResponse(<String, String>{'detail': '无操作权限'}, 403),
      ),
    );

    await expectLater(
      client.ackAlert(token: 'token', alertId: 'alert-001'),
      throwsA(
        isA<ApiException>().having(
          (error) => error.kind,
          'kind',
          ApiErrorKind.forbidden,
        ),
      ),
    );
  });

  test('设备列表按 envelope.data.items 解析，忽略非法条目', () async {
    final client = ApiClient(
      baseUrl: 'http://api.example:8000',
      httpClient: MockClient(
        (_) async => _jsonResponse(
          _ok(<String, dynamic>{
            'items': <dynamic>[
              <String, dynamic>{
                'device_id': 'dev-0001',
                'name': '电池传感器',
                'model': 'VG-S100',
                'firmware_version': '1.0.0',
                'group_name': '默认分组',
                'status': 'online',
              },
              'not-a-map',
            ],
            'total': 2,
          }),
        ),
      ),
    );

    final devices = await client.fetchDevices(token: 'token');

    expect(devices, hasLength(1));
    expect(devices.first.deviceId, 'dev-0001');
    expect(devices.first.online, isTrue);
  });
}
