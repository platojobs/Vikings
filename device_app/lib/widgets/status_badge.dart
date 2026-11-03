import 'package:flutter/material.dart';

import '../services/device_mqtt_service.dart';

/// 设备状态徽标：在线绿 / 离线灰 / 休眠蓝 / 唤醒橙 / OTA 紫。
class StatusBadge extends StatelessWidget {
  const StatusBadge({super.key, required this.phase});

  final DevicePhase phase;

  Color get _color => switch (phase) {
    DevicePhase.idle => Colors.grey,
    DevicePhase.sleeping => Colors.blue,
    DevicePhase.waking => Colors.orange,
    DevicePhase.connected || DevicePhase.reporting => Colors.green,
    DevicePhase.ota => Colors.purple,
  };

  @override
  Widget build(BuildContext context) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
      decoration: BoxDecoration(
        color: _color.withValues(alpha: 0.12),
        borderRadius: BorderRadius.circular(20),
        border: Border.all(color: _color.withValues(alpha: 0.5)),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Container(
            width: 8,
            height: 8,
            decoration: BoxDecoration(color: _color, shape: BoxShape.circle),
          ),
          const SizedBox(width: 6),
          Text(
            phase.label,
            style: TextStyle(
              color: _color,
              fontWeight: FontWeight.w600,
              fontSize: 13,
            ),
          ),
        ],
      ),
    );
  }
}

/// MQTT 连接指示灯：绿色已连接 / 灰色未连接。
class MqttIndicator extends StatelessWidget {
  const MqttIndicator({super.key, required this.connected});

  final bool connected;

  @override
  Widget build(BuildContext context) {
    final color = connected ? Colors.green : Colors.grey;
    return Row(
      mainAxisSize: MainAxisSize.min,
      children: [
        Icon(Icons.circle, size: 10, color: color),
        const SizedBox(width: 6),
        Text(
          connected ? 'MQTT 已连接' : 'MQTT 未连接',
          style: TextStyle(fontSize: 12, color: color),
        ),
      ],
    );
  }
}
