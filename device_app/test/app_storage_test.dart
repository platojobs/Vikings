import 'package:flutter_test/flutter_test.dart';
import 'package:iot_device_app/config/api_config.dart';
import 'package:iot_device_app/models/mobile_api_models.dart';
import 'package:iot_device_app/services/app_storage.dart';
import 'package:shared_preferences/shared_preferences.dart';

/// 内存版安全存储：单测无需真实 Keychain / Keystore。
class _FakeSecureStore implements SecureStore {
  final Map<String, String> values = <String, String>{};

  @override
  Future<String?> read(String key) async => values[key];

  @override
  Future<void> write(String key, String value) async {
    values[key] = value;
  }

  @override
  Future<void> delete(String key) async {
    values.remove(key);
  }
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  late _FakeSecureStore secure;

  setUp(() {
    SharedPreferences.setMockInitialValues(<String, Object>{});
    secure = _FakeSecureStore();
  });

  test('会话写入安全存储，网关地址仍留在 shared_preferences', () async {
    final storage = AppStorage(secureStore: secure);
    const session = AuthSession(
      token: 'jwt-token',
      username: 'admin',
      role: 'admin',
    );

    await storage.saveBaseUrl('http://192.168.1.8:8000');
    await storage.saveSession(session);

    expect(secure.values['mobile.session'], contains('jwt-token'));
    final prefs = await SharedPreferences.getInstance();
    expect(prefs.getString('mobile.session'), isNull);

    final loadedBaseUrl = await storage.loadBaseUrl();
    final loadedSession = await storage.loadSession();

    expect(loadedBaseUrl, 'http://192.168.1.8:8000');
    expect(loadedSession?.token, 'jwt-token');
    expect(loadedSession?.username, 'admin');
    expect(loadedSession?.role, 'admin');

    await storage.clearSession();
    expect(secure.values.containsKey('mobile.session'), isFalse);
    expect(await storage.loadSession(), isNull);
  });

  test('v1.7.0 明文会话可迁移到安全存储并删除旧值', () async {
    SharedPreferences.setMockInitialValues(<String, Object>{
      'mobile.session': '{"token":"legacy-token","username":"operator",'
          '"role":"operator"}',
    });
    final storage = AppStorage(secureStore: secure);

    final session = await storage.loadSession();

    expect(session?.token, 'legacy-token');
    expect(session?.role, 'operator');
    expect(secure.values['mobile.session'], contains('legacy-token'));
    final prefs = await SharedPreferences.getInstance();
    expect(prefs.getString('mobile.session'), isNull);
  });

  test('安全存储已有会话时优先使用，并清理残留明文', () async {
    SharedPreferences.setMockInitialValues(<String, Object>{
      'mobile.session': '{"token":"plain-text","username":"viewer",'
          '"role":"viewer"}',
    });
    secure.values['mobile.session'] =
        '{"token":"secure-token","username":"admin","role":"admin"}';
    final storage = AppStorage(secureStore: secure);

    final session = await storage.loadSession();

    expect(session?.token, 'secure-token');
    final prefs = await SharedPreferences.getInstance();
    expect(prefs.getString('mobile.session'), isNull);
  });

  test('旧版本会话损坏时按未登录处理，不抛异常', () async {
    SharedPreferences.setMockInitialValues(<String, Object>{
      'mobile.session': 'not-json',
    });
    final storage = AppStorage(secureStore: secure);

    expect(await storage.loadSession(), isNull);
    final prefs = await SharedPreferences.getInstance();
    expect(prefs.getString('mobile.session'), isNull);
    expect(secure.values.containsKey('mobile.session'), isFalse);
  });

  test('实时地址可配置并持久化，留空回退自动推导', () async {
    final storage = AppStorage(secureStore: secure);

    await storage.saveRealtimeUrl('ws://192.168.1.8:9001');
    expect(await storage.loadRealtimeUrl(), 'ws://192.168.1.8:9001');

    await storage.saveRealtimeUrl('   ');
    expect(await storage.loadRealtimeUrl(), isNull);

    expect(ApiConfig.isValidRealtimeUrl('ws://192.168.1.8:9001'), isTrue);
    expect(ApiConfig.isValidRealtimeUrl('http://192.168.1.8:8000'), isFalse);
  });

  test('网关地址归一化、校验与实时地址推导', () {
    expect(ApiConfig.normalize(' 192.168.1.8:8000/ '), 'http://192.168.1.8:8000');
    expect(ApiConfig.normalize('https://iot.example.com/'), 'https://iot.example.com');
    expect(ApiConfig.isValid('192.168.1.8:8000'), isTrue);
    expect(ApiConfig.isValid(''), isFalse);
    expect(ApiConfig.isValid('ftp://example.com'), isFalse);

    expect(
      ApiConfig.realtimeUrl('http://192.168.1.8:8000'),
      'ws://192.168.1.8:9001',
    );
    expect(
      ApiConfig.realtimeUrl('https://iot.example.com'),
      'wss://iot.example.com:9001',
    );
    expect(ApiConfig.realtimeUrl(''), '');
  });

  test('告警与指令回执按 snake_case 解析', () {
    final alert = AlertItem.fromJson(<String, dynamic>{
      'alert_id': 'alt-1',
      'device_id': 'dev-0001',
      'level': 'critical',
      'metric': 'temperature',
      'value': 80,
      'threshold': 75,
      'message': '温度过高',
      'ai_generated': true,
      'status': 'open',
      'created_at': '2026-11-04T02:00:00+00:00',
    });
    final command = CommandItem.fromJson(<String, dynamic>{
      'cmd_id': 'cmd-1',
      'type': 'reboot',
      'state': 'success',
      'progress': 0,
      'message': '设备已重启',
      'operator': 'admin',
      'created_at': '2026-11-04T02:00:00+00:00',
    });

    expect(alert.critical, isTrue);
    expect(alert.aiGenerated, isTrue);
    expect(alert.createdAt?.isUtc, isTrue);
    expect(command.type, 'reboot');
    expect(command.state, 'success');
  });
}
