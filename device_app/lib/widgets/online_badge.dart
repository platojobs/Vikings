import 'package:flutter/material.dart';

/// 设备在线徽标：与 REST 的 online/offline 对应。
class OnlineBadge extends StatelessWidget {
  const OnlineBadge({super.key, required this.online});

  final bool online;

  @override
  Widget build(BuildContext context) {
    final color = online ? Colors.green : Colors.grey;
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 9, vertical: 4),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.12),
        borderRadius: BorderRadius.circular(14),
        border: Border.all(color: color.withValues(alpha: 0.45)),
      ),
      child: Row(
        mainAxisSize: MainAxisSize.min,
        children: [
          Icon(Icons.circle, size: 8, color: color),
          const SizedBox(width: 5),
          Text(
            online ? '在线' : '离线',
            style: TextStyle(
              color: color,
              fontSize: 12,
              fontWeight: FontWeight.w600,
            ),
          ),
        ],
      ),
    );
  }
}
