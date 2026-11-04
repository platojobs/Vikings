import 'package:flutter/material.dart';

import '../services/app_storage.dart';
import 'alert_center_page.dart';
import 'device_page.dart';
import 'mobile_board_page.dart';

/// 设备端入口：
/// - tab 0（默认）：设备模拟，本地模拟硬件，不依赖后端；
/// - tab 1：移动看板，登录网关后查看实时数据与受限写操作；
/// - tab 2：告警中心，筛选 / 分页 / 批量确认 / 实时未读角标。
class HomeShell extends StatefulWidget {
  const HomeShell({
    super.key,
    this.storage,
    this.apiClientFactory,
    this.realtimeServiceFactory,
  });

  /// 仅用于测试注入；为空时两个页面各自使用生产默认实现。
  final AppStorage? storage;
  final BoardApiClientFactory? apiClientFactory;
  final BoardRealtimeFactory? realtimeServiceFactory;

  @override
  State<HomeShell> createState() => _HomeShellState();
}

class _HomeShellState extends State<HomeShell> {
  int _index = 0;

  /// 告警中心 tab 的未读角标；进入该 tab 即清零。
  int _unreadAlerts = 0;

  void _onDestinationSelected(int index) {
    if (index == 2 && _unreadAlerts != 0) {
      setState(() {
        _index = index;
        _unreadAlerts = 0;
      });
      return;
    }
    setState(() => _index = index);
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: IndexedStack(
        index: _index,
        children: [
          const DevicePage(),
          // 看板只在可见时保持实时连接，切走后断开省电
          MobileBoardPage(
            active: _index == 1,
            storage: widget.storage,
            apiClientFactory: widget.apiClientFactory,
            realtimeServiceFactory: widget.realtimeServiceFactory,
          ),
          // 告警中心保持前台实时订阅，保证未读角标在其它 tab 上也能累加
          AlertCenterPage(
            active: _index == 2,
            storage: widget.storage,
            apiClientFactory: widget.apiClientFactory,
            realtimeServiceFactory: widget.realtimeServiceFactory,
            onUnreadChanged: (count) {
              if (!mounted || count == _unreadAlerts) {
                return;
              }
              setState(() => _unreadAlerts = count);
            },
          ),
        ],
      ),
      bottomNavigationBar: NavigationBar(
        selectedIndex: _index,
        onDestinationSelected: _onDestinationSelected,
        destinations: [
          const NavigationDestination(
            icon: Icon(Icons.memory_outlined),
            selectedIcon: Icon(Icons.memory),
            label: '设备模拟',
          ),
          const NavigationDestination(
            icon: Icon(Icons.dashboard_outlined),
            selectedIcon: Icon(Icons.dashboard),
            label: '移动看板',
          ),
          NavigationDestination(
            icon: Badge(
              key: const ValueKey('alert-tab-badge'),
              isLabelVisible: _unreadAlerts > 0,
              label: Text(_unreadAlerts > 99 ? '99+' : '$_unreadAlerts'),
              child: const Icon(Icons.notifications_outlined),
            ),
            selectedIcon: Badge(
              isLabelVisible: _unreadAlerts > 0,
              label: Text(_unreadAlerts > 99 ? '99+' : '$_unreadAlerts'),
              child: const Icon(Icons.notifications),
            ),
            label: '告警中心',
          ),
        ],
      ),
    );
  }
}
