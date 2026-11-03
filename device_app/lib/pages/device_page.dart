import 'dart:async';

import 'package:flutter/material.dart';

import '../config/device_config.dart';
import '../services/device_mqtt_service.dart';
import '../widgets/log_panel.dart';
import '../widgets/sensor_card.dart';
import '../widgets/status_badge.dart';

/// 设备端单页界面：状态总览 + 传感器数据 + 控制区 + 事件日志。
class DevicePage extends StatefulWidget {
  const DevicePage({super.key});

  @override
  State<DevicePage> createState() => _DevicePageState();
}

class _DevicePageState extends State<DevicePage> {
  late final DeviceMqttService _service;
  late final TextEditingController _hostController;
  late final TextEditingController _deviceIdController;
  late final TextEditingController _sleepController;
  late final TextEditingController _windowController;

  bool _applying = false;

  @override
  void initState() {
    super.initState();
    const initial = DeviceConfig();
    _service = DeviceMqttService(config: initial);
    _hostController = TextEditingController(text: initial.brokerHost);
    _deviceIdController = TextEditingController(text: initial.deviceId);
    _sleepController = TextEditingController(text: '${initial.sleepSec}');
    _windowController = TextEditingController(
      text: '${initial.commandWindowSec}',
    );
  }

  @override
  void dispose() {
    _hostController.dispose();
    _deviceIdController.dispose();
    _sleepController.dispose();
    _windowController.dispose();
    _service.dispose();
    super.dispose();
  }

  Future<void> _applyConfig() async {
    final sleepSec = int.tryParse(_sleepController.text.trim());
    final windowSec = int.tryParse(_windowController.text.trim());
    if (sleepSec == null || sleepSec <= 0) {
      _showMessage('休眠秒数必须为正整数');
      return;
    }
    if (windowSec == null || windowSec <= 0) {
      _showMessage('指令窗口秒数必须为正整数');
      return;
    }
    setState(() => _applying = true);
    try {
      await _service.applySettings(
        brokerHost: _hostController.text,
        deviceId: _deviceIdController.text,
        sleepSec: sleepSec,
        commandWindowSec: windowSec,
      );
      if (mounted) {
        _showMessage('配置已应用');
      }
    } catch (error) {
      if (mounted) {
        _showMessage('配置应用失败：$error');
      }
    } finally {
      if (mounted) {
        setState(() => _applying = false);
      }
    }
  }

  void _showMessage(String text) {
    ScaffoldMessenger.of(context).showSnackBar(
      SnackBar(content: Text(text), duration: const Duration(seconds: 2)),
    );
  }

  String _formatReportTime(int millis) {
    final time = DateTime.fromMillisecondsSinceEpoch(millis);
    String two(int value) => value.toString().padLeft(2, '0');
    return '${two(time.hour)}:${two(time.minute)}:${two(time.second)}';
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('IoT 设备端模拟器'),
        centerTitle: false,
      ),
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.all(12),
          child: Column(
            children: [
              _buildHeader(),
              const SizedBox(height: 12),
              Expanded(
                child: SingleChildScrollView(
                  child: Column(
                    crossAxisAlignment: CrossAxisAlignment.stretch,
                    children: [
                      _buildSensorSection(),
                      const SizedBox(height: 12),
                      _buildControlSection(),
                    ],
                  ),
                ),
              ),
              const SizedBox(height: 12),
              SizedBox(
                height: 220,
                child: ValueListenableBuilder<List<DeviceLogEntry>>(
                  valueListenable: _service.logs,
                  builder: (context, entries, _) =>
                      LogPanel(entries: entries),
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _buildHeader() {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: AnimatedBuilder(
          animation: Listenable.merge([
            _service.configNotifier,
            _service.firmwareVersion,
            _service.phase,
            _service.connected,
            _service.countdown,
            _service.seq,
            _service.otaProgress,
          ]),
          builder: (context, _) {
            final config = _service.configNotifier.value;
            final phase = _service.phase.value;
            final countdown = _service.countdown.value;
            final progress = _service.otaProgress.value;
            return Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                Row(
                  children: [
                    const Icon(Icons.memory, size: 28),
                    const SizedBox(width: 10),
                    Expanded(
                      child: Column(
                        crossAxisAlignment: CrossAxisAlignment.start,
                        children: [
                          Text(
                            '设备 ${config.deviceId}',
                            style: const TextStyle(
                              fontSize: 17,
                              fontWeight: FontWeight.w700,
                            ),
                          ),
                          const SizedBox(height: 2),
                          Text(
                            '固件 ${_service.firmwareVersion.value} · '
                            'Broker ${config.brokerHost}:${config.brokerPort}',
                            style: const TextStyle(
                              fontSize: 12,
                              color: Colors.black54,
                            ),
                          ),
                        ],
                      ),
                    ),
                    StatusBadge(phase: phase),
                  ],
                ),
                const SizedBox(height: 10),
                Row(
                  children: [
                    MqttIndicator(connected: _service.connected.value),
                    const Spacer(),
                    Text(
                      '上报序号 seq ${_service.seq.value}',
                      style: const TextStyle(
                        fontSize: 12,
                        color: Colors.black54,
                      ),
                    ),
                  ],
                ),
                const SizedBox(height: 6),
                Text(
                  phase.countdownLabel(countdown),
                  style: TextStyle(
                    fontSize: 13,
                    color: phase == DevicePhase.ota
                        ? Colors.purple
                        : Colors.black87,
                  ),
                ),
                if (progress >= 0) ...[
                  const SizedBox(height: 8),
                  LinearProgressIndicator(value: progress / 100),
                  const SizedBox(height: 4),
                  Text(
                    progress >= 100
                        ? 'OTA 已完成（100%）'
                        : 'OTA 进度 ${progress.toStringAsFixed(0)}%',
                    style: const TextStyle(
                      fontSize: 12,
                      color: Colors.purple,
                    ),
                  ),
                ],
              ],
            );
          },
        ),
      ),
    );
  }

  Widget _buildSensorSection() {
    return AnimatedBuilder(
      animation: _service.lastReport,
      builder: (context, _) {
        final report = _service.lastReport.value;
        if (report == null) {
          return Card(
            child: Padding(
              padding: const EdgeInsets.all(16),
              child: Row(
                children: const [
                  Icon(Icons.sensors_off, color: Colors.grey),
                  SizedBox(width: 8),
                  Expanded(
                    child: Text(
                      '暂无上报数据：点击「启动循环」后设备会自动唤醒并上报',
                      style: TextStyle(fontSize: 13, color: Colors.black54),
                    ),
                  ),
                ],
              ),
            ),
          );
        }
        return Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Wrap(
              spacing: 8,
              runSpacing: 8,
              children: [
                SensorCard(
                  label: '电压',
                  value: report.voltage.toStringAsFixed(2),
                  unit: 'V',
                  icon: Icons.battery_charging_full,
                  color: Colors.blue,
                ),
                SensorCard(
                  label: '温度',
                  value: report.temperature.toStringAsFixed(1),
                  unit: '℃',
                  icon: Icons.thermostat,
                  color: Colors.deepOrange,
                ),
                SensorCard(
                  label: '湿度',
                  value: report.humidity.toStringAsFixed(1),
                  unit: '%RH',
                  icon: Icons.water_drop,
                  color: Colors.teal,
                ),
                SensorCard(
                  label: '信号',
                  value: '${report.rssi}',
                  unit: 'dBm',
                  icon: Icons.wifi,
                  color: Colors.purple,
                ),
                SensorCard(
                  label: '电量',
                  value: '${report.battery}',
                  unit: '%',
                  icon: Icons.battery_5_bar,
                  color: Colors.green,
                ),
              ],
            ),
            const SizedBox(height: 8),
            Text(
              '最近上报：seq ${report.seq} · '
              '${_formatReportTime(report.ts)}',
              style: const TextStyle(fontSize: 12, color: Colors.black54),
            ),
          ],
        );
      },
    );
  }

  Widget _buildControlSection() {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(12),
        child: AnimatedBuilder(
          animation: Listenable.merge([
            _service.running,
            _service.configNotifier,
          ]),
          builder: (context, _) {
            final running = _service.running.value;
            final config = _service.configNotifier.value;
            return Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              children: [
                const Text(
                  '控制区',
                  style: TextStyle(fontWeight: FontWeight.w600),
                ),
                const SizedBox(height: 8),
                Wrap(
                  spacing: 8,
                  runSpacing: 8,
                  children: [
                    FilledButton.icon(
                      onPressed: () {
                        if (running) {
                          unawaited(_service.stop());
                        } else {
                          _service.start();
                        }
                      },
                      icon: Icon(
                        running ? Icons.stop_circle : Icons.play_circle,
                      ),
                      label: Text(running ? '停止循环' : '启动循环'),
                    ),
                    OutlinedButton.icon(
                      onPressed: _service.wakeNow,
                      icon: const Icon(Icons.wb_sunny_outlined),
                      label: const Text('立即唤醒'),
                    ),
                    OutlinedButton.icon(
                      onPressed: _service.reportNow,
                      icon: const Icon(Icons.upload),
                      label: const Text('手动上报'),
                    ),
                  ],
                ),
                const SizedBox(height: 12),
                Wrap(
                  spacing: 8,
                  runSpacing: 8,
                  crossAxisAlignment: WrapCrossAlignment.center,
                  children: [
                    SizedBox(
                      width: 200,
                      child: TextField(
                        controller: _hostController,
                        decoration: const InputDecoration(
                          labelText: 'Broker 地址',
                          isDense: true,
                          border: OutlineInputBorder(),
                        ),
                      ),
                    ),
                    SizedBox(
                      width: 150,
                      child: TextField(
                        controller: _deviceIdController,
                        decoration: const InputDecoration(
                          labelText: '设备 ID',
                          isDense: true,
                          border: OutlineInputBorder(),
                        ),
                      ),
                    ),
                    SizedBox(
                      width: 120,
                      child: TextField(
                        controller: _sleepController,
                        keyboardType: TextInputType.number,
                        decoration: const InputDecoration(
                          labelText: '休眠秒数',
                          isDense: true,
                          border: OutlineInputBorder(),
                        ),
                      ),
                    ),
                    SizedBox(
                      width: 130,
                      child: TextField(
                        controller: _windowController,
                        keyboardType: TextInputType.number,
                        decoration: const InputDecoration(
                          labelText: '指令窗口秒数',
                          isDense: true,
                          border: OutlineInputBorder(),
                        ),
                      ),
                    ),
                    FilledButton.tonal(
                      onPressed: _applying ? null : _applyConfig,
                      child: _applying
                          ? const SizedBox(
                              width: 16,
                              height: 16,
                              child: CircularProgressIndicator(strokeWidth: 2),
                            )
                          : const Text('应用配置'),
                    ),
                  ],
                ),
                const SizedBox(height: 8),
                Text(
                  '当前生效：休眠 ${config.sleepSec}s · 指令窗口 '
                  '${config.commandWindowSec}s · 上报间隔 '
                  '${config.reportIntervalSec}s',
                  style: const TextStyle(fontSize: 12, color: Colors.black54),
                ),
              ],
            );
          },
        ),
      ),
    );
  }
}
