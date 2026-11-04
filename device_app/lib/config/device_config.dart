/// 设备运行配置：Broker 地址、设备身份、省电周期参数。
///
/// 保持不可变对象，修改时使用 [copyWith] 生成新实例，
/// 便于 UI 通过 ValueNotifier 感知配置变化。
class DeviceConfig {
  const DeviceConfig({
    this.brokerHost = '127.0.0.1',
    this.brokerPort = 1883,
    this.deviceId = 'dev-0001',
    this.sleepSec = 15,
    this.commandWindowSec = 8,
    this.reportIntervalSec = 30,
    this.firmwareVersion = '1.0.0',
    this.mqttPassword = '',
  });

  /// MQTT Broker 主机地址。
  final String brokerHost;

  /// MQTT Broker 端口，契约要求设备端口为 1883。
  final int brokerPort;

  /// 设备唯一标识，全链路统一，用于拼接 Topic。
  final String deviceId;

  /// 休眠时长（秒），真实电池设备靠休眠省电。
  final int sleepSec;

  /// 每次上线后打开指令接收窗口的时长（秒）。
  final int commandWindowSec;

  /// 上报间隔（秒），来自云端 config 指令，用于唤醒期间的额外上报。
  final int reportIntervalSec;

  /// 当前固件版本，OTA 成功后更新，并在下一次上线状态报文中携带。
  final String firmwareVersion;

  /// 设备 MQTT 口令。用户名恒等于 [deviceId]（Broker ACL 按 %u 隔离子树）；
  /// 凭证由 `scripts/mqtt_credentials.py` 或设备凭证接口签发，轮换后需在设置页更新。
  final String mqttPassword;

  DeviceConfig copyWith({
    String? brokerHost,
    int? brokerPort,
    String? deviceId,
    int? sleepSec,
    int? commandWindowSec,
    int? reportIntervalSec,
    String? firmwareVersion,
    String? mqttPassword,
  }) {
    return DeviceConfig(
      brokerHost: brokerHost ?? this.brokerHost,
      brokerPort: brokerPort ?? this.brokerPort,
      deviceId: deviceId ?? this.deviceId,
      sleepSec: sleepSec ?? this.sleepSec,
      commandWindowSec: commandWindowSec ?? this.commandWindowSec,
      reportIntervalSec: reportIntervalSec ?? this.reportIntervalSec,
      firmwareVersion: firmwareVersion ?? this.firmwareVersion,
      mqttPassword: mqttPassword ?? this.mqttPassword,
    );
  }
}
