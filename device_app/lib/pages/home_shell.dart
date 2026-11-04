import 'package:flutter/material.dart';

import 'device_page.dart';
import 'mobile_board_page.dart';

/// 设备端双模式入口：
/// - 设备模拟（默认）：本地模拟硬件，不依赖后端，保留原有调试能力；
/// - 移动看板：登录网关后只读查看真实数据。
class HomeShell extends StatefulWidget {
  const HomeShell({super.key});

  @override
  State<HomeShell> createState() => _HomeShellState();
}

class _HomeShellState extends State<HomeShell> {
  int _index = 0;

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: IndexedStack(
        index: _index,
        children: [
          const DevicePage(),
          // 看板只在可见时保持实时连接，切走后断开省电
          MobileBoardPage(active: _index == 1),
        ],
      ),
      bottomNavigationBar: NavigationBar(
        selectedIndex: _index,
        onDestinationSelected: (index) => setState(() => _index = index),
        destinations: const [
          NavigationDestination(
            icon: Icon(Icons.memory_outlined),
            selectedIcon: Icon(Icons.memory),
            label: '设备模拟',
          ),
          NavigationDestination(
            icon: Icon(Icons.dashboard_outlined),
            selectedIcon: Icon(Icons.dashboard),
            label: '移动看板',
          ),
        ],
      ),
    );
  }
}
