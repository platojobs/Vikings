import 'package:flutter_test/flutter_test.dart';
import 'package:iot_device_app/models/command_request.dart';

void main() {
  group('buildCommandRequest 参数校验', () {
    test('config 空值与非整数分别报错', () {
      expect(
        () => buildCommandRequest(type: DeviceCommandType.config),
        throwsA(
          isA<CommandValidationException>().having(
            (error) => error.message,
            'message',
            '上报间隔不能为空',
          ),
        ),
      );
      expect(
        () => buildCommandRequest(
          type: DeviceCommandType.config,
          reportIntervalSec: 'abc',
        ),
        throwsA(
          isA<CommandValidationException>().having(
            (error) => error.message,
            'message',
            '上报间隔需为整数',
          ),
        ),
      );
    });

    test('calibrate 越界与非数字被拦截，合法输入生成摘要', () {
      expect(
        () => buildCommandRequest(
          type: DeviceCommandType.calibrate,
          temperatureOffset: '30',
        ),
        throwsA(
          isA<CommandValidationException>().having(
            (error) => error.message,
            'message',
            '温度偏移需在 -20 ~ 20 ℃ 之间',
          ),
        ),
      );
      expect(
        () => buildCommandRequest(
          type: DeviceCommandType.calibrate,
          humidityOffset: 'x',
        ),
        throwsA(isA<CommandValidationException>()),
      );

      final request = buildCommandRequest(
        type: DeviceCommandType.calibrate,
        temperatureOffset: '2.5',
        humidityOffset: '-1',
      );
      expect(request.payload['temperatureOffset'], 2.5);
      expect(request.payload['humidityOffset'], -1.0);
      expect(request.summary, contains('温度偏移 +2.5℃'));
    });

    test('ota 缺目标版本报错，合法参数只保留非空字段', () {
      expect(
        () => buildCommandRequest(type: DeviceCommandType.ota),
        throwsA(
          isA<CommandValidationException>().having(
            (error) => error.message,
            'message',
            'OTA 目标版本不能为空',
          ),
        ),
      );
      expect(
        () => buildCommandRequest(
          type: DeviceCommandType.ota,
          targetVersion: '1.1.0',
          url: 'ftp://example.com/fw.bin',
        ),
        throwsA(isA<CommandValidationException>()),
      );

      final request = buildCommandRequest(
        type: DeviceCommandType.ota,
        targetVersion: '1.1.0',
        url: 'https://example.com/fw.bin',
        size: '1024',
      );
      expect(request.payload, <String, dynamic>{
        'targetVersion': '1.1.0',
        'url': 'https://example.com/fw.bin',
        'size': 1024,
      });
    });

    test('reboot 无参数，payload 为空对象', () {
      final request = buildCommandRequest(type: DeviceCommandType.reboot);
      expect(request.payload, isEmpty);
      expect(request.summary, contains('立即重启'));
    });
  });
}
