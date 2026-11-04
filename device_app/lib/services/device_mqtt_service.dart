import 'dart:async';
import 'dart:convert';
import 'dart:math' as math;

import 'package:flutter/foundation.dart';
import 'package:mqtt_client/mqtt_client.dart';
import 'package:mqtt_client/mqtt_server_client.dart';

import '../config/device_config.dart';
import '../models/sensor_payload.dart';
import 'device_simulator.dart';

/// 设备运行阶段，UI 直接展示对应中文标签。
enum DevicePhase { idle, sleeping, waking, connected, reporting, ota }

extension DevicePhaseLabel on DevicePhase {
  String get label => switch (this) {
    DevicePhase.idle => '已停止',
    DevicePhase.sleeping => '休眠中',
    DevicePhase.waking => '唤醒中',
    DevicePhase.connected => '已连接',
    DevicePhase.reporting => '上报中',
    DevicePhase.ota => 'OTA 升级中',
  };

  /// 倒计时文案：休眠时是剩余休眠时间，唤醒窗口内是剩余窗口时间。
  String countdownLabel(int seconds) => switch (this) {
    DevicePhase.sleeping => '距离下次唤醒 ${seconds}s',
    DevicePhase.waking => '正在连接 Broker…',
    DevicePhase.connected || DevicePhase.reporting => '指令窗口剩余 ${seconds}s',
    DevicePhase.ota => 'OTA 中保持唤醒（不回休眠）',
    DevicePhase.idle => '模拟循环未启动',
  };
}

/// 日志级别，用于给事件日志着色。
enum DeviceLogLevel { info, success, warning, error }

/// 一条设备事件日志。
class DeviceLogEntry {
  DeviceLogEntry(this.time, this.message, this.level);

  final DateTime time;
  final String message;
  final DeviceLogLevel level;
}

/// 设备端 MQTT 服务：负责协议收发与「间歇上线 + 休眠省电」状态机。
///
/// 状态机完整流程：
/// ```
/// 唤醒 → 连接 Broker → 发布 retained online → 上报一次数据 →
/// 订阅 cmd 并打开指令接收窗口 → 发布 offline(sleep, retained) →
/// 断开连接 → 休眠 N 秒 → 再次唤醒
/// ```
/// OTA 期间会持续顺延指令窗口，保证升级过程不掉线（真实硬件同理）。
class DeviceMqttService {
  DeviceMqttService({DeviceConfig config = const DeviceConfig()})
    : _config = config,
      configNotifier = ValueNotifier<DeviceConfig>(config),
      firmwareVersion = ValueNotifier<String>(config.firmwareVersion);

  /// 单页应用、单设备实例：Service 与页面同生命周期。
  final DeviceSimulator _simulator = DeviceSimulator();
  final math.Random _random = math.Random();

  DeviceConfig _config;

  /// 当前生效配置（云端 config 指令也会更新它）。
  final ValueNotifier<DeviceConfig> configNotifier;

  final ValueNotifier<String> firmwareVersion;

  /// 本地自增上报序号，重启指令会将其归零。
  final ValueNotifier<int> seq = ValueNotifier<int>(0);

  final ValueNotifier<DevicePhase> phase = ValueNotifier<DevicePhase>(
    DevicePhase.idle,
  );

  /// 倒计时秒数，供 UI 展示休眠/指令窗口剩余时间。
  final ValueNotifier<int> countdown = ValueNotifier<int>(0);
  final ValueNotifier<bool> connected = ValueNotifier<bool>(false);
  final ValueNotifier<bool> running = ValueNotifier<bool>(false);
  final ValueNotifier<SensorPayload?> lastReport =
      ValueNotifier<SensorPayload?>(null);

  /// OTA 进度：-1 表示当前没有升级任务。
  final ValueNotifier<double> otaProgress = ValueNotifier<double>(-1);
  final ValueNotifier<List<DeviceLogEntry>> logs =
      ValueNotifier<List<DeviceLogEntry>>(<DeviceLogEntry>[]);

  MqttServerClient? _client;
  StreamSubscription<List<MqttReceivedMessage<MqttMessage>>>? _updatesSub;

  bool _loopRunning = false;
  bool _wakeRequested = false;
  bool _restartImmediately = false;
  bool _manualDisconnect = false;
  bool _otaActive = false;

  /// 循环代际：配置变更/停止时递增，旧循环据此安静退出，避免两个循环同时操作连接。
  int _generation = 0;

  DateTime _windowDeadline = DateTime.now();

  final List<DeviceCommand> _commandQueue = <DeviceCommand>[];
  bool _processingCommands = false;

  // ---------------- Topic 构造（与契约一一对应） ----------------

  String get statusTopic => 'device/${_config.deviceId}/status';
  String get reportTopic => 'device/${_config.deviceId}/sensor/report';
  String get cmdTopic => 'device/${_config.deviceId}/cmd';
  String get ackTopic => 'device/${_config.deviceId}/cmd/ack';

  // ---------------- 对外控制 ----------------

  /// 启动模拟循环。
  void start() {
    if (_loopRunning) {
      return;
    }
    _loopRunning = true;
    running.value = true;
    _wakeRequested = false;
    _restartImmediately = false;
    _log(
      '启动模拟循环（休眠 ${_config.sleepSec}s / 指令窗口 ${_config.commandWindowSec}s）',
    );
    unawaited(_runLoop(_generation));
  }

  /// 停止模拟循环：主动发布 offline(sleep) 后断开，避免后台误判为异常掉线。
  Future<void> stop() async {
    if (!_loopRunning && _client == null) {
      running.value = false;
      phase.value = DevicePhase.idle;
      countdown.value = 0;
      return;
    }
    _loopRunning = false;
    _generation++;
    _wakeRequested = false;

    final client = _client;
    _client = null;
    await _updatesSub?.cancel();
    _updatesSub = null;

    if (client != null) {
      _manualDisconnect = true;
      if (client.connectionStatus?.state == MqttConnectionState.connected) {
        _publishWithClient(
          client,
          statusTopic,
          _offlinePayload('sleep').toJson(),
          retain: true,
        );
      }
      try {
        client.disconnect();
      } catch (error) {
        _log('断开连接异常：$error', DeviceLogLevel.error);
      }
      _manualDisconnect = false;
    }

    connected.value = false;
    running.value = false;
    phase.value = DevicePhase.idle;
    countdown.value = 0;
    otaProgress.value = -1;
    _log('模拟循环已停止', DeviceLogLevel.warning);
  }

  /// 立即唤醒一次：跳过剩余休眠倒计时。
  void wakeNow() {
    if (!_loopRunning) {
      _log('模拟循环未启动，已自动启动', DeviceLogLevel.warning);
      start();
      return;
    }
    if (phase.value != DevicePhase.sleeping) {
      _log('设备已处于唤醒状态，无需重复唤醒');
      return;
    }
    _wakeRequested = true;
    countdown.value = 0;
    _log('收到立即唤醒请求：跳过剩余休眠', DeviceLogLevel.success);
  }

  /// 手动上报一次（未连接时先唤醒再上报）。
  void reportNow() {
    if (!connected.value) {
      _log('手动上报失败：当前未连接 Broker，已触发一次唤醒', DeviceLogLevel.warning);
      wakeNow();
      return;
    }
    unawaited(_reportOnce());
  }

  /// 更新 Broker 地址、设备 ID 与周期参数。
  ///
  /// 设备 ID / Broker 变更会重建连接，其他参数在下个周期生效。
  Future<void> applySettings({
    String? brokerHost,
    int? brokerPort,
    String? deviceId,
    int? sleepSec,
    int? commandWindowSec,
    String? mqttPassword,
  }) async {
    final host = (brokerHost ?? _config.brokerHost).trim();
    final id = (deviceId ?? _config.deviceId).trim();
    if (host.isEmpty || id.isEmpty) {
      throw ArgumentError('Broker 地址与设备 ID 不能为空');
    }
    final identityChanged =
        host != _config.brokerHost ||
        (brokerPort ?? _config.brokerPort) != _config.brokerPort ||
        id != _config.deviceId ||
        (mqttPassword != null && mqttPassword != _config.mqttPassword);
    final wasRunning = _loopRunning;

    // 连接参数变更时先用旧配置正常下线，再切换身份，避免把 offline 发到新 Topic
    if (identityChanged && wasRunning) {
      _log('连接参数变更，重启模拟循环以重新建立连接', DeviceLogLevel.warning);
      await stop();
    }

    _config = _config.copyWith(
      brokerHost: host,
      brokerPort: brokerPort ?? _config.brokerPort,
      deviceId: id,
      sleepSec: sleepSec != null && sleepSec > 0
          ? sleepSec
          : _config.sleepSec,
      commandWindowSec: commandWindowSec != null && commandWindowSec > 0
          ? commandWindowSec
          : _config.commandWindowSec,
      mqttPassword: mqttPassword ?? _config.mqttPassword,
    );
    configNotifier.value = _config;
    _log(
      '配置更新：Broker ${_config.brokerHost}:${_config.brokerPort}，设备 ${_config.deviceId}，'
      '休眠 ${_config.sleepSec}s，窗口 ${_config.commandWindowSec}s',
    );

    if (identityChanged && wasRunning) {
      start();
    }
  }

  /// 页面销毁时停止循环并断开连接（LWT 负责兜底异常退出场景）。
  void dispose() {
    _loopRunning = false;
    _generation++;
    _wakeRequested = false;

    final client = _client;
    _client = null;
    _manualDisconnect = true;
    if (client != null) {
      if (client.connectionStatus?.state == MqttConnectionState.connected) {
        _publishWithClient(
          client,
          statusTopic,
          _offlinePayload('sleep').toJson(),
          retain: true,
        );
      }
      try {
        client.disconnect();
      } catch (_) {
        // 页面销毁阶段忽略断开异常，LWT 仍可在异常掉线时兜底
      }
    }
    connected.value = false;
    // 说明：Notifier 随 Service 实例被回收，此处不显式 dispose，
    // 避免异步循环退出过程中写入已释放的 Notifier。
  }

  // ---------------- 省电状态机主循环 ----------------

  Future<void> _runLoop(int generation) async {
    while (_loopRunning && generation == _generation) {
      try {
        await _wakeCycle(generation);
      } catch (error, stack) {
        _log('设备循环异常：$error', DeviceLogLevel.error);
        debugPrint('$stack');
      }
      if (!_loopRunning || generation != _generation) {
        break;
      }
      if (_restartImmediately) {
        // 重启指令：不进入休眠，立即重新上线（seq 已归零）
        _restartImmediately = false;
        continue;
      }
      await _sleepPhase(generation);
    }

    if (generation == _generation) {
      running.value = false;
      phase.value = DevicePhase.idle;
      countdown.value = 0;
    }
  }

  Future<void> _sleepPhase(int generation) async {
    phase.value = DevicePhase.sleeping;
    _log(
      '进入休眠 ${_config.sleepSec}s（断开连接，降低电池消耗）',
      DeviceLogLevel.info,
    );
    final deadline = DateTime.now().add(Duration(seconds: _config.sleepSec));
    while (_loopRunning && generation == _generation) {
      final remainMs = deadline.difference(DateTime.now()).inMilliseconds;
      if (remainMs <= 0 || _wakeRequested) {
        break;
      }
      countdown.value = (remainMs + 999) ~/ 1000;
      await Future.delayed(const Duration(milliseconds: 200));
    }
    _wakeRequested = false;
    countdown.value = 0;
  }

  /// 一次完整的唤醒周期。
  Future<void> _wakeCycle(int generation) async {
    phase.value = DevicePhase.waking;
    _log(
      '唤醒 → 连接 Broker ${_config.brokerHost}:${_config.brokerPort}',
      DeviceLogLevel.info,
    );

    final client = await _connectWithRetry(generation);
    if (client == null) {
      _log('本轮连接失败，直接进入休眠，下一周期再试', DeviceLogLevel.error);
      return;
    }
    if (!_loopRunning || generation != _generation) {
      _disconnectSilently(client);
      return;
    }

    _client = client;
    connected.value = true;
    _updatesSub = client.updates?.listen(_onMqttEvents);
    client.onSubscribed = (String topic) {
      _log('已订阅指令 Topic：$topic', DeviceLogLevel.success);
    };

    // 1. retained 在线状态：网关/后台据此实时判定上线
    _publishStatus('online', 'boot', retain: true);

    // 2. 上报一次传感器数据
    await _reportOnce();

    // 3. 打开指令接收窗口：先订阅 cmd
    try {
      client.subscribe(cmdTopic, MqttQos.atLeastOnce);
    } catch (error) {
      _log('订阅指令 Topic 失败：$error', DeviceLogLevel.error);
    }
    phase.value = DevicePhase.connected;
    _log('进入指令接收窗口 ${_config.commandWindowSec}s', DeviceLogLevel.info);
    await _commandWindow(generation);

    if (generation != _generation) {
      _disconnectSilently(client);
      return;
    }

    // 4. OTA 未结束时保持唤醒，不能睡
    await _drainPendingWork(generation);

    if (generation != _generation) {
      _disconnectSilently(client);
      return;
    }

    if (!_restartImmediately) {
      // 5. 主动休眠：发布 retained offline，后台不会出现「假在线」
      _publishStatus('offline', 'sleep', retain: true);
      _log('发布 offline(sleep, retained)，本轮下线', DeviceLogLevel.info);
    }

    await _cleanupClient(client);
    if (generation == _generation) {
      phase.value = DevicePhase.sleeping;
      _log('已断开连接', DeviceLogLevel.info);
    }
  }

  /// 指令窗口倒计时；OTA 进行中持续顺延，模拟真实设备升级期间不休眠。
  Future<void> _commandWindow(int generation) async {
    _windowDeadline = DateTime.now().add(
      Duration(seconds: _config.commandWindowSec),
    );
    var lastReportAt = DateTime.now();

    while (_loopRunning && generation == _generation) {
      if (!connected.value || _restartImmediately) {
        break;
      }
      final now = DateTime.now();
      if (_otaActive) {
        _windowDeadline = now.add(Duration(seconds: _config.commandWindowSec));
      }
      final remainMs = _windowDeadline.difference(now).inMilliseconds;
      if (remainMs <= 0) {
        break;
      }
      countdown.value = (remainMs + 999) ~/ 1000;

      // config 指令修改 reportIntervalSec 后，唤醒窗口内按新间隔补报
      if (!_otaActive &&
          _config.reportIntervalSec > 0 &&
          now.difference(lastReportAt).inSeconds >= _config.reportIntervalSec) {
        lastReportAt = now;
        await _reportOnce();
      }
      await Future.delayed(const Duration(milliseconds: 200));
    }
    countdown.value = 0;
  }

  Future<void> _drainPendingWork(int generation) async {
    while (_loopRunning &&
        generation == _generation &&
        (_otaActive || _processingCommands)) {
      countdown.value = _config.commandWindowSec;
      await Future.delayed(const Duration(milliseconds: 200));
    }
  }

  // ---------------- MQTT 连接 ----------------

  MqttServerClient _createClient() {
    final clientId =
        'iot-device-${_config.deviceId}-${_random.nextInt(1 << 20)}';
    final client = MqttServerClient.withPort(
      _config.brokerHost,
      clientId,
      _config.brokerPort,
    );
    client.logging(on: false);
    client.keepAlivePeriod = 30;
    client.connectTimeoutPeriod = 5000;
    // 手工管理连接：休眠时必须彻底断开，不能用自动重连
    client.autoReconnect = false;
    client.onDisconnected = _handleUnexpectedDisconnect;

    // LWT：App 被杀死/断网时 Broker 代发 offline(lwt)，后台不会「假在线」
    client.connectionMessage = MqttConnectMessage()
        .withClientIdentifier(clientId)
        .withWillTopic(statusTopic)
        .withWillMessage(jsonEncode(_offlinePayload('lwt').toJson()))
        .withWillQos(MqttQos.atLeastOnce)
        .withWillRetain()
        .startClean();
    return client;
  }

  Future<MqttServerClient?> _connectWithRetry(int generation) async {
    const maxAttempts = 3;
    for (var attempt = 1; attempt <= maxAttempts; attempt++) {
      if (!_loopRunning || generation != _generation) {
        return null;
      }
      MqttServerClient? client;
      try {
        client = _createClient();
        // Broker 已关闭匿名：用户名 = deviceId，ACL 按 %u 绑定自己的子树
        await client.connect(_config.deviceId, _config.mqttPassword);
        if (client.connectionStatus?.state == MqttConnectionState.connected) {
          _log('MQTT 连接成功（第 $attempt 次尝试）', DeviceLogLevel.success);
          return client;
        }
        _log(
          'MQTT 连接未就绪，当前状态：${client.connectionStatus?.state}',
          DeviceLogLevel.warning,
        );
      } catch (error) {
        _log(
          'MQTT 连接失败（第 $attempt/$maxAttempts 次）：$error',
          DeviceLogLevel.warning,
        );
      }

      if (client != null) {
        _disconnectSilently(client);
      }
      if (attempt < maxAttempts) {
        _log('${attempt * 2}s 后重试连接…', DeviceLogLevel.warning);
        await Future.delayed(Duration(seconds: attempt * 2));
      }
    }
    return null;
  }

  void _handleUnexpectedDisconnect() {
    connected.value = false;
    if (_manualDisconnect || !_loopRunning) {
      return;
    }
    _log('MQTT 连接意外断开，将在下一唤醒周期自动重连', DeviceLogLevel.error);
  }

  void _disconnectSilently(MqttServerClient client) {
    _manualDisconnect = true;
    try {
      client.disconnect();
    } catch (_) {
      // 已断开时忽略异常
    }
    _manualDisconnect = false;
  }

  Future<void> _cleanupClient(MqttServerClient client) async {
    await _updatesSub?.cancel();
    _updatesSub = null;
    _commandQueue.clear();
    _manualDisconnect = true;
    connected.value = false;
    try {
      client.disconnect();
    } catch (_) {
      // 已断开时忽略异常
    }
    if (identical(_client, client)) {
      _client = null;
    }
    _manualDisconnect = false;
  }

  // ---------------- 上报 ----------------

  Future<void> _reportOnce() async {
    if (!connected.value) {
      _log('上报失败：MQTT 未连接', DeviceLogLevel.warning);
      return;
    }
    final currentSeq = seq.value;
    final reading = _simulator.nextReading(
      deviceId: _config.deviceId,
      seq: currentSeq,
    );

    if (!_otaActive) {
      phase.value = DevicePhase.reporting;
    }
    _publishRaw(reportTopic, reading.toJson());
    seq.value = currentSeq + 1;
    lastReport.value = reading;
    _log(
      '上报 seq=$currentSeq 温度 ${reading.temperature}℃ 湿度 ${reading.humidity}%RH '
      '电压 ${reading.voltage}V RSSI ${reading.rssi}dBm 电量 ${reading.battery}%',
      DeviceLogLevel.success,
    );

    await Future.delayed(const Duration(milliseconds: 600));
    if (!_otaActive && phase.value == DevicePhase.reporting) {
      phase.value = DevicePhase.connected;
    }
  }

  void _publishStatus(String status, String reason, {bool retain = false}) {
    final payload = DeviceStatusPayload(
      deviceId: _config.deviceId,
      status: status,
      firmwareVersion: _config.firmwareVersion,
      reason: reason,
      ts: DateTime.now().millisecondsSinceEpoch,
    );
    _publishRaw(statusTopic, payload.toJson(), retain: retain);
    _log(
      '发布状态 $status($reason)${retain ? ' [retained]' : ''}，固件 ${_config.firmwareVersion}',
      status == 'online'
          ? DeviceLogLevel.success
          : DeviceLogLevel.info,
    );
  }

  void _publishRaw(
    String topic,
    Map<String, dynamic> payload, {
    bool retain = false,
  }) {
    final client = _client;
    if (client == null || !connected.value) {
      _log('发布失败（MQTT 未连接）：$topic', DeviceLogLevel.warning);
      return;
    }
    _publishWithClient(client, topic, payload, retain: retain);
  }

  void _publishWithClient(
    MqttServerClient client,
    String topic,
    Map<String, dynamic> payload, {
    bool retain = false,
  }) {
    try {
      final builder = MqttClientPayloadBuilder();
      // jsonEncode 后必须按 UTF-8 写入，避免中文/特殊字符被按 UTF-16 错误编码
      builder.addUTF8String(jsonEncode(payload));
      client.publishMessage(
        topic,
        MqttQos.atLeastOnce,
        builder.payload!,
        retain: retain,
      );
    } catch (error) {
      _log('发布异常 $topic：$error', DeviceLogLevel.error);
    }
  }

  // ---------------- 指令接收与处理 ----------------

  void _onMqttEvents(List<MqttReceivedMessage<MqttMessage>> events) {
    for (final event in events) {
      if (event.topic != cmdTopic) {
        continue;
      }
      final message = event.payload;
      if (message is! MqttPublishMessage) {
        continue;
      }
      final text = MqttPublishPayload.bytesToStringAsString(
        message.payload.message,
      );
      try {
        final decoded = jsonDecode(text);
        if (decoded is! Map<String, dynamic>) {
          _log('指令报文不是 JSON 对象，已丢弃', DeviceLogLevel.warning);
          continue;
        }
        final command = DeviceCommand.fromJson(decoded);
        if (command.cmdId.isEmpty || command.type.isEmpty) {
          _log('指令缺少 cmdId/type，已丢弃：$text', DeviceLogLevel.warning);
          continue;
        }
        _log(
          '收到指令 ${command.cmdId} type=${command.type}'
          '${command.operator.isEmpty ? '' : ' operator=${command.operator}'}',
          DeviceLogLevel.info,
        );
        _commandQueue.add(command);
        unawaited(_pumpCommands());
      } catch (error) {
        _log('指令解析失败：$error', DeviceLogLevel.error);
      }
    }
  }

  Future<void> _pumpCommands() async {
    if (_processingCommands) {
      return;
    }
    _processingCommands = true;
    try {
      while (_commandQueue.isNotEmpty &&
          _loopRunning &&
          connected.value) {
        final command = _commandQueue.removeAt(0);
        try {
          await _handleCommand(command);
        } catch (error) {
          _log('指令处理异常 ${command.type}：$error', DeviceLogLevel.error);
          _sendAck(
            command,
            state: 'failed',
            progress: 0,
            message: '设备处理异常：$error',
          );
        }
      }
    } finally {
      _processingCommands = false;
    }
  }

  Future<void> _handleCommand(DeviceCommand command) async {
    _sendAck(
      command,
      state: 'received',
      progress: 0,
      message: '已接收指令，开始处理',
    );
    switch (command.type) {
      case 'calibrate':
        final temperatureOffset = _toDouble(
          command.payload['temperatureOffset'],
        );
        final humidityOffset = _toDouble(command.payload['humidityOffset']);
        _simulator.applyCalibration(
          temperatureOffset: temperatureOffset,
          humidityOffset: humidityOffset,
        );
        _log(
          '校准参数已保存：温度偏移 $temperatureOffset℃，湿度偏移 $humidityOffset%RH，'
          '后续读数自动应用',
          DeviceLogLevel.success,
        );
        _sendAck(
          command,
          state: 'success',
          progress: 0,
          message: '校准参数已生效',
        );
        break;
      case 'ota':
        await _handleOta(command);
        break;
      case 'reboot':
        await _handleReboot(command);
        break;
      case 'config':
        _handleConfig(command);
        break;
      default:
        _log(
          '收到不支持的指令类型：${command.type}',
          DeviceLogLevel.warning,
        );
        _sendAck(
          command,
          state: 'failed',
          progress: 0,
          message: '不支持的指令类型：${command.type}',
        );
    }
  }

  /// 模拟 OTA：进度 10/30/55/80 为 progress，100 时 success。
  Future<void> _handleOta(DeviceCommand command) async {
    final targetVersion =
        command.payload['targetVersion']?.toString().trim() ?? '';
    final url = command.payload['url']?.toString() ?? '';
    final size = _toIntOrNull(command.payload['size']);

    if (targetVersion.isEmpty) {
      _sendAck(
        command,
        state: 'failed',
        progress: 0,
        message: 'OTA 指令缺少 targetVersion',
      );
      return;
    }
    if (_otaActive) {
      _sendAck(
        command,
        state: 'failed',
        progress: 0,
        message: '已有 OTA 任务进行中，请稍后重试',
      );
      return;
    }

    _otaActive = true;
    otaProgress.value = 0;
    phase.value = DevicePhase.ota;
    _log(
      'OTA 开始：目标版本 $targetVersion，地址 $url'
      '${size == null ? '' : '，大小 $size 字节'}',
      DeviceLogLevel.info,
    );

    const steps = <int>[10, 30, 55, 80, 100];
    for (final step in steps) {
      await Future.delayed(const Duration(milliseconds: 1000));
      if (!_loopRunning || !connected.value) {
        _log('OTA 中断：设备连接已断开，升级未完成', DeviceLogLevel.error);
        _otaActive = false;
        otaProgress.value = -1;
        return;
      }
      otaProgress.value = step.toDouble();

      if (step == 100) {
        // 升级成功：固件版本更新，下一次上线状态报文会带上新版本
        _config = _config.copyWith(firmwareVersion: targetVersion);
        configNotifier.value = _config;
        firmwareVersion.value = targetVersion;
        _sendAck(
          command,
          state: 'success',
          progress: 100,
          message: '升级完成，当前固件版本 $targetVersion',
        );
        _log(
          'OTA 完成：固件版本已更新为 $targetVersion（下次上线状态报文携带新版本）',
          DeviceLogLevel.success,
        );
      } else {
        _sendAck(
          command,
          state: 'progress',
          progress: step,
          message: _otaStepMessage(step),
        );
        _log('OTA 进度 $step%：${_otaStepMessage(step)}');
      }
    }

    _otaActive = false;
    phase.value = DevicePhase.connected;
    // 升级结束后再留一个窗口，方便网关收到成功回执
    _windowDeadline = DateTime.now().add(
      Duration(seconds: _config.commandWindowSec),
    );
  }

  String _otaStepMessage(int step) => switch (step) {
    10 => '开始下载固件',
    30 => '固件下载中',
    55 => '下载完成，校验固件',
    80 => '安装固件中',
    100 => '升级完成',
    _ => '升级中',
  };

  Future<void> _handleReboot(DeviceCommand command) async {
    _log('执行重启：重置上报序号 seq 并重新上线', DeviceLogLevel.warning);
    seq.value = 0;
    _sendAck(
      command,
      state: 'success',
      progress: 0,
      message: '设备已重启，seq 已重置',
    );
    _otaActive = false;
    _otaProgressReset();
    _restartImmediately = true;
    _commandQueue.clear();
  }

  void _handleConfig(DeviceCommand command) {
    final reportIntervalSec = _toIntOrNull(
      command.payload['reportIntervalSec'],
    );
    final sleepSec = _toIntOrNull(command.payload['sleepSec']);
    if ((reportIntervalSec == null || reportIntervalSec <= 0) &&
        (sleepSec == null || sleepSec <= 0)) {
      _sendAck(
        command,
        state: 'failed',
        progress: 0,
        message: 'config 指令缺少有效的 reportIntervalSec / sleepSec',
      );
      return;
    }
    _config = _config.copyWith(
      reportIntervalSec: reportIntervalSec != null && reportIntervalSec > 0
          ? reportIntervalSec
          : _config.reportIntervalSec,
      sleepSec: sleepSec != null && sleepSec > 0
          ? sleepSec
          : _config.sleepSec,
    );
    configNotifier.value = _config;
    _log(
      '动态配置已生效：上报间隔 ${_config.reportIntervalSec}s，休眠 ${_config.sleepSec}s',
      DeviceLogLevel.success,
    );
    _sendAck(
      command,
      state: 'success',
      progress: 0,
      message:
          '配置已更新：上报间隔 ${_config.reportIntervalSec}s，休眠 ${_config.sleepSec}s',
    );
  }

  void _sendAck(
    DeviceCommand command, {
    required String state,
    required int progress,
    required String message,
  }) {
    final ack = CommandAckPayload(
      cmdId: command.cmdId,
      type: command.type,
      state: state,
      progress: progress.clamp(0, 100),
      message: message,
      ts: DateTime.now().millisecondsSinceEpoch,
    );
    _publishRaw(ackTopic, ack.toJson());
    _log(
      '回执 ${command.cmdId} ${command.type} state=$state progress=$progress：$message',
      state == 'failed'
          ? DeviceLogLevel.error
          : (state == 'success'
              ? DeviceLogLevel.success
              : DeviceLogLevel.info),
    );
  }

  void _otaProgressReset() {
    otaProgress.value = -1;
    if (phase.value == DevicePhase.ota) {
      phase.value = DevicePhase.connected;
    }
  }

  DeviceStatusPayload _offlinePayload(String reason) => DeviceStatusPayload(
    deviceId: _config.deviceId,
    status: 'offline',
    firmwareVersion: _config.firmwareVersion,
    reason: reason,
    ts: DateTime.now().millisecondsSinceEpoch,
  );

  // ---------------- 工具方法 ----------------

  int? _toIntOrNull(Object? value) {
    if (value is int) {
      return value;
    }
    if (value is num) {
      return value.toInt();
    }
    if (value is String) {
      return int.tryParse(value) ?? num.tryParse(value)?.toInt();
    }
    return null;
  }

  double _toDouble(Object? value) {
    if (value is num) {
      return value.toDouble();
    }
    if (value is String) {
      return double.tryParse(value) ?? 0;
    }
    return 0;
  }

  /// 记录事件日志（最多保留 100 条，最新在最前）。
  void _log(String message, [DeviceLogLevel level = DeviceLogLevel.info]) {
    final next = <DeviceLogEntry>[
      DeviceLogEntry(DateTime.now(), message, level),
      ...logs.value,
    ];
    logs.value = next.length > 100 ? next.sublist(0, 100) : next;
    debugPrint('[设备] $message');
  }
}
