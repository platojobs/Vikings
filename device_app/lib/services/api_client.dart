import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;

import '../models/mobile_api_models.dart';

/// 移动看板的 REST 客户端：只读复用网关既有接口，后端零改动。
///
/// 统一处理三类可预期故障并给出可读文案：
/// 401（token 失效）、超时、网络不可达；业务错误沿用网关 message。
class ApiClient {
  ApiClient({
    required this.baseUrl,
    http.Client? httpClient,
    this.timeout = const Duration(seconds: 8),
  }) : _client = httpClient ?? http.Client();

  final String baseUrl;
  final Duration timeout;
  final http.Client _client;

  void close() => _client.close();

  Future<AuthSession> login({
    required String username,
    required String password,
  }) async {
    final data = await _send(
      'POST',
      '/api/auth/login',
      body: <String, dynamic>{'username': username, 'password': password},
    );
    if (data is! Map<String, dynamic>) {
      throw const ApiException(ApiErrorKind.invalidResponse, '登录响应缺少 data');
    }
    final user = data['user'];
    return AuthSession(
      token: data['token']?.toString() ?? '',
      username: user is Map
          ? user['username']?.toString() ?? username
          : username,
      role: user is Map ? user['role']?.toString() ?? 'viewer' : 'viewer',
    );
  }

  Future<List<DeviceSummary>> fetchDevices({required String token}) async {
    final data = await _send(
      'GET',
      '/api/devices',
      token: token,
      query: <String, String>{'page': '1', 'pageSize': '100'},
    );
    if (data is! Map<String, dynamic>) {
      throw const ApiException(ApiErrorKind.invalidResponse, '设备列表响应缺少 data');
    }
    return _mapList(data['items'], DeviceSummary.fromJson);
  }

  Future<List<SensorReading>> fetchLatestReadings({
    required String token,
  }) async {
    final data = await _send('GET', '/api/records/latest', token: token);
    return _mapList(data, SensorReading.fromJson);
  }

  /// 单设备最近若干条上报，用于看板详情页的「最近上报」列表。
  Future<List<SensorReading>> fetchDeviceRecords({
    required String token,
    required String deviceId,
    int limit = 10,
  }) async {
    final data = await _send(
      'GET',
      '/api/devices/$deviceId/records',
      token: token,
      query: <String, String>{'limit': '$limit'},
    );
    return _mapList(data, SensorReading.fromJson);
  }

  Future<List<AlertItem>> fetchOpenAlerts({required String token}) async {
    final data = await _send(
      'GET',
      '/api/alerts',
      token: token,
      query: <String, String>{'status': 'open', 'page': '1', 'pageSize': '50'},
    );
    if (data is! Map<String, dynamic>) {
      throw const ApiException(ApiErrorKind.invalidResponse, '告警响应缺少 data');
    }
    final items = data['items'];
    return _mapList(items, AlertItem.fromJson);
  }

  Future<List<CommandItem>> fetchCommands({
    required String token,
    required String deviceId,
  }) async {
    final data = await _send(
      'GET',
      '/api/devices/$deviceId/commands',
      token: token,
      query: <String, String>{'limit': '20'},
    );
    return _mapList(data, CommandItem.fromJson);
  }

  /// 下发单条指令，复用网关既有 `POST /api/devices/{id}/commands`。
  ///
  /// 网关仅校验 `type` 枚举，payload 的具体参数需由 CommandRequest 先行校验。
  Future<CommandItem> sendCommand({
    required String token,
    required String deviceId,
    required String type,
    required Map<String, dynamic> payload,
  }) async {
    final data = await _send(
      'POST',
      '/api/devices/$deviceId/commands',
      token: token,
      body: <String, dynamic>{'type': type, 'payload': payload},
    );
    if (data is! Map<String, dynamic>) {
      throw const ApiException(ApiErrorKind.invalidResponse, '指令回执响应缺少 data');
    }
    return CommandItem.fromJson(data);
  }

  /// 确认告警，复用网关既有 `POST /api/alerts/{id}/ack`。
  Future<AlertItem> ackAlert({
    required String token,
    required String alertId,
  }) async {
    final data = await _send('POST', '/api/alerts/$alertId/ack', token: token);
    if (data is! Map<String, dynamic>) {
      throw const ApiException(ApiErrorKind.invalidResponse, '告警确认响应缺少 data');
    }
    return AlertItem.fromJson(data);
  }

  Future<dynamic> _send(
    String method,
    String path, {
    String? token,
    Map<String, dynamic>? body,
    Map<String, String>? query,
  }) async {
    final uri = Uri.parse('$baseUrl$path').replace(queryParameters: query);
    final headers = <String, String>{
      'Accept': 'application/json',
      if (body != null) 'Content-Type': 'application/json',
      if (token != null && token.isNotEmpty) 'Authorization': 'Bearer $token',
    };

    http.Response? response;
    try {
      if (method == 'POST') {
        response = await _client
            .post(uri, headers: headers, body: jsonEncode(body ?? const {}))
            .timeout(timeout);
      } else {
        response = await _client.get(uri, headers: headers).timeout(timeout);
      }
    } on TimeoutException {
      throw const ApiException(ApiErrorKind.timeout, '网关响应超时，请检查网络后重试');
    } on http.ClientException catch (error) {
      throw ApiException(ApiErrorKind.network, error.message);
    } catch (_) {
      // 覆盖 SocketException 等平台底层网络异常，统一收敛为可读提示
      throw const ApiException(ApiErrorKind.network, '无法连接网关，请确认地址、后端进程与网络连通性');
    }
    // 解码/业务异常只在传输成功后才产生，交给调用方按 ApiErrorKind 处理
    return _decode(response);
  }

  dynamic _decode(http.Response response) {
    Map<String, dynamic>? envelope;
    try {
      // FastAPI 返回 UTF-8 JSON 但不一定带 charset，必须按字节解码，
      // 否则 http 的默认 latin1 会把中文告警文案解成乱码
      final decoded = jsonDecode(utf8.decode(response.bodyBytes));
      if (decoded is Map<String, dynamic>) {
        envelope = decoded;
      }
    } catch (_) {
      envelope = null;
    }

    if (response.statusCode == 401) {
      throw const ApiException(
        ApiErrorKind.unauthorized,
        '登录已失效，请重新登录',
        statusCode: 401,
      );
    }
    if (response.statusCode == 403) {
      throw const ApiException(
        ApiErrorKind.forbidden,
        '当前账号无权访问该数据',
        statusCode: 403,
      );
    }
    if (response.statusCode == 409) {
      throw ApiException(
        ApiErrorKind.conflict,
        envelope?['detail']?.toString() ?? '操作冲突，请刷新后重试',
        statusCode: 409,
      );
    }
    if (response.statusCode >= 400) {
      final detail =
          envelope?['detail']?.toString() ?? envelope?['message']?.toString();
      throw ApiException(
        ApiErrorKind.server,
        detail ?? '网关请求失败（HTTP ${response.statusCode}）',
        statusCode: response.statusCode,
      );
    }
    if (envelope == null) {
      throw const ApiException(ApiErrorKind.invalidResponse, '网关返回数据格式异常');
    }
    final code = envelope['code'];
    if (code is int && code != 0) {
      throw ApiException(
        ApiErrorKind.server,
        envelope['message']?.toString() ?? '业务错误（code=$code）',
      );
    }
    return envelope['data'];
  }

  List<T> _mapList<T>(dynamic data, T Function(Map<String, dynamic>) parser) {
    if (data is! List) {
      throw const ApiException(ApiErrorKind.invalidResponse, '网关返回的列表结构异常');
    }
    return data
        .whereType<Map>()
        .map((item) => parser(Map<String, dynamic>.from(item)))
        .toList(growable: false);
  }
}

/// 可预期的请求失败类型，UI 直接取 [userMessage] 展示。
enum ApiErrorKind {
  unauthorized,
  forbidden,
  conflict,
  network,
  timeout,
  server,
  invalidResponse,
}

class ApiException implements Exception {
  const ApiException(this.kind, this.message, {this.statusCode});

  final ApiErrorKind kind;
  final String message;
  final int? statusCode;

  String get userMessage => switch (kind) {
    ApiErrorKind.unauthorized => '登录已失效，请重新登录',
    ApiErrorKind.forbidden => '当前账号无权访问该数据',
    ApiErrorKind.conflict => message.isEmpty ? '操作冲突，请刷新后重试' : message,
    ApiErrorKind.network => '无法连接网关，请检查地址、后端进程与网络',
    ApiErrorKind.timeout => '网关响应超时，请稍后重试',
    ApiErrorKind.server => message.isEmpty ? '网关返回异常' : message,
    ApiErrorKind.invalidResponse => '网关返回数据格式异常',
  };

  @override
  String toString() => 'ApiException(${kind.name}, $message)';
}
