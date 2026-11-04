import 'dart:async';

import 'package:flutter/foundation.dart';

import '../models/realtime_payload.dart';
import 'realtime_transport.dart';

/// 实时链路状态，直接映射到界面上的连接指示。
enum RealtimeState { idle, connecting, connected, reconnecting, stopped }

extension RealtimeStateLabel on RealtimeState {
  String get label => switch (this) {
    RealtimeState.idle => '实时未启动',
    RealtimeState.connecting => '实时连接中…',
    RealtimeState.connected => '实时已连接',
    RealtimeState.reconnecting => '实时重连中…',
    RealtimeState.stopped => '实时已断开',
  };

  /// 是否需要 REST 轮询兜底：已连接时不需要，其余状态都视为实时不可用。
  bool get requiresPollingFallback =>
      this == RealtimeState.reconnecting || this == RealtimeState.stopped;
}

/// 移动看板的实时数据服务。
///
/// 职责边界：
/// - 只负责 MQTT over WebSocket 的订阅、解析与断线重连；
/// - 首屏数据仍由 REST 兜底（[ApiClient]），本服务不碰 HTTP；
/// - 重连采用指数退避（3s 起，封顶 30s），成功后计数归零。
class BoardRealtimeService {
  BoardRealtimeService({
    required this.url,
    required RealtimeTransport Function(String url, String clientId)
    transportFactory,
    this.retryBase = const Duration(seconds: 3),
    this.retryMax = const Duration(seconds: 30),
    this.parser = const RealtimeParser(),
  }) : _createTransport = transportFactory;

  /// `ws://<网关主机>:9001`。
  final String url;
  final Duration retryBase;
  final Duration retryMax;

  final RealtimeTransport Function(String url, String clientId)
  _createTransport;

  /// 报文解析器，单测可替换。
  final RealtimeParser parser;

  final ValueNotifier<RealtimeState> state = ValueNotifier<RealtimeState>(
    RealtimeState.idle,
  );

  /// 最近一次失败原因（中文可读），连接成功后清空。
  final ValueNotifier<String?> failure = ValueNotifier<String?>(null);

  /// 已连续重试次数，便于界面展示「第 N 次重连」。
  final ValueNotifier<int> attempts = ValueNotifier<int>(0);

  final StreamController<RealtimeEvent> _events =
      StreamController<RealtimeEvent>.broadcast();

  Stream<RealtimeEvent> get events => _events.stream;

  RealtimeTransport? _transport;
  StreamSubscription<RealtimeMessage>? _messageSub;
  StreamSubscription<void>? _dropSub;
  Timer? _retryTimer;

  bool _running = false;
  int _attempt = 0;

  void start() {
    if (_running) {
      return;
    }
    _running = true;
    _attempt = 0;
    attempts.value = 0;
    unawaited(_connect());
  }

  Future<void> stop() async {
    if (!_running) {
      return;
    }
    _running = false;
    _retryTimer?.cancel();
    _retryTimer = null;
    await _teardown();
    state.value = RealtimeState.stopped;
    if (!_events.isClosed) {
      await _events.close();
    }
  }

  Future<void> _connect() async {
    if (!_running) {
      return;
    }
    state.value = _attempt == 0
        ? RealtimeState.connecting
        : RealtimeState.reconnecting;

    final clientId =
        'iot-mobile-board-$_attempt-${DateTime.now().millisecondsSinceEpoch}';
    final transport = _createTransport(url, clientId);
    _transport = transport;
    _messageSub = transport.messages.listen(_onMessage);
    _dropSub = transport.disconnected.listen((_) => _handleDrop(transport));

    try {
      await transport.connect();
    } catch (error) {
      if (!_running) {
        await _teardown();
        return;
      }
      failure.value = _describe(error);
      await _teardown();
      _scheduleRetry();
      return;
    }

    if (!_running) {
      await _teardown();
      return;
    }
    _attempt = 0;
    attempts.value = 0;
    failure.value = null;
    state.value = RealtimeState.connected;
  }

  void _handleDrop(RealtimeTransport transport) {
    if (!_running || !identical(transport, _transport)) {
      return;
    }
    if (state.value == RealtimeState.reconnecting) {
      return;
    }
    state.value = RealtimeState.reconnecting;
    failure.value = '实时连接已断开，正在重连…';
    unawaited(_teardown().then((_) => _scheduleRetry()));
  }

  void _scheduleRetry() {
    if (!_running) {
      return;
    }
    _attempt += 1;
    attempts.value = _attempt;
    state.value = RealtimeState.reconnecting;
    _retryTimer?.cancel();
    _retryTimer = Timer(_backoff(_attempt), () => unawaited(_connect()));
  }

  /// 指数退避：retryBase × 2^(n-1)，封顶 retryMax。
  Duration _backoff(int attempt) {
    final exponent = (attempt - 1).clamp(0, 10);
    final ms = retryBase.inMilliseconds * (1 << exponent);
    return Duration(milliseconds: ms.clamp(0, retryMax.inMilliseconds));
  }

  void _onMessage(RealtimeMessage message) {
    final event = parser.parse(message.topic, message.payload);
    if (event == null || _events.isClosed) {
      return;
    }
    _events.add(event);
  }

  Future<void> _teardown() async {
    final transport = _transport;
    _transport = null;
    await _dropSub?.cancel();
    _dropSub = null;
    await _messageSub?.cancel();
    _messageSub = null;
    if (transport != null) {
      try {
        await transport.disconnect();
      } catch (_) {
        // 断开本身失败不应阻塞重连
      }
    }
  }

  String _describe(Object error) {
    if (error is RealtimeConnectException) {
      return error.message;
    }
    return '实时连接失败：$error';
  }

  /// 停止实时链路。
  ///
  /// 不主动 dispose Notifier：页面切换网关时会立即换上新实例，
  /// 旧实例的监听器由 Flutter 在下一帧移除，提前 dispose 会触发断言。
  Future<void> dispose() async {
    await stop();
  }
}
