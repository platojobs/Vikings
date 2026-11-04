import 'package:flutter_test/flutter_test.dart';
import 'package:iot_device_app/config/api_config.dart';
import 'package:iot_device_app/models/mobile_api_models.dart';
import 'package:iot_device_app/services/app_storage.dart';
import 'package:shared_preferences/shared_preferences.dart';

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUp(() {
    SharedPreferences.setMockInitialValues(<String, Object>{});
  });

  test('会话与网关地址可持久化并能清除', () async {
    final storage = AppStorage();
    const session = AuthSession(
      token: 'jwt-token',
      username: 'admin',
      role: 'admin',
    );

    await storage.saveBaseUrl('http://192.168.1.8:8000');
    await storage.saveSession(session);

    final loadedBaseUrl = await storage.loadBaseUrl();
    final loadedSession = await storage.loadSession();

    expect(loadedBaseUrl, 'http://192.168.1.8:8000');
    expect(loadedSession?.token, 'jwt-token');
    expect(loadedSession?.username, 'admin');
    expect(loadedSession?.role, 'admin');

    await storage.clearSession();
    expect(await storage.loadSession(), isNull);
  });

  test('本地会话损坏时按未登录处理，不抛异常', () async {
    SharedPreferences.setMockInitialValues(<String, Object>{
      'mobile.session': 'not-json',
    });
    final storage = AppStorage();

    expect(await storage.loadSession(), isNull);
  });

  test('网关地址归一化与校验覆盖常见输入', () {
    expect(ApiConfig.normalize(' 192.168.1.8:8000/ '), 'http://192.168.1.8:8000');
    expect(ApiConfig.normalize('https://iot.example.com/'), 'https://iot.example.com');
    expect(ApiConfig.isValid('192.168.1.8:8000'), isTrue);
    expect(ApiConfig.isValid(''), isFalse);
    expect(ApiConfig.isValid('ftp://example.com'), isFalse);
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
