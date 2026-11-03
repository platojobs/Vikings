import 'dart:math' as math;

import '../models/sensor_payload.dart';

/// 传感器模拟器：模拟真实电池传感器硬件的读数漂移与电量缓慢消耗。
///
/// 与 MQTT 协议无关，只负责生成符合契约范围的传感器数据，
/// 校准偏移量也保存在这里，后续读数自动叠加。
class DeviceSimulator {
  DeviceSimulator({math.Random? random}) : _random = random ?? math.Random();

  final math.Random _random;

  // 初始读数贴近真实室内环境（锂电 3.7V 典型值）
  double _temperature = 24.6;
  double _humidity = 55.2;
  double _voltage = 3.72;
  int _rssi = -63;
  double _battery = 86;

  /// 温度校准偏移（手机端 calibrate 指令下发后生效并持久保存）。
  double temperatureOffset = 0;

  /// 湿度校准偏移。
  double humidityOffset = 0;

  /// 应用云端下发的校准参数。
  void applyCalibration({
    required double temperatureOffset,
    required double humidityOffset,
  }) {
    this.temperatureOffset = temperatureOffset;
    this.humidityOffset = humidityOffset;
  }

  /// 生成下一次传感器读数。
  ///
  /// - 读数在合理范围内随机浮动，模拟真实传感器噪声；
  /// - 电量每次上报缓慢下降，电压随电量同步轻微下滑；
  /// - 所有数值裁剪到契约规定范围。
  SensorPayload nextReading({required String deviceId, required int seq}) {
    _temperature = (_temperature + _random.nextDouble() * 0.6 - 0.3).clamp(
      -10.0,
      70.0,
    );
    _humidity = (_humidity + _random.nextDouble() * 1.2 - 0.6).clamp(
      20.0,
      95.0,
    );
    // RSSI 在 -50 ~ -80 之间随机波动，偶发弱信号
    _rssi = (_rssi + _random.nextInt(7) - 3).clamp(-90, -45);

    // 真实硬件：每次唤醒上报都会消耗一点电量
    _battery = math.max(0, _battery - 0.02 - _random.nextDouble() * 0.02);
    // 电压与电量正相关，叠加采样噪声
    _voltage = (3.0 + _battery * 0.0085 + _random.nextDouble() * 0.04).clamp(
      3.0,
      4.2,
    );

    final temperature = double.parse(
      (_temperature + temperatureOffset).clamp(-40.0, 85.0).toStringAsFixed(2),
    );
    final humidity = double.parse(
      (_humidity + humidityOffset).clamp(0.0, 100.0).toStringAsFixed(2),
    );
    final voltage = double.parse(_voltage.toStringAsFixed(2));

    return SensorPayload(
      deviceId: deviceId,
      temperature: temperature,
      humidity: humidity,
      voltage: voltage,
      rssi: _rssi,
      battery: _battery.round().clamp(0, 100),
      seq: seq,
      ts: DateTime.now().millisecondsSinceEpoch,
    );
  }
}
