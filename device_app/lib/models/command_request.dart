/// 移动看板写操作的指令构造与提交前参数校验。
///
/// 网关 `POST /api/devices/{id}/commands` 只校验 type 枚举
/// （calibrate / ota / reboot / config），payload 为自由字典；
/// 这里只做最小前端校验，避免把明显非法的参数发到设备端。
library;

/// 网关 `CommandCreateRequest.type` 支持的四种指令。
enum DeviceCommandType {
  calibrate('calibrate', '传感器校准', '调整温度 / 湿度读数偏移'),
  config('config', '运行参数配置', '修改上报间隔与休眠时长'),
  reboot('reboot', '设备重启', '立即重启目标设备'),
  ota('ota', '固件升级（单台）', '指定目标版本；批量升级建议用 Web 的 OTA 任务');

  const DeviceCommandType(this.wireName, this.label, this.description);

  /// 发往网关的枚举值，与 `CommandCreateRequest` 的 pattern 一致。
  final String wireName;
  final String label;
  final String description;
}

/// 校验通过的指令请求，可直接交给 [ApiClient.sendCommand]。
class CommandRequest {
  const CommandRequest({
    required this.type,
    required this.payload,
    required this.summary,
  });

  final DeviceCommandType type;

  /// 网关 `payload` 字段，键名与设备模拟器约定一致（camelCase）。
  final Map<String, dynamic> payload;

  /// 二次确认弹窗展示的参数摘要。
  final String summary;
}

/// 前端侧参数校验失败，[message] 可直接展示给用户。
class CommandValidationException implements Exception {
  const CommandValidationException(this.message);

  final String message;

  @override
  String toString() => message;
}

/// 根据表单输入构造指令请求；非法输入抛 [CommandValidationException]。
CommandRequest buildCommandRequest({
  required DeviceCommandType type,
  String temperatureOffset = '',
  String humidityOffset = '',
  String reportIntervalSec = '',
  String sleepSec = '',
  String targetVersion = '',
  String url = '',
  String size = '',
}) {
  switch (type) {
    case DeviceCommandType.calibrate:
      final temperature = _parseDouble(temperatureOffset, '温度偏移', fallback: 0);
      final humidity = _parseDouble(humidityOffset, '湿度偏移', fallback: 0);
      if (temperature < -20 || temperature > 20) {
        throw const CommandValidationException('温度偏移需在 -20 ~ 20 ℃ 之间');
      }
      if (humidity < -50 || humidity > 50) {
        throw const CommandValidationException('湿度偏移需在 -50 ~ 50 %RH 之间');
      }
      return CommandRequest(
        type: type,
        payload: <String, dynamic>{
          'temperatureOffset': temperature,
          'humidityOffset': humidity,
        },
        summary:
            '温度偏移 ${_signed(temperature)}℃ · '
            '湿度偏移 ${_signed(humidity)}%RH',
      );

    case DeviceCommandType.config:
      final reportInterval = _parseInt(
        reportIntervalSec,
        '上报间隔',
        min: 1,
        max: 86400,
      );
      final sleep = _parseInt(sleepSec, '休眠时长', min: 1, max: 86400);
      return CommandRequest(
        type: type,
        payload: <String, dynamic>{
          'reportIntervalSec': reportInterval,
          'sleepSec': sleep,
        },
        summary: '上报间隔 $reportInterval 秒 · 休眠 $sleep 秒',
      );

    case DeviceCommandType.reboot:
      return CommandRequest(
        type: type,
        payload: const <String, dynamic>{},
        summary: '无附加参数，设备将立即重启',
      );

    case DeviceCommandType.ota:
      final version = targetVersion.trim();
      if (version.isEmpty) {
        throw const CommandValidationException('OTA 目标版本不能为空');
      }
      if (version.length > 32) {
        throw const CommandValidationException('OTA 目标版本不能超过 32 个字符');
      }
      final address = url.trim();
      if (address.isNotEmpty &&
          (!address.startsWith('http://') && !address.startsWith('https://'))) {
        throw const CommandValidationException(
          'OTA 地址需以 http:// 或 https:// 开头',
        );
      }
      if (address.length > 512) {
        throw const CommandValidationException('OTA 地址不能超过 512 个字符');
      }
      int? bytes;
      if (size.trim().isNotEmpty) {
        bytes = _parseInt(size, '升级包大小', min: 1, max: 2147483647);
      }
      return CommandRequest(
        type: type,
        payload: <String, dynamic>{
          'targetVersion': version,
          if (address.isNotEmpty) 'url': address,
          'size': ?bytes,
        },
        summary:
            '目标版本 $version'
            '${address.isEmpty ? '' : ' · 地址 $address'}'
            '${bytes == null ? '' : ' · 大小 $bytes 字节'}',
      );
  }
}

double _parseDouble(String raw, String label, {required double fallback}) {
  final text = raw.trim();
  if (text.isEmpty) {
    return fallback;
  }
  final value = double.tryParse(text);
  if (value == null) {
    throw CommandValidationException('$label 需为数字');
  }
  return value;
}

int _parseInt(String raw, String label, {required int min, required int max}) {
  final text = raw.trim();
  if (text.isEmpty) {
    throw CommandValidationException('$label不能为空');
  }
  final value = int.tryParse(text);
  if (value == null) {
    throw CommandValidationException('$label需为整数');
  }
  if (value < min || value > max) {
    throw CommandValidationException('$label需在 $min ~ $max 之间');
  }
  return value;
}

String _signed(double value) {
  final text = value % 1 == 0 ? value.toInt().toString() : value.toString();
  return value > 0 ? '+$text' : text;
}
