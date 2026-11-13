import 'package:flutter/material.dart';

import 'pages/home_shell.dart';
import 'widgets/app_feedback.dart';

void main() {
  runApp(const IotDeviceApp());
}

/// 智能硬件 IoT 全链路管理平台 - Flutter 设备端（模拟电池 BLE 传感器 + 只读移动看板）。
class IotDeviceApp extends StatelessWidget {
  const IotDeviceApp({super.key});

  @override
  Widget build(BuildContext context) {
    return MaterialApp(
      title: 'IoT 设备端',
      debugShowCheckedModeBanner: false,
      // 提示层挂在根导航上：页面销毁/跳转后（如 401 回登录页）仍能显示。
      navigatorKey: AppFeedback.navigatorKey,
      theme: ThemeData(
        useMaterial3: true,
        colorSchemeSeed: Colors.teal,
        scaffoldBackgroundColor: const Color(0xFFF5F7F8),
      ),
      home: const HomeShell(),
    );
  }
}
