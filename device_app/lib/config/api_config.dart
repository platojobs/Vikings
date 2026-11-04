import 'dart:io';

/// 网关联调配置：移动看板只读复用网关 REST，不直连数据库。
class ApiConfig {
  const ApiConfig({required this.baseUrl});

  /// 网关根地址，形如 `http://192.168.1.8:8000`，不含结尾斜杠。
  final String baseUrl;

  /// 默认地址按运行环境区分：
  /// Android 模拟器用 `10.0.2.2` 访问宿主机，其余默认本机。
  /// 真机必须改成电脑局域网 IP，`127.0.0.1` 在真机上指向手机自身。
  static String get defaultBaseUrl =>
      Platform.isAndroid ? 'http://10.0.2.2:8000' : 'http://127.0.0.1:8000';

  /// 归一化用户输入：补协议、去尾部斜杠、去空格。
  static String normalize(String raw) {
    var value = raw.trim();
    if (value.isEmpty) {
      return '';
    }
    if (!value.startsWith('http://') && !value.startsWith('https://')) {
      value = 'http://$value';
    }
    while (value.endsWith('/')) {
      value = value.substring(0, value.length - 1);
    }
    return value;
  }

  /// 校验是否为可用的 http(s) 地址。
  static bool isValid(String raw) {
    final trimmed = raw.trim();
    if (trimmed.isEmpty) {
      return false;
    }
    // 显式写了其它协议（ftp://、ws:// 等）时不自动补 http，直接判非法
    if (trimmed.contains('://') &&
        !trimmed.startsWith('http://') &&
        !trimmed.startsWith('https://')) {
      return false;
    }
    final uri = Uri.tryParse(normalize(trimmed));
    return uri != null &&
        uri.hasScheme &&
        (uri.scheme == 'http' || uri.scheme == 'https') &&
        uri.host.isNotEmpty;
  }

  String endpoint(String path) => '$baseUrl$path';

  /// 由网关地址推导 MQTT over WebSocket 地址。
  ///
  /// 手机端与浏览器一样无法直连 TCP 1883，实时链路固定走 Broker 的 9001
  /// WebSocket 端口；主机沿用网关地址，便于界面只维护一个网关输入框。
  static String realtimeUrl(String baseUrl, {int port = 9001}) {
    final uri = Uri.tryParse(normalize(baseUrl));
    if (uri == null || uri.host.isEmpty) {
      return '';
    }
    final scheme = uri.scheme == 'https' ? 'wss' : 'ws';
    return '$scheme://${uri.host}:$port';
  }
}
