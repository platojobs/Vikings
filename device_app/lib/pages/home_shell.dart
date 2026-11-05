import 'dart:async';

import 'package:flutter/material.dart';

import '../models/realtime_payload.dart';
import '../services/app_storage.dart';
import '../services/board_realtime_service.dart';
import 'alert_center_page.dart';
import 'device_page.dart';
import 'mobile_board_page.dart';
import 'overview_page.dart';

/// 设备端入口（v1.27.0）：
/// - tab 0（默认）：概览，设备/告警/趋势/异常设备的产品化首页；
/// - tab 1：移动看板，登录网关后查看实时数据与受限写操作；
/// - tab 2：告警中心，筛选 / 分页 / 批量确认 / 实时未读角标；
/// - tab 3：工具，保留原有设备模拟器（本地模拟硬件，不依赖后端）。
///
/// 实时连接只有一个：告警中心在 App 前台保持订阅，并把事件与连接状态转发给
/// 概览页；概览页因此不再新建 MQTT 连接，避免两条常驻通道与状态不一致。
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

  /// 概览页复用的前台实时事件流：由告警中心的订阅转发，不额外建连接。
  final StreamController<RealtimeEvent> _liveEvents =
      StreamController<RealtimeEvent>.broadcast();
  final ValueNotifier<RealtimeState> _liveState = ValueNotifier<RealtimeState>(
    RealtimeState.idle,
  );
  final ValueNotifier<bool> _livePolling = ValueNotifier<bool>(false);

  /// 从概览跳到看板时聚焦的设备；seq 用于同一设备重复点击也能触发过滤。
  String _boardFocusDevice = '';
  int _boardFocusSeq = 0;

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

  void _openDeviceInBoard(String deviceId) {
    setState(() {
      _boardFocusDevice = deviceId;
      _boardFocusSeq += 1;
      _index = 1;
    });
  }

  @override
  void dispose() {
    _liveEvents.close();
    _liveState.dispose();
    _livePolling.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: IndexedStack(
        index: _index,
        children: [
          // 概览本身不持有 MQTT 连接，只消费告警中心转发的事件与状态
          OverviewPage(
            key: const ValueKey('overview-tab'),
            active: _index == 0,
            storage: widget.storage,
            apiClientFactory: widget.apiClientFactory,
            liveEvents: _liveEvents.stream,
            feedState: _liveState,
            feedPolling: _livePolling,
            onOpenDeviceInBoard: _openDeviceInBoard,
          ),
          // 看板只在可见时保持实时连接，切走后断开省电
          MobileBoardPage(
            active: _index == 1,
            storage: widget.storage,
            apiClientFactory: widget.apiClientFactory,
            realtimeServiceFactory: widget.realtimeServiceFactory,
            focusDeviceId: _boardFocusDevice,
            focusSeq: _boardFocusSeq,
          ),
          // 告警中心保持前台实时订阅，保证未读角标在其它 tab 上也能累加，
          // 同时作为概览页的唯一实时数据源
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
            onRealtimeEvent: (event) {
              if (!_liveEvents.isClosed) {
                _liveEvents.add(event);
              }
            },
            onRealtimeState: (state, polling) {
              _liveState.value = state;
              _livePolling.value = polling;
            },
          ),
          const DevicePage(),
        ],
      ),
      bottomNavigationBar: NavigationBar(
        selectedIndex: _index,
        onDestinationSelected: _onDestinationSelected,
        destinations: [
          const NavigationDestination(
            icon: Icon(Icons.insights_outlined),
            selectedIcon: Icon(Icons.insights),
            label: '概览',
          ),
          const NavigationDestination(
            icon: Icon(Icons.dashboard_outlined),
            selectedIcon: Icon(Icons.dashboard),
            label: '看板',
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
          const NavigationDestination(
            icon: Icon(Icons.build_outlined),
            selectedIcon: Icon(Icons.build),
            label: '工具',
          ),
        ],
      ),
    );
  }
}
