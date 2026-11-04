import 'dart:async';
import 'dart:convert';

import 'package:mqtt_client/mqtt_client.dart';
import 'package:mqtt_client/mqtt_server_client.dart';

/// 一条原始 MQTT 报文（Topic + UTF-8 文本载荷）。
class RealtimeMessage {
  const RealtimeMessage(this.topic, this.payload);

  final String topic;
  final String payload;
}

/// 实时连接抽象。
///
/// 生产实现是 [MqttWebSocketTransport]（Mosquitto 9001 WebSocket），
/// 单测可注入假实现验证断线重连逻辑，无需真实 Broker。
abstract class RealtimeTransport {
  /// 下行报文流，仅包含订阅到的 Topic。
  Stream<RealtimeMessage> get messages;

  /// 被动断线信号（不含主动 disconnect），用于触发重连。
  Stream<void> get disconnected;

  Future<void> connect();

  Future<void> disconnect();
}

/// 实时连接建立失败（区别于网络层异常，便于 UI 统一文案）。
class RealtimeConnectException implements Exception {
  const RealtimeConnectException(this.message);

  final String message;

  @override
  String toString() => message;
}

/// 移动看板的 MQTT over WebSocket 连接。
///
/// 移动端与浏览器一样无法直连 TCP 1883，必须走 Mosquitto 的 9001
/// WebSocket 端口。这里关闭 mqtt_client 自带重连，由上层统一管理退避重试，
/// 避免包内重连与页面状态双轨。
class MqttWebSocketTransport implements RealtimeTransport {
  MqttWebSocketTransport({
    required this.url,
    required this.clientId,
    this.topics = const <String>[
      'device/system/alert',
      'device/+/sensor/report',
      'device/+/status',
    ],
  });

  /// 形如 `ws://192.168.1.8:9001`。
  final String url;
  final String clientId;
  final List<String> topics;

  final StreamController<RealtimeMessage> _messages =
      StreamController<RealtimeMessage>.broadcast();
  final StreamController<void> _disconnected = StreamController<void>.broadcast();

  MqttServerClient? _client;
  StreamSubscription<List<MqttReceivedMessage<MqttMessage>>>? _updatesSub;
  bool _closing = false;

  @override
  Stream<RealtimeMessage> get messages => _messages.stream;

  @override
  Stream<void> get disconnected => _disconnected.stream;

  @override
  Future<void> connect() async {
    final uri = Uri.parse(url);
    if (uri.host.isEmpty) {
      throw RealtimeConnectException('实时地址无效：$url');
    }
    final client = MqttServerClient.withPort(
      uri.host,
      clientId,
      uri.hasPort ? uri.port : 9001,
    );
    client.useWebSocket = true;
    client.logging(on: false);
    client.keepAlivePeriod = 30;
    client.connectTimeoutPeriod = 8000;
    client.autoReconnect = false;
    client.onDisconnected = _notifyDisconnected;

    try {
      await client.connect();
    } catch (error) {
      _safeDisconnect(client);
      throw RealtimeConnectException('实时连接失败：$error');
    }
    if (client.connectionStatus?.state != MqttConnectionState.connected) {
      _safeDisconnect(client);
      throw RealtimeConnectException(
        '实时连接未就绪：${client.connectionStatus?.state}',
      );
    }

    _client = client;
    _updatesSub = client.updates?.listen(_onUpdates);
    for (final topic in topics) {
      try {
        client.subscribe(topic, MqttQos.atLeastOnce);
      } catch (_) {
        // 单个 Topic 订阅失败不影响其它通道；重连后会重新订阅
      }
    }
  }

  @override
  Future<void> disconnect() async {
    _closing = true;
    await _updatesSub?.cancel();
    _updatesSub = null;
    final client = _client;
    _client = null;
    _safeDisconnect(client);
    await _messages.close();
    await _disconnected.close();
  }

  void _onUpdates(List<MqttReceivedMessage<MqttMessage>> events) {
    for (final event in events) {
      final payload = event.payload;
      if (payload is! MqttPublishMessage) {
        continue;
      }
      final text = utf8.decode(
        payload.payload.message,
        allowMalformed: true,
      );
      if (!_messages.isClosed) {
        _messages.add(RealtimeMessage(event.topic, text));
      }
    }
  }

  void _notifyDisconnected() {
    if (_closing || _disconnected.isClosed) {
      return;
    }
    _disconnected.add(null);
  }

  void _safeDisconnect(MqttServerClient? client) {
    if (client == null) {
      return;
    }
    try {
      client.disconnect();
    } catch (_) {
      // 已断开时忽略，避免重连流程被异常打断
    }
  }
}
