import 'dart:async';
import 'dart:convert';

import 'package:dio/dio.dart' show DioExceptionType;
import 'package:flutter/foundation.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:iot_device_app/services/api_client.dart';

import 'support/dio_mock.dart';

void main() {
  test('login 解析 token / 角色，并正确拼接地址与请求体', () async {
    final adapter = MockDioAdapter(
      (_) async => jsonResponse(
        okEnvelope(<String, dynamic>{
          'token': 'jwt-token',
          'user': <String, dynamic>{'username': 'operator', 'role': 'operator'},
        }),
      ),
    );
    final client = mockApiClient(adapter);

    final session = await client.login(
      username: 'operator',
      password: 'operator123',
    );

    expect(session.token, 'jwt-token');
    expect(session.username, 'operator');
    expect(session.role, 'operator');
    expect(adapter.last.uri.toString(), 'http://api.example:8000/api/auth/login');
    expect(adapter.last.header('content-type'), startsWith('application/json'));
    expect(jsonDecode(adapter.last.body)['password'], 'operator123');
  });

  test('未登录请求不携带 Authorization，带 token 时注入 Bearer', () async {
    final adapter = MockDioAdapter((request) async {
      if (request.uri.path == '/api/auth/login') {
        return jsonResponse(
          okEnvelope(<String, dynamic>{
            'token': 'jwt-token',
            'user': <String, dynamic>{'username': 'admin', 'role': 'admin'},
          }),
        );
      }
      return jsonResponse(
        okEnvelope(<String, dynamic>{
          'items': <dynamic>[],
          'total': 0,
          'page': 1,
          'page_size': 20,
        }),
      );
    });
    final client = mockApiClient(adapter);

    await client.login(username: 'admin', password: 'admin123');
    expect(adapter.last.header('authorization'), isNull);

    await client.fetchDevices(token: 'access-1');
    expect(adapter.last.header('authorization'), 'Bearer access-1');
  });

  test('401 统一映射为 unauthorized，便于看板引导重新登录', () async {
    final client = mockApiClient(
      MockDioAdapter(
        (_) async => jsonResponse(<String, String>{'detail': '登录已失效'}, 401),
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
    final client = mockApiClient(
      MockDioAdapter(
        (_) async => throw const MockDioFailure(
          DioExceptionType.receiveTimeout,
          message: 'timeout',
        ),
      ),
      timeout: const Duration(milliseconds: 50),
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
    final client = mockApiClient(
      MockDioAdapter(
        (_) async => throw const MockDioFailure(
          DioExceptionType.connectionError,
          message: 'connection refused',
        ),
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
    final client = mockApiClient(
      MockDioAdapter(
        (_) async => MockResponse(
          jsonEncode(<String, dynamic>{'detail': '设备不存在'}),
          statusCode: 404,
          // 故意不带 content-type，模拟部分 FastAPI/代理响应
          headers: const <String, List<String>>{},
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
    final adapter = MockDioAdapter(
      (_) async => jsonResponse(
        okEnvelope(<String, dynamic>{
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
      ),
    );
    final client = mockApiClient(adapter);

    final shadow = await client.fetchDeviceShadow(
      token: 'token',
      deviceId: 'dev/0001',
    );

    expect(adapter.last.method, 'GET');
    expect(
      adapter.last.uri,
      Uri.parse('http://api.example:8000/api/devices/dev%2F0001/shadow'),
    );
    expect(adapter.last.header('authorization'), 'Bearer token');
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
    final adapter = MockDioAdapter(
      (_) async => jsonResponse(
        okEnvelope(<String, dynamic>{
          'items': <dynamic>[
            <String, dynamic>{
              'id': 8,
              'device_id': 'dev/0001',
              'version': 3,
              'desired': <String, dynamic>{'led': 'on', 'report_interval': 30},
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
      ),
    );
    final client = mockApiClient(adapter);

    final history = await client.fetchDeviceShadowHistory(
      token: 'token',
      deviceId: 'dev/0001',
      limit: 20,
      offset: 20,
    );

    expect(adapter.last.method, 'GET');
    expect(
      adapter.last.uri,
      Uri.parse(
        'http://api.example:8000/api/devices/dev%2F0001/shadow/history?limit=20&offset=20',
      ),
    );
    expect(adapter.last.header('authorization'), 'Bearer token');
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
    final adapter = MockDioAdapter(
      (_) async => jsonResponse(
        okEnvelope(<dynamic>[
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
      ),
    );
    final client = mockApiClient(adapter);

    final records = await client.fetchDeviceRecords(
      token: 'token',
      deviceId: 'dev-0001',
      limit: 5,
    );

    expect(records, hasLength(1));
    expect(records.first.seq, 12);
    expect(adapter.last.uri.path, '/api/devices/dev-0001/records');
    expect(adapter.last.uri.queryParameters['limit'], '5');
    // 回归：默认不传 offset，请求参数与历史保持一致
    expect(adapter.last.uri.queryParameters.containsKey('offset'), isFalse);
    expect(adapter.last.header('authorization'), 'Bearer token');
  });

  test('单设备历史记录翻页时携带 offset', () async {
    final adapter = MockDioAdapter(
      (_) async => jsonResponse(okEnvelope(const <dynamic>[])),
    );
    final client = mockApiClient(adapter);

    final records = await client.fetchDeviceRecords(
      token: 'token',
      deviceId: 'dev-0001',
      limit: 10,
      offset: 10,
    );

    expect(records, isEmpty);
    expect(adapter.last.uri.queryParameters['limit'], '10');
    expect(adapter.last.uri.queryParameters['offset'], '10');
  });

  test('sendCommand 使用 POST 并携带 type/payload，解析回执', () async {
    final adapter = MockDioAdapter(
      (_) async => jsonResponse(
        okEnvelope(<String, dynamic>{
          'cmd_id': 'cmd-abc',
          'device_id': 'dev-0001',
          'type': 'calibrate',
          'state': 'issued',
          'progress': 0,
          'message': '指令已下发，等待设备回执',
          'operator': 'operator',
          'created_at': '2026-11-04T02:00:00+00:00',
        }),
      ),
    );
    final client = mockApiClient(adapter);

    final command = await client.sendCommand(
      token: 'token',
      deviceId: 'dev-0001',
      type: 'calibrate',
      payload: <String, dynamic>{'temperatureOffset': 1.5, 'humidityOffset': 0},
    );

    expect(adapter.last.method, 'POST');
    expect(adapter.last.uri.path, '/api/devices/dev-0001/commands');
    expect(adapter.last.header('authorization'), 'Bearer token');
    expect(jsonDecode(adapter.last.body), <String, dynamic>{
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
    final client = mockApiClient(
      MockDioAdapter(
        (_) async =>
            jsonResponse(<String, String>{'detail': '告警已被处理'}, 409),
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
    final client = mockApiClient(
      MockDioAdapter(
        (_) async => jsonResponse(<String, String>{'detail': '无操作权限'}, 403),
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

  test('5xx 映射为 server 并保留中文 detail', () async {
    final client = mockApiClient(
      MockDioAdapter(
        (_) async => jsonResponse(<String, String>{'detail': '网关内部错误'}, 502),
      ),
    );

    await expectLater(
      client.sendCommand(
        token: 'token',
        deviceId: 'dev-0001',
        type: 'calibrate',
        payload: const <String, dynamic>{},
      ),
      throwsA(
        isA<ApiException>()
            .having((error) => error.kind, 'kind', ApiErrorKind.server)
            .having((error) => error.message, 'message', '网关内部错误'),
      ),
    );
  });

  test('写操作失败不重试，避免重复下发指令或重复确认告警', () async {
    final adapter = MockDioAdapter(
      (_) async => jsonResponse(<String, String>{'detail': '网关内部错误'}, 500),
    );
    final client = mockApiClient(adapter);

    await expectLater(
      client.sendCommand(
        token: 'token',
        deviceId: 'dev-0001',
        type: 'calibrate',
        payload: const <String, dynamic>{},
      ),
      throwsA(isA<ApiException>()),
    );
    expect(adapter.calls, 1, reason: 'POST 写操作绝不自动重试');

    await expectLater(
      client.ackAlert(token: 'token', alertId: 'alert-001'),
      throwsA(isA<ApiException>()),
    );
    expect(adapter.calls, 2, reason: '确认告警同样只尝试一次');
  });

  test('幂等 GET 在网络错误后按退避重试，最多 2 次', () async {
    var calls = 0;
    final adapter = MockDioAdapter((_) async {
      calls += 1;
      if (calls < 3) {
        throw const MockDioFailure(DioExceptionType.connectionError);
      }
      return jsonResponse(okEnvelope(<String, dynamic>{
        'items': <dynamic>[],
        'total': 0,
        'page': 1,
        'page_size': 20,
      }));
    });
    final client = mockApiClient(adapter);

    final page = await client.fetchDevices(token: 'token');

    expect(calls, 3, reason: '首次 + 2 次退避重试');
    expect(page.items, isEmpty);
  });

  test('4xx 不重试：GET 401 只请求一次', () async {
    final adapter = MockDioAdapter(
      (_) async => jsonResponse(<String, String>{'detail': '登录已失效'}, 401),
    );
    final client = mockApiClient(adapter);

    await expectLater(
      client.fetchDevices(token: 'expired'),
      throwsA(isA<ApiException>()),
    );
    expect(adapter.calls, 1);
  });

  test('页面销毁取消在途请求，不回写已卸载的 State', () async {
    final gate = Completer<MockResponse>();
    final adapter = MockDioAdapter((_) => gate.future);
    final cancellation = RequestCancellation();
    final client = mockApiClient(adapter, cancellation: cancellation);

    final future = client.fetchDevices(token: 'token');
    await Future<void>.delayed(Duration.zero);
    cancellation.cancel();
    gate.complete(jsonResponse(okEnvelope(const <dynamic>[])));

    await expectLater(
      future,
      throwsA(
        isA<ApiException>().having(
          (error) => error.kind,
          'kind',
          ApiErrorKind.cancelled,
        ),
      ),
    );
  });

  test('日志只记录方法、路径与状态，不输出 token 或口令', () async {
    final logs = <String>[];
    final original = debugPrint;
    debugPrint = (String? message, {int? wrapWidth}) {
      if (message != null) logs.add(message);
    };
    addTearDown(() => debugPrint = original);

    final client = mockApiClient(
      MockDioAdapter(
        (_) async => jsonResponse(
          okEnvelope(<String, dynamic>{
            'token': 'jwt-secret-token',
            'user': <String, dynamic>{'username': 'admin', 'role': 'admin'},
          }),
        ),
      ),
      enableLogging: true,
    );
    await client.login(username: 'admin', password: 'secret-password');

    expect(logs, isNotEmpty);
    final joined = logs.join('\n');
    expect(joined, contains('/api/auth/login'));
    expect(joined, isNot(contains('secret-password')));
    expect(joined, isNot(contains('jwt-secret-token')));
    expect(joined.toLowerCase(), isNot(contains('authorization')));
  });

  test('设备列表按 envelope.data.items 解析，忽略非法条目', () async {
    final client = mockApiClient(
      MockDioAdapter(
        (_) async => jsonResponse(
          okEnvelope(<String, dynamic>{
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
    final adapter = MockDioAdapter(
      (_) async => jsonResponse(
        okEnvelope(<String, dynamic>{
          'items': <dynamic>[],
          'total': 0,
          'page': 3,
          'page_size': 20,
        }),
      ),
    );
    final client = mockApiClient(adapter);

    await client.fetchDevices(
      token: 'token',
      page: 3,
      pageSize: 20,
      status: 'online',
      keyword: 'dev',
      hasAlert: true,
      withAlertCount: true,
    );

    expect(adapter.last.uri.path, '/api/devices');
    expect(adapter.last.uri.queryParameters, <String, String>{
      'page': '3',
      'pageSize': '20',
      'status': 'online',
      'keyword': 'dev',
      'hasAlert': 'true',
      'withAlertCount': 'true',
    });
  });

  test('v1.35.0 标签筛选按重复参数下发，并解析标签统计', () async {
    final adapter = MockDioAdapter((request) async {
      switch (request.uri.path) {
        case '/api/devices':
        case '/api/alerts':
          return jsonResponse(
            okEnvelope(<String, dynamic>{
              'items': <dynamic>[],
              'total': 0,
              'page': 1,
              'page_size': 20,
            }),
          );
        default:
          return jsonResponse(
            okEnvelope(<dynamic>[
              <String, dynamic>{'tag': '冷库', 'device_count': 3},
            ]),
          );
      }
    });
    final client = mockApiClient(adapter);

    await client.fetchDevices(token: 'token', tags: const <String>['冷库', '常温']);
    // ListFormat.multi 把 List<String> 展开为重复参数，后端 Optional[List[str]] 才能收到多值
    expect(adapter.requests.first.uri.queryParametersAll['tag'], <String>[
      '冷库',
      '常温',
    ]);

    await client.fetchAlerts(token: 'token', tags: const <String>['冷库']);
    expect(adapter.requests[1].uri.queryParametersAll['tag'], <String>['冷库']);

    final tags = await client.fetchDeviceTags(token: 'token');
    expect(tags.single.tag, '冷库');
    expect(tags.single.deviceCount, 3);
  });

  test('deviceIds 为空时跳过最新值请求', () async {
    final adapter = MockDioAdapter(
      (_) async => jsonResponse(okEnvelope(const <dynamic>[])),
    );
    final client = mockApiClient(adapter);

    final rows = await client.fetchLatestReadings(
      token: 'token',
      deviceIds: const <String>[],
    );

    expect(rows, isEmpty);
    expect(adapter.calls, 0);
  });
}
