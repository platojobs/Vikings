import 'dart:async';
import 'dart:convert';

import 'package:dio/dio.dart';
import 'package:flutter/foundation.dart';

import '../models/mobile_api_models.dart';

/// 401 时的续期抽象：由页面持有单飞协调器，ApiClient 不关心并发与存储。
abstract class SessionRefresher {
  /// 用失效的 access 触发一次续期；成功返回新会话，失败返回 null。
  Future<AuthSession?> refresh(String staleAccessToken);
}

/// 页面级取消令牌：页面持有一个，创建的每个 [ApiClient] 都绑定它，
/// dispose 时一次性取消在途请求，避免回写已卸载的 State。
class RequestCancellation {
  final CancelToken token = CancelToken();

  bool get isCancelled => token.isCancelled;

  void cancel([String? reason]) {
    if (!token.isCancelled) {
      token.cancel(reason ?? '页面已销毁');
    }
  }
}

/// 拦截器之间传递上下文的键，避免把 token 塞进日志或查询串。
const String _kAuthToken = 'authToken';
const String _kAllowRefresh = 'allowRefresh';
const String _kAuthRetried = 'authRetried';
const String _kRetryAttempt = 'retryAttempt';
const String _kLogStartedAt = 'logStartedAt';

/// 移动看板的 REST 客户端：只读复用网关既有接口。
///
/// 横切逻辑都放在 dio 拦截器里：
/// - 认证头统一注入（access 来自安全存储，由页面传入，不在页面手写 Header）；
/// - 401 先走页面的单飞 [SessionRefresher]，成功后重放一次，失败保留 401 语义；
/// - 错误统一映射为 [ApiErrorKind]，页面既有分支不变；
/// - 仅幂等 GET 做有限次退避重试，写操作与 4xx 一律不重试；
/// - 日志只记录方法、路径、状态与耗时，不输出 token、口令或 Authorization。
class ApiClient {
  ApiClient({
    required this.baseUrl,
    Dio? dio,
    this.timeout = const Duration(seconds: 8),
    this.refresher,
    RequestCancellation? cancellation,
    bool enableLogging = false,
  }) : _dio = dio ?? Dio() {
    _cancelToken = cancellation?.token;
    _dio.options = BaseOptions(
      baseUrl: baseUrl,
      connectTimeout: timeout,
      receiveTimeout: timeout,
      sendTimeout: timeout,
      responseType: ResponseType.bytes,
      listFormat: ListFormat.multi,
      // 4xx/5xx 交给拦截器按网关 detail 统一映射，避免 dio 默认文案丢掉中文原因
      validateStatus: (status) =>
          status != null && status >= 200 && status < 300,
    );
    _dio.interceptors.addAll(<Interceptor>[
      _AuthInterceptor(),
      _UnauthorizedInterceptor(dio: _dio, refresher: refresher),
      _GetRetryInterceptor(dio: _dio),
      _ErrorMappingInterceptor(),
      if (enableLogging) _SanitizedLogInterceptor(),
    ]);
  }

  final String baseUrl;
  final Duration timeout;

  /// 为空时保持旧行为：401 直接抛 unauthorized，由页面清会话回登录页。
  final SessionRefresher? refresher;

  final Dio _dio;
  CancelToken? _cancelToken;

  /// 页面销毁时取消在途请求：客户端由页面创建，取消令牌挂在页面上。
  void bindCancellation(RequestCancellation cancellation) {
    _cancelToken = cancellation.token;
  }

  /// 仅用于测试断言超时配置，业务代码不要直接操作 dio。
  @visibleForTesting
  Dio get dio => _dio;

  void close() => _dio.close(force: true);

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

  /// 设备影子版本历史，只在用户展开时请求；移动端不提供回滚写操作。
  Future<DeviceShadowHistory> fetchDeviceShadowHistory({
    required String token,
    required String deviceId,
    int limit = 20,
    int offset = 0,
  }) async {
    final data = await _send(
      'GET',
      '/api/devices/${Uri.encodeComponent(deviceId)}/shadow/history',
      token: token,
      query: <String, dynamic>{'limit': '$limit', 'offset': '$offset'},
    );
    if (data is! Map<String, dynamic>) {
      throw const ApiException(ApiErrorKind.invalidResponse, '影子历史响应缺少 data');
    }
    return DeviceShadowHistory.fromJson(data);
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
    // 值可为 String 或 List<String>；dio 的 ListFormat.multi 展开为重复查询参数
    Map<String, dynamic>? query,
    bool allowRefresh = true,
  }) async {
    final options = Options(
      method: method.toUpperCase(),
      extra: <String, dynamic>{
        _kAuthToken: token,
        _kAllowRefresh: allowRefresh,
      },
    );
    try {
      final response = await _dio.request<List<int>>(
        path,
        // 与旧实现保持一致：POST 无 body 时发送空对象，DELETE/GET 不带 body
        data: body ?? (method.toUpperCase() == 'POST' ? const <String, dynamic>{} : null),
        queryParameters: query,
        options: options,
        cancelToken: _cancelToken,
      );
      return _decodePayload(response.data, response.statusCode ?? 0);
    } on DioException catch (error) {
      final mapped = error.error;
      if (mapped is ApiException) {
        throw mapped;
      }
      if (error.type == DioExceptionType.cancel) {
        throw const ApiException(ApiErrorKind.cancelled, '请求已取消');
      }
      throw ApiException(
        ApiErrorKind.network,
        error.message ?? '无法连接网关，请确认地址、后端进程与网络连通性',
      );
    }
  }

  /// 仅处理 2xx：状态码错误由 [_ErrorMappingInterceptor] 统一映射。
  dynamic _decodePayload(List<int>? bytes, int statusCode) {
    final envelope = _decodeEnvelopeBytes(bytes);
    if (statusCode >= 400) {
      throw _httpStatusException(statusCode, envelope);
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

/// 按字节解码网关 JSON（UTF-8），失败返回 null。
Map<String, dynamic>? _decodeEnvelopeBytes(List<int>? bytes) {
  if (bytes == null || bytes.isEmpty) {
    return null;
  }
  try {
    final decoded = jsonDecode(utf8.decode(bytes));
    return decoded is Map<String, dynamic> ? decoded : null;
  } catch (_) {
    return null;
  }
}

/// 状态码 → 既有错误分类，页面按 [ApiErrorKind] 的既有分支不受影响。
ApiException _httpStatusException(
  int statusCode,
  Map<String, dynamic>? envelope,
) {
  switch (statusCode) {
    case 401:
      return const ApiException(
        ApiErrorKind.unauthorized,
        '登录已失效，请重新登录',
        statusCode: 401,
      );
    case 403:
      return const ApiException(
        ApiErrorKind.forbidden,
        '当前账号无权访问该数据',
        statusCode: 403,
      );
    case 409:
      return ApiException(
        ApiErrorKind.conflict,
        envelope?['detail']?.toString() ?? '操作冲突，请刷新后重试',
        statusCode: 409,
      );
    default:
      if (statusCode >= 400) {
        final detail =
            envelope?['detail']?.toString() ?? envelope?['message']?.toString();
        return ApiException(
          ApiErrorKind.server,
          detail ?? '网关请求失败（HTTP $statusCode）',
          statusCode: statusCode,
        );
      }
      return ApiException(
        ApiErrorKind.network,
        '网关返回异常（HTTP $statusCode）',
        statusCode: statusCode,
      );
  }
}

/// 认证头统一注入：页面只传 access（来自安全存储），不再手写 Header。
class _AuthInterceptor extends Interceptor {
  @override
  void onRequest(RequestOptions options, RequestInterceptorHandler handler) {
    options.headers['Accept'] = 'application/json';
    final token = options.extra[_kAuthToken];
    if (token is String && token.isNotEmpty) {
      options.headers['Authorization'] = 'Bearer $token';
    } else {
      options.headers.remove('Authorization');
    }
    handler.next(options);
  }
}

/// 401 单飞续期：并发多个 401 共用页面持有的协调器，只发一次 refresh，
/// 其余请求拿到新 access 后各自重放；refresh 失败保留 401，由页面清会话回登录页。
class _UnauthorizedInterceptor extends Interceptor {
  _UnauthorizedInterceptor({required this.dio, required this.refresher});

  final Dio dio;
  final SessionRefresher? refresher;

  @override
  Future<void> onError(
    DioException err,
    ErrorInterceptorHandler handler,
  ) async {
    final options = err.requestOptions;
    final token = options.extra[_kAuthToken];
    final canRefresh =
        err.response?.statusCode == 401 &&
        refresher != null &&
        options.extra[_kAllowRefresh] != false &&
        options.extra[_kAuthRetried] != true &&
        token is String &&
        token.isNotEmpty;
    if (!canRefresh) {
      handler.next(err);
      return;
    }
    final renewed = await refresher!.refresh(token);
    if (renewed == null || renewed.token.isEmpty) {
      handler.next(err);
      return;
    }
    final replay = options.copyWith(
      extra: <String, dynamic>{
        ...options.extra,
        _kAuthToken: renewed.token,
        _kAuthRetried: true,
      },
    );
    try {
      handler.resolve(await dio.fetch<List<int>>(replay));
    } on DioException catch (error) {
      handler.next(error);
    }
  }
}

/// 仅幂等 GET 做有限次退避重试；POST/DELETE 与所有 4xx 都不重试，
/// 避免指令下发、告警确认等写操作产生重复副作用。
class _GetRetryInterceptor extends Interceptor {
  _GetRetryInterceptor({required this.dio});

  static const int maxRetries = 2;

  final Dio dio;

  @override
  Future<void> onError(
    DioException err,
    ErrorInterceptorHandler handler,
  ) async {
    final options = err.requestOptions;
    final attempt = options.extra[_kRetryAttempt] is int
        ? options.extra[_kRetryAttempt] as int
        : 0;
    final canRetry =
        options.method.toUpperCase() == 'GET' &&
        attempt < maxRetries &&
        _isRetryable(err);
    if (!canRetry) {
      handler.next(err);
      return;
    }
    await Future<void>.delayed(Duration(milliseconds: 200 * (1 << attempt)));
    final replay = options.copyWith(
      extra: <String, dynamic>{...options.extra, _kRetryAttempt: attempt + 1},
    );
    try {
      handler.resolve(await dio.fetch<List<int>>(replay));
    } on DioException catch (error) {
      handler.next(error);
    }
  }

  bool _isRetryable(DioException err) {
    switch (err.type) {
      case DioExceptionType.connectionTimeout:
      case DioExceptionType.sendTimeout:
      case DioExceptionType.receiveTimeout:
      case DioExceptionType.connectionError:
        return true;
      case DioExceptionType.badResponse:
        // 只看 5xx；4xx 是确定性失败，重试没有意义
        return (err.response?.statusCode ?? 0) >= 500;
      case DioExceptionType.cancel:
      case DioExceptionType.badCertificate:
      case DioExceptionType.transformTimeout:
      case DioExceptionType.unknown:
        return false;
    }
  }
}

/// 把 dio 异常统一收敛为 [ApiException]，页面原有 catch 分支无需改动。
class _ErrorMappingInterceptor extends Interceptor {
  @override
  void onError(DioException err, ErrorInterceptorHandler handler) {
    final mapped = err.error;
    if (mapped is ApiException) {
      handler.next(err);
      return;
    }
    handler.reject(err.copyWith(error: _map(err)), true);
  }

  ApiException _map(DioException err) {
    switch (err.type) {
      case DioExceptionType.connectionTimeout:
      case DioExceptionType.sendTimeout:
      case DioExceptionType.receiveTimeout:
      case DioExceptionType.transformTimeout:
        return const ApiException(
          ApiErrorKind.timeout,
          '网关响应超时，请检查网络后重试',
        );
      case DioExceptionType.cancel:
        return const ApiException(ApiErrorKind.cancelled, '请求已取消');
      case DioExceptionType.connectionError:
      case DioExceptionType.unknown:
        return const ApiException(
          ApiErrorKind.network,
          '无法连接网关，请确认地址、后端进程与网络连通性',
        );
      case DioExceptionType.badCertificate:
        return const ApiException(
          ApiErrorKind.network,
          '网关证书校验失败，请检查网络环境',
        );
      case DioExceptionType.badResponse:
        final status = err.response?.statusCode ?? 0;
        final data = err.response?.data;
        return _httpStatusException(
          status,
          data is List<int> ? _decodeEnvelopeBytes(data) : null,
        );
    }
  }
}

/// 只记录方法、去参数路径、状态与耗时；绝不输出请求头、请求体、token 或口令。
class _SanitizedLogInterceptor extends Interceptor {
  @override
  void onRequest(RequestOptions options, RequestInterceptorHandler handler) {
    options.extra[_kLogStartedAt] = DateTime.now();
    handler.next(options);
  }

  @override
  void onResponse(
    Response<dynamic> response,
    ResponseInterceptorHandler handler,
  ) {
    _log(response.requestOptions, '${response.statusCode}', null);
    handler.next(response);
  }

  @override
  void onError(DioException err, ErrorInterceptorHandler handler) {
    _log(
      err.requestOptions,
      err.response?.statusCode?.toString(),
      err.type.name,
    );
    handler.next(err);
  }

  void _log(RequestOptions options, String? status, String? failure) {
    final started = options.extra[_kLogStartedAt];
    final elapsed = started is DateTime
        ? '${DateTime.now().difference(started).inMilliseconds}ms'
        : '-';
    debugPrint(
      '[api] ${options.method} ${options.uri.path} → ${status ?? failure ?? '-'} $elapsed',
    );
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
  cancelled,
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
    ApiErrorKind.cancelled => '请求已取消',
  };

  @override
  String toString() => 'ApiException(${kind.name}, $message)';
}
