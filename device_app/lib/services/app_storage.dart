import 'dart:convert';

import 'package:shared_preferences/shared_preferences.dart';

import '../models/mobile_api_models.dart';

/// 移动看板的轻量本地存储：token / 网关地址 / 选中设备。
///
/// 仅用于演示级持久化，不引入安全存储依赖；README 已注明
/// 生产环境应替换为 Keychain / Keystore。
class AppStorage {
  static const String _sessionKey = 'mobile.session';
  static const String _baseUrlKey = 'mobile.baseUrl';
  static const String _selectedDeviceKey = 'mobile.selectedDevice';

  Future<String?> loadBaseUrl() async {
    final prefs = await SharedPreferences.getInstance();
    final value = prefs.getString(_baseUrlKey);
    return value == null || value.isEmpty ? null : value;
  }

  Future<void> saveBaseUrl(String baseUrl) async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(_baseUrlKey, baseUrl);
  }

  Future<AuthSession?> loadSession() async {
    final prefs = await SharedPreferences.getInstance();
    final raw = prefs.getString(_sessionKey);
    if (raw == null || raw.isEmpty) {
      return null;
    }
    try {
      final json = jsonDecode(raw);
      if (json is! Map<String, dynamic>) {
        return null;
      }
      final session = AuthSession.fromJson(json);
      return session.token.isEmpty ? null : session;
    } catch (_) {
      // 本地数据损坏时按未登录处理，不阻塞进入移动看板
      return null;
    }
  }

  Future<void> saveSession(AuthSession session) async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(_sessionKey, jsonEncode(session.toJson()));
  }

  Future<void> clearSession() async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.remove(_sessionKey);
  }

  Future<String?> loadSelectedDeviceId() async {
    final prefs = await SharedPreferences.getInstance();
    final value = prefs.getString(_selectedDeviceKey);
    return value == null || value.isEmpty ? null : value;
  }

  Future<void> saveSelectedDeviceId(String deviceId) async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(_selectedDeviceKey, deviceId);
  }
}
