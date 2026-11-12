import 'dart:async';
import 'dart:convert';

import 'package:http/http.dart' as http;

import '../models/mobile_api_models.dart';

/// 401 时的续期抽象：由页面持有单飞协调器，ApiClient 不关心并发与存储。
abstract class SessionRefresher {
  /// 用失效的 access 触发一次续期；成功返回新会话，失败返回 null。
  Future<AuthSession?> refresh(String staleAccessToken);
}

/// 移动看板的 REST 客户端：只读复用网关既有接口。
///
/// 统一处理三类可预期故障并给出可读文案：
/// 401（token 失效，先尝试单飞 refresh 再重放一次）、超时、网络不可达；
/// 业务错误沿用网关 message。
class ApiClient {
  ApiClient({
    required this.baseUrl,
    http.Client? httpClient,
    this.timeout = const Duration(seconds: 8),
    this.refresher,
  }) : _client = httpClient ?? http.Client();

  final String baseUrl;
  final Duration timeout;
  final http.Client _client;

  /// 为空时保持旧行为：401 直接抛 unauthorized，由页面清会话回登录页。
  final SessionRefresher? refresher;

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
    return _sessionFrom(data, fallbackUsername: username);
  }

  /// 用 refresh 换取新（access, refresh）；网关会轮换并让旧 refresh 失效。
  Future<AuthSession> refreshSession({required String refreshToken}) async {
    final data = await _send(
      'POST',
      '/api/auth/refresh',
      body: <String, dynamic>{'refresh_token': refreshToken},
      allowRefresh: false,
    );
    return _sessionFrom(data);
  }

  /// 登出：吊销服务端 refresh（幂等）；本地清理由调用方负责。
  Future<void> logoutSession({required String refreshToken}) async {
    await _send(
      'POST',
      '/api/auth/logout',
      body: <String, dynamic>{'refresh_token': refreshToken},
      allowRefresh: false,
    );
  }

  AuthSession _sessionFrom(dynamic data, {String fallbackUsername = ''}) {
    if (data is! Map<String, dynamic>) {
      throw const ApiException(ApiErrorKind.invalidResponse, '认证响应缺少 data');
    }
    final user = data['user'];
    return AuthSession(
      token: data['token']?.toString() ?? '',
      refreshToken: data['refresh_token']?.toString() ?? '',
      username: user is Map
          ? user['username']?.toString() ?? fallbackUsername
          : fallbackUsername,
      role: user is Map ? user['role']?.toString() ?? 'viewer' : 'viewer',
    );
  }

  /// 登录后换取 9001 只读 MQTT 凭证；Broker 已关闭匿名，不得降级为匿名连接。
  Future<MqttClientCredential> fetchMqttCredentials({
    required String token,
    String client = 'mobile',
  }) async {
    final data = await _send(
      'GET',
      '/api/auth/mqtt-credentials',
      token: token,
      query: <String, String>{'client': client},
    );
    if (data is! Map<String, dynamic>) {
      throw const ApiException(ApiErrorKind.invalidResponse, '凭证响应缺少 data');
    }
    return MqttClientCredential.fromJson(data);
  }

  /// 服务端分页设备列表。
  ///
  /// `hasAlert` 由网关按未处理告警聚合筛选；`withAlertCount` 返回每设备告警数，
  /// 两者都不受告警列表 pageSize 影响。关键词与状态同样转为请求参数。
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
    final data = await _send(
      'GET',
      '/api/devices',
      token: token,
      query: <String, dynamic>{
        'page': '$page',
        'pageSize': '$pageSize',
        if (status != null && status.isNotEmpty) 'status': status,
        if (keyword != null && keyword.isNotEmpty) 'keyword': keyword,
        if (group != null && group.isNotEmpty) 'group': group,
        // v1.35.0：重复参数 tag=a&tag=b，服务端按「或」匹配
        if (tags != null && tags.isNotEmpty) 'tag': tags,
        if (hasAlert) 'hasAlert': 'true',
        if (withAlertCount) 'withAlertCount': 'true',
      },
    );
    if (data is! Map<String, dynamic>) {
      throw const ApiException(ApiErrorKind.invalidResponse, '设备列表响应缺少 data');
    }
    return DevicePage.fromJson(data);
  }

  /// 设备影子只读视图；同步状态与差异直接使用服务端派生结果。
  Future<DeviceShadow> fetchDeviceShadow({
    required String token,
    required String deviceId,
  }) async {
    final data = await _send(
      'GET',
      '/api/devices/${Uri.encodeComponent(deviceId)}/shadow',
      token: token,
    );
    if (data is! Map<String, dynamic>) {
      throw const ApiException(ApiErrorKind.invalidResponse, '设备影子响应缺少 data');
    }
    return DeviceShadow.fromJson(data);
  }

  /// 签发/轮换设备凭证：口令只在本次响应出现一次，调用方必须立即展示并清空。
  Future<DeviceCredentialResult> issueDeviceCredential({
    required String token,
    required String deviceId,
  }) async {
    final data = await _send(
      'POST',
      '/api/devices/$deviceId/mqtt-credential',
      token: token,
    );
    if (data is! Map<String, dynamic>) {
      throw const ApiException(ApiErrorKind.invalidResponse, '凭证响应缺少 data');
    }
    return DeviceCredentialResult.fromJson(data);
  }

  /// 吊销设备凭证（仅 admin）：吊销后设备须重新签发才能接入 Broker。
  Future<DeviceCredentialRevokeResult> revokeDeviceCredential({
    required String token,
    required String deviceId,
  }) async {
    final data = await _send(
      'DELETE',
      '/api/devices/$deviceId/mqtt-credential',
      token: token,
    );
    if (data is! Map<String, dynamic>) {
      throw const ApiException(ApiErrorKind.invalidResponse, '吊销响应缺少 data');
    }
    return DeviceCredentialRevokeResult.fromJson(data);
  }

  /// 分组概览（每组总数 / 在线 / 离线），用于看板分组筛选。
  Future<List<DeviceGroup>> fetchGroups({required String token}) async {
    final data = await _send('GET', '/api/devices/groups', token: token);
    return _mapList(data, DeviceGroup.fromJson);
  }

  /// v1.35.0 标签列表（含每个标签下的设备数），用于看板与告警中心的标签筛选。
  Future<List<DeviceTagStat>> fetchDeviceTags({required String token}) async {
    final data = await _send('GET', '/api/devices/tags', token: token);
    return _mapList(data, DeviceTagStat.fromJson);
  }

  /// 每设备最新一条上报。
  ///
  /// 传入 [deviceIds] 时只取当前已加载页的设备，避免一次拉取全量设备最新值；
  /// 传空列表直接返回空，不发请求。
  Future<List<SensorReading>> fetchLatestReadings({
    required String token,
    List<String>? deviceIds,
  }) async {
    if (deviceIds != null && deviceIds.isEmpty) {
      return const <SensorReading>[];
    }
    final data = await _send(
      'GET',
      '/api/records/latest',
      token: token,
      query: deviceIds == null
          ? null
          : <String, String>{'deviceIds': deviceIds.join(',')},
    );
    return _mapList(data, SensorReading.fromJson);
  }

  /// 单设备历史上报，用于看板详情页的分页列表。
  ///
  /// 网关按时间倒序取数后翻转返回；[offset] 从最新一条开始跳过，
  /// 默认 0 时请求参数与历史一致（只带 limit），不改变既有行为。
  Future<List<SensorReading>> fetchDeviceRecords({
    required String token,
    required String deviceId,
    int limit = 10,
    int offset = 0,
  }) async {
    final data = await _send(
      'GET',
      '/api/devices/$deviceId/records',
      token: token,
      query: <String, String>{
        'limit': '$limit',
        if (offset > 0) 'offset': '$offset',
      },
    );
    return _mapList(data, SensorReading.fromJson);
  }

  /// 未处理告警分页列表，`total` 为服务端全量未处理数。
  Future<AlertPage> fetchOpenAlerts({
    required String token,
    int page = 1,
    int pageSize = 20,
  }) async {
    final data = await _send(
      'GET',
      '/api/alerts',
      token: token,
      query: <String, String>{
        'status': 'open',
        'page': '$page',
        'pageSize': '$pageSize',
      },
    );
    if (data is! Map<String, dynamic>) {
      throw const ApiException(ApiErrorKind.invalidResponse, '告警响应缺少 data');
    }
    return AlertPage.fromJson(data);
  }

  /// 告警中心使用的分页查询：level / status / deviceId 均可组合。
  ///
  /// 与 [fetchOpenAlerts] 并存：后者保持看板既有调用语义不变，
  /// 本方法只用于新页面，空参数时不带任何筛选，行为等同网关默认查询。
  Future<AlertPage> fetchAlerts({
    required String token,
    int page = 1,
    int pageSize = 20,
    String? deviceId,
    String? level,
    String? status,
    List<String>? tags,
  }) async {
    final data = await _send(
      'GET',
      '/api/alerts',
      token: token,
      query: <String, dynamic>{
        'page': '$page',
        'pageSize': '$pageSize',
        if (deviceId != null && deviceId.isNotEmpty) 'deviceId': deviceId,
        if (level != null && level.isNotEmpty) 'level': level,
        if (status != null && status.isNotEmpty) 'status': status,
        // v1.35.0：重复参数 tag=a&tag=b，与页面筛选组合生效
        if (tags != null && tags.isNotEmpty) 'tag': tags,
      },
    );
    if (data is! Map<String, dynamic>) {
      throw const ApiException(ApiErrorKind.invalidResponse, '告警响应缺少 data');
    }
    return AlertPage.fromJson(data);
  }

  /// 告警汇总：用于告警中心角标与打开告警总数兜底。
  Future<AlertSummary> fetchAlertSummary({required String token}) async {
    final data = await _send('GET', '/api/alerts/summary', token: token);
    if (data is! Map<String, dynamic>) {
      throw const ApiException(ApiErrorKind.invalidResponse, '告警汇总响应缺少 data');
    }
    return AlertSummary.fromJson(data);
  }

  /// 批量确认：一次请求提交多条，服务端逐类返回部分失败。
  ///
  /// 不采用 N 次单条请求，避免网络放大与部分提交不可见；
  /// 权限与单条确认一致，viewer 会被网关 403。
  /// v1.21.0 增加可选 `note`（处置备注），与 Web 使用同一份请求体语义。
  Future<BulkAckResult> bulkAckAlerts({
    required String token,
    required List<String> alertIds,
    String? note,
  }) async {
    final text = note?.trim();
    final data = await _send(
      'POST',
      '/api/alerts/bulk-ack',
      token: token,
      body: <String, dynamic>{
        'alert_ids': alertIds,
        if (text != null && text.isNotEmpty) 'note': text,
      },
    );
    if (data is! Map<String, dynamic>) {
      throw const ApiException(ApiErrorKind.invalidResponse, '批量确认响应缺少 data');
    }
    return BulkAckResult.fromJson(data);
  }

  /// 看板汇总卡的服务端全量统计（设备总数 / 在线数 / 未处理告警数）。
  Future<StatsOverview> fetchStatsOverview({required String token}) async {
    final data = await _send('GET', '/api/stats/overview', token: token);
    if (data is! Map<String, dynamic>) {
      throw const ApiException(ApiErrorKind.invalidResponse, '统计响应缺少 data');
    }
    return StatsOverview.fromJson(data);
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
    // 值可为 String 或 List<String>；Uri.replace 会把列表展开为重复查询参数
    Map<String, dynamic>? query,
    bool allowRefresh = true,
  }) async {
    final uri = Uri.parse('$baseUrl$path').replace(queryParameters: query);

    for (var attempt = 0; ; attempt += 1) {
      final headers = <String, String>{
        'Accept': 'application/json',
        if (body != null) 'Content-Type': 'application/json',
        if (token != null && token.isNotEmpty) 'Authorization': 'Bearer $token',
      };

      http.Response response;
      try {
        if (method == 'POST') {
          response = await _client
              .post(uri, headers: headers, body: jsonEncode(body ?? const {}))
              .timeout(timeout);
        } else if (method == 'DELETE') {
          response = await _client
              .delete(uri, headers: headers)
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
        throw const ApiException(
          ApiErrorKind.network,
          '无法连接网关，请确认地址、后端进程与网络连通性',
        );
      }

      try {
        // 解码/业务异常只在传输成功后才产生，交给调用方按 ApiErrorKind 处理
        return _decode(response);
      } on ApiException catch (error) {
        final canRetry =
            error.kind == ApiErrorKind.unauthorized &&
            attempt == 0 &&
            allowRefresh &&
            refresher != null &&
            token != null &&
            token.isNotEmpty;
        if (!canRetry) rethrow;
        // 单飞协调器保证并发 401 只发一次 refresh；失败则返回 null → 保留 401 语义
        final renewed = await refresher!.refresh(token);
        if (renewed == null || renewed.token.isEmpty) rethrow;
        token = renewed.token;
      }
    }
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
