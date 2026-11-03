import 'package:flutter/material.dart';

import '../services/device_mqtt_service.dart';

/// 滚动事件日志面板：带时间戳、按级别着色，服务端已限制最多 100 条。
class LogPanel extends StatelessWidget {
  const LogPanel({super.key, required this.entries});

  final List<DeviceLogEntry> entries;

  Color _levelColor(DeviceLogLevel level) => switch (level) {
    DeviceLogLevel.info => Colors.black87,
    DeviceLogLevel.success => Colors.green.shade700,
    DeviceLogLevel.warning => Colors.orange.shade800,
    DeviceLogLevel.error => Colors.red.shade700,
  };

  IconData _levelIcon(DeviceLogLevel level) => switch (level) {
    DeviceLogLevel.info => Icons.info_outline,
    DeviceLogLevel.success => Icons.check_circle_outline,
    DeviceLogLevel.warning => Icons.warning_amber_rounded,
    DeviceLogLevel.error => Icons.error_outline,
  };

  String _formatTime(DateTime time) {
    String two(int value) => value.toString().padLeft(2, '0');
    return '${two(time.hour)}:${two(time.minute)}:${two(time.second)}.'
        '${time.millisecond.toString().padLeft(3, '0')}';
  }

  @override
  Widget build(BuildContext context) {
    return Container(
      decoration: BoxDecoration(
        color: Colors.grey.shade50,
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: Colors.grey.shade300),
      ),
      child: Column(
        children: [
          Padding(
            padding: const EdgeInsets.symmetric(horizontal: 12, vertical: 8),
            child: Row(
              children: [
                const Icon(Icons.receipt_long, size: 16),
                const SizedBox(width: 6),
                const Text(
                  '事件日志',
                  style: TextStyle(fontWeight: FontWeight.w600),
                ),
                const Spacer(),
                Text(
                  '${entries.length} / 100 条',
                  style: const TextStyle(fontSize: 12, color: Colors.black54),
                ),
              ],
            ),
          ),
          const Divider(height: 1),
          Expanded(
            child: entries.isEmpty
                ? const Center(
                    child: Text(
                      '暂无日志，点击「启动循环」开始模拟',
                      style: TextStyle(color: Colors.black54, fontSize: 13),
                    ),
                  )
                : ListView.builder(
                    padding: const EdgeInsets.symmetric(vertical: 4),
                    itemCount: entries.length,
                    itemBuilder: (context, index) {
                      final entry = entries[index];
                      final color = _levelColor(entry.level);
                      return Padding(
                        padding: const EdgeInsets.symmetric(
                          horizontal: 12,
                          vertical: 3,
                        ),
                        child: Row(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(
                              _formatTime(entry.time),
                              style: const TextStyle(
                                fontSize: 11,
                                color: Colors.black45,
                                fontFeatures: [FontFeature.tabularFigures()],
                              ),
                            ),
                            const SizedBox(width: 6),
                            Icon(
                              _levelIcon(entry.level),
                              size: 12,
                              color: color,
                            ),
                            const SizedBox(width: 6),
                            Expanded(
                              child: Text(
                                entry.message,
                                style: TextStyle(fontSize: 12, color: color),
                              ),
                            ),
                          ],
                        ),
                      );
                    },
                  ),
          ),
        ],
      ),
    );
  }
}
