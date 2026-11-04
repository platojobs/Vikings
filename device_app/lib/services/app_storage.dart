import 'dart:convert';

import 'package:flutter_secure_storage/flutter_secure_storage.dart';
import 'package:shared_preferences/shared_preferences.dart';

import '../models/mobile_api_models.dart';

/// 安全存储抽象：生产用 Keychain / Keystore，单测注入内存实现。
abstract class SecureStore {
  Future<String?> read(String key);

  Future<void> write(String key, String value);

  Future<void> delete(String key);
}

/// iOS Keychain / Android Keystore 实现（flutter_secure_storage）。
class KeychainSecureStore implements SecureStore {
  const KeychainSecureStore();

  static const FlutterSecureStorage _storage = FlutterSecureStorage();

  @override
  Future<String?> read(String key) => _storage.read(key: key);

  @override
  Future<void> write(String key, String value) =>
      _storage.write(key: key, value: value);

  @override
  Future<void> delete(String key) => _storage.delete(key: key);
}

/// 移动看板的本地存储。
///
/// - 登录会话（access + refresh）用**同一个 key**（`mobile.session`）整包写入
///   系统安全存储：iOS Keychain / Android Keystore。两者同属一份登录凭据、
///   生命周期由同一次轮换原子更新，拆 key 只会增加一次写失败即不一致的风险；
/// - 网关地址与选中设备属于非敏感配置，保留在 shared_preferences；
/// - 兼容旧版本（v1.7.0）把会话明文写在 shared_preferences 的情况：
///   读取时若安全存储为空，则取出旧值迁移过去并立即删除明文。
class AppStorage {
  AppStorage({SecureStore secureStore = const KeychainSecureStore()})
    : _secure = secureStore;

  static const String _sessionKey = 'mobile.session';
  static const String _baseUrlKey = 'mobile.baseUrl';
  static const String _realtimeUrlKey = 'mobile.realtimeUrl';
  static const String _selectedDeviceKey = 'mobile.selectedDevice';

  final SecureStore _secure;

  Future<String?> loadBaseUrl() async {
    final prefs = await SharedPreferences.getInstance();
    final value = prefs.getString(_baseUrlKey);
    return value == null || value.isEmpty ? null : value;
  }

  Future<void> saveBaseUrl(String baseUrl) async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.setString(_baseUrlKey, baseUrl);
  }

  /// 自定义实时地址；空值表示按网关地址自动推导。
  Future<String?> loadRealtimeUrl() async {
    final prefs = await SharedPreferences.getInstance();
    final value = prefs.getString(_realtimeUrlKey);
    return value == null || value.isEmpty ? null : value;
  }

  Future<void> saveRealtimeUrl(String url) async {
    final prefs = await SharedPreferences.getInstance();
    if (url.trim().isEmpty) {
      await prefs.remove(_realtimeUrlKey);
    } else {
      await prefs.setString(_realtimeUrlKey, url.trim());
    }
  }

  Future<AuthSession?> loadSession() async {
    String? raw;
    try {
      raw = await _secure.read(_sessionKey);
    } catch (_) {
      // 安全存储不可用（部分桌面环境缺 libsecret）时退回迁移逻辑
      raw = null;
    }
    if (raw != null && raw.isNotEmpty) {
      // 安全存储已有会话时，顺手清理旧版本可能残留的明文副本
      final prefs = await SharedPreferences.getInstance();
      if (prefs.containsKey(_sessionKey)) {
        await prefs.remove(_sessionKey);
      }
      return _decode(raw);
    }
    return _migrateLegacySession();
  }

  Future<void> saveSession(AuthSession session) async {
    final value = jsonEncode(session.toJson());
    // 先清掉旧版本明文，再写入安全存储，避免同一 token 两份副本
    final prefs = await SharedPreferences.getInstance();
    await prefs.remove(_sessionKey);
    await _secure.write(_sessionKey, value);
  }

  Future<void> clearSession() async {
    final prefs = await SharedPreferences.getInstance();
    await prefs.remove(_sessionKey);
    try {
      await _secure.delete(_sessionKey);
    } catch (_) {
      // 安全存储删除失败不阻塞退出登录流程
    }
  }

  /// 迁移 v1.7.0 的明文会话；无论迁移是否成功都删除旧值。
  Future<AuthSession?> _migrateLegacySession() async {
    final prefs = await SharedPreferences.getInstance();
    final legacy = prefs.getString(_sessionKey);
    if (legacy == null || legacy.isEmpty) {
      return null;
    }
    await prefs.remove(_sessionKey);
    final session = _decode(legacy);
    if (session == null) {
      return null;
    }
    try {
      await _secure.write(_sessionKey, legacy);
    } catch (_) {
      // 迁移失败时本次仍可免登录，下次启动会回到登录页
    }
    return session;
  }

  AuthSession? _decode(String raw) {
    try {
      final json = jsonDecode(raw);
      if (json is! Map<String, dynamic>) {
        return null;
      }
      final session = AuthSession.fromJson(json);
      return session.token.isEmpty ? null : session;
    } catch (_) {
      // 数据损坏时按未登录处理，不阻塞进入移动看板
      return null;
    }
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
