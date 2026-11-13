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

  test('fetchDeviceShadow 编码设备 ID，并解析期望、上报与差异', () async {
    late http.Request captured;
    final client = ApiClient(
      baseUrl: 'http://api.example:8000',
      httpClient: MockClient((request) async {
        captured = request;
        return _jsonResponse(
          _ok(<String, dynamic>{
            'device_id': 'dev/0001',
            'desired': <String, dynamic>{'report_interval': 30, 'led': true},
            'reported': <String, dynamic>{'report_interval': 10, 'led': true},
            'version': 7,
            'reported_version': 6,
            'sync_status': 'pending',
            'diff_keys': <String>['report_interval'],
            'pending_desired': <String, dynamic>{'report_interval': 30},
            'desired_updated_at': '2026-11-13T02:00:00+00:00',
            'desired_updated_by': 'operator',
            'reported_updated_at': '2026-11-13T02:01:00+00:00',
          }),
        );
      }),
    );

    final shadow = await client.fetchDeviceShadow(
      token: 'token',
      deviceId: 'dev/0001',
    );

    expect(captured.method, 'GET');
    expect(
      captured.url,
      Uri.parse('http://api.example:8000/api/devices/dev%2F0001/shadow'),
    );
    expect(captured.headers['Authorization'], 'Bearer token');
    expect(shadow.deviceId, 'dev/0001');
    expect(shadow.desired['report_interval'], 30);
    expect(shadow.reported['report_interval'], 10);
    expect(shadow.version, 7);
    expect(shadow.reportedVersion, 6);
    expect(shadow.syncStatus, 'pending');
    expect(shadow.diffKeys, <String>['report_interval']);
    expect(shadow.pendingDesired, <String, dynamic>{'report_interval': 30});
    expect(shadow.desiredUpdatedBy, 'operator');
    expect(shadow.desiredUpdatedAt, DateTime.utc(2026, 11, 13, 2));
    expect(shadow.reportedUpdatedAt, DateTime.utc(2026, 11, 13, 2, 1));
  });

  test('fetchDeviceShadowHistory 保留分页、认证并解析完整快照', () async {
    late http.Request captured;
    final client = ApiClient(
      baseUrl: 'http://api.example:8000',
      httpClient: MockClient((request) async {
        captured = request;
        return _jsonResponse(
          _ok(<String, dynamic>{
            'items': <dynamic>[
              <String, dynamic>{
                'id': 8,
                'device_id': 'dev/0001',
                'version': 3,
                'desired': <String, dynamic>{
                  'led': 'on',
                  'report_interval': 30,
                },
                'changed_keys': <String>['led'],
                'action': 'rollback',
                'source_version': 1,
                'created_at': '2026-11-13T09:00:00+00:00',
                'created_by': 'operator',
              },
            ],
            'total': 24,
            'limit': 20,
            'offset': 20,
          }),
        );
      }),
    );

    final history = await client.fetchDeviceShadowHistory(
      token: 'token',
      deviceId: 'dev/0001',
      limit: 20,
      offset: 20,
    );

    expect(captured.method, 'GET');
    expect(
      captured.url,
      Uri.parse(
        'http://api.example:8000/api/devices/dev%2F0001/shadow/history?limit=20&offset=20',
      ),
    );
    expect(captured.headers['Authorization'], 'Bearer token');
    expect(history.total, 24);
    expect(history.limit, 20);
    expect(history.offset, 20);
    expect(history.items, hasLength(1));
    final revision = history.items.single;
    expect(revision.id, 8);
    expect(revision.version, 3);
    expect(revision.desired, <String, dynamic>{
      'led': 'on',
      'report_interval': 30,
    });
    expect(revision.changedKeys, <String>['led']);
    expect(revision.action, 'rollback');
    expect(revision.sourceVersion, 1);
    expect(revision.createdBy, 'operator');
    expect(revision.createdAt, DateTime.utc(2026, 11, 13, 9));
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
    // 回归：默认不传 offset，请求参数与历史保持一致
    expect(captured.url.queryParameters.containsKey('offset'), isFalse);
    expect(captured.headers['Authorization'], 'Bearer token');
  });

  test('单设备历史记录翻页时携带 offset', () async {
    late http.Request captured;
    final client = ApiClient(
      baseUrl: 'http://api.example:8000',
      httpClient: MockClient((request) async {
        captured = request;
        return _jsonResponse(_ok(const <dynamic>[]));
      }),
    );

    final records = await client.fetchDeviceRecords(
      token: 'token',
      deviceId: 'dev-0001',
      limit: 10,
      offset: 10,
    );

    expect(records, isEmpty);
    expect(captured.url.queryParameters['limit'], '10');
    expect(captured.url.queryParameters['offset'], '10');
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

    final page = await client.fetchDevices(token: 'token');

    expect(page.items, hasLength(1));
    expect(page.items.first.deviceId, 'dev-0001');
    expect(page.items.first.online, isTrue);
    expect(page.total, 2);
    expect(page.pageSize, 20);
  });

  test('设备分页/筛选/聚合告警参数正确下发', () async {
    late http.Request captured;
    final client = ApiClient(
      baseUrl: 'http://api.example:8000',
      httpClient: MockClient((request) async {
        captured = request;
        return _jsonResponse(
          _ok(<String, dynamic>{
            'items': <dynamic>[],
            'total': 0,
            'page': 3,
            'page_size': 20,
          }),
        );
      }),
    );

    await client.fetchDevices(
      token: 'token',
      page: 3,
      pageSize: 20,
      status: 'online',
      keyword: 'dev',
      hasAlert: true,
      withAlertCount: true,
    );

    expect(captured.url.path, '/api/devices');
    expect(captured.url.queryParameters, <String, String>{
      'page': '3',
      'pageSize': '20',
      'status': 'online',
      'keyword': 'dev',
      'hasAlert': 'true',
      'withAlertCount': 'true',
    });
  });

  test('v1.35.0 标签筛选按重复参数下发，并解析标签统计', () async {
    late http.Request deviceRequest;
    late http.Request alertRequest;
    final client = ApiClient(
      baseUrl: 'http://api.example:8000',
      httpClient: MockClient((request) async {
        switch (request.url.path) {
          case '/api/devices':
            deviceRequest = request;
            return _jsonResponse(
              _ok(<String, dynamic>{
                'items': <dynamic>[],
                'total': 0,
                'page': 1,
                'page_size': 20,
              }),
            );
          case '/api/alerts':
            alertRequest = request;
            return _jsonResponse(
              _ok(<String, dynamic>{
                'items': <dynamic>[],
                'total': 0,
                'page': 1,
                'page_size': 20,
              }),
            );
          default:
            return _jsonResponse(
              _ok(<dynamic>[
                <String, dynamic>{'tag': '冷库', 'device_count': 3},
              ]),
            );
        }
      }),
    );

    await client.fetchDevices(token: 'token', tags: const <String>['冷库', '常温']);
    // Uri.replace 把 List<String> 展开为重复参数，后端 Optional[List[str]] 才能收到多值
    expect(deviceRequest.url.queryParametersAll['tag'], <String>['冷库', '常温']);

    await client.fetchAlerts(token: 'token', tags: const <String>['冷库']);
    expect(alertRequest.url.queryParametersAll['tag'], <String>['冷库']);

    final tags = await client.fetchDeviceTags(token: 'token');
    expect(tags.single.tag, '冷库');
    expect(tags.single.deviceCount, 3);
  });

  test('deviceIds 为空时跳过最新值请求', () async {
    var calls = 0;
    final client = ApiClient(
      baseUrl: 'http://api.example:8000',
      httpClient: MockClient((_) async {
        calls += 1;
        return _jsonResponse(_ok(const <dynamic>[]));
      }),
    );

    final rows = await client.fetchLatestReadings(
      token: 'token',
      deviceIds: const <String>[],
    );

    expect(rows, isEmpty);
    expect(calls, 0);
  });
}
