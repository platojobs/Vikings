import '../models/mobile_api_models.dart';
import 'api_client.dart';
import 'app_storage.dart';

/// 移动端刷新令牌的**单飞**协调器（v1.24.0）。
///
/// 并发 5 个请求同时 401 时，只有第一个会真正调用 `/api/auth/refresh`，
/// 其余复用同一个 Future；刷新成功后新 access/refresh 会写入安全存储并回传页面。
///
/// 额外优化：如果调用方带着旧的 access 进来，而当前会话已经换过新 access
/// （例如另一个请求刚刷新完），直接返回当前会话，不再发请求。
class SessionRefreshCoordinator implements SessionRefresher {
  SessionRefreshCoordinator({
    required this.storage,
    required this.baseUrl,
    required this.readSession,
    required this.onRefreshed,
    ApiClient Function(String baseUrl)? clientBuilder,
  }) : clientBuilder = clientBuilder ?? ((url) => ApiClient(baseUrl: url));

  final AppStorage storage;
  final String Function() baseUrl;
  final AuthSession? Function() readSession;
  final void Function(AuthSession session) onRefreshed;
  final ApiClient Function(String baseUrl) clientBuilder;

  Future<AuthSession?>? _inflight;

  /// 供单测断言：当前是否有刷新在途。
  bool get hasInflightRefresh => _inflight != null;

  @override
  Future<AuthSession?> refresh(String staleAccessToken) {
    final current = readSession();
    if (current == null || !current.canRefresh) {
      return Future<AuthSession?>.value(null);
    }
    if (staleAccessToken.isNotEmpty && current.token != staleAccessToken) {
      // 别的请求已经刷新过：直接复用当前会话，避免刷新风暴
      return Future<AuthSession?>.value(current);
    }
    final inflight = _inflight;
    if (inflight != null) return inflight;

    final future = _performRefresh(current);
    _inflight = future;
    future.whenComplete(() {
      if (identical(_inflight, future)) {
        _inflight = null;
      }
    });
    return future;
  }

  Future<AuthSession?> _performRefresh(AuthSession current) async {
    final client = clientBuilder(baseUrl());
    try {
      final renewed = await client.refreshSession(
        refreshToken: current.refreshToken,
      );
      if (renewed.token.isEmpty) return null;
      await storage.saveSession(renewed);
      onRefreshed(renewed);
      return renewed;
    } on ApiException {
      // refresh 失败（过期/已吊销/账号停用）：不在此处清存储，
      // 由页面的 401 处理统一清会话并回登录页，避免两处并发清理
      return null;
    } catch (_) {
      return null;
    } finally {
      client.close();
    }
  }
}
