import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';

import '../config/api_config.dart';
import '../models/mobile_api_models.dart';
import '../models/realtime_payload.dart';
import '../services/api_client.dart';
import '../services/app_storage.dart';
import '../services/board_realtime_service.dart';
import '../services/session_refresh.dart';
import '../widgets/app_feedback.dart';
import 'login_page.dart';

/// 概览：HomeShell 的默认 tab（第 0 页）。
///
/// 设计约束（v1.27.0）：
/// - 数据全部来自 `GET /api/stats/overview`，与 Web 仪表盘同一套口径，不造数据；
/// - **不新建 MQTT 连接**：告警中心在前台已保持一条订阅，HomeShell 把事件与
///   连接状态转发给本页；本页只在可见时监听事件、在实时不可用时做 REST 轮询；
/// - 切走 tab、App 进入后台、登录失效都会停掉监听与轮询，不留悬空定时器；
/// - 只读视图，viewer / operator / admin 看到的信息一致；写操作仍在看板与告警中心。
class OverviewPage extends StatefulWidget {
  const OverviewPage({
    super.key,
    this.active = true,
    this.storage,
    this.apiClientFactory,
    this.liveEvents,
    this.feedState,
    this.feedPolling,
    this.onOpenDeviceInBoard,
  });

  /// 是否为当前展示 tab；切走后停止监听与轮询。
  final bool active;

  /// 仅用于测试注入；为空时使用生产默认实现。
  final AppStorage? storage;
  final ApiClient Function(String baseUrl)? apiClientFactory;

  /// 告警中心前台订阅转发出来的实时事件（HomeShell 提供）。
  final Stream<RealtimeEvent>? liveEvents;

  /// 同一订阅的连接状态与轮询兜底状态（HomeShell 提供）。
  final ValueListenable<RealtimeState>? feedState;
  final ValueListenable<bool>? feedPolling;

  /// 点击异常设备详情里的「在移动看板查看」时切到看板 tab 并聚焦该设备。
  final void Function(String deviceId)? onOpenDeviceInBoard;

  @override
  State<OverviewPage> createState() => _OverviewPageState();
}

class _OverviewPageState extends State<OverviewPage>
    with WidgetsBindingObserver {
  static const Duration _pollInterval = Duration(seconds: 15);
  static const Duration _eventRefreshDelay = Duration(milliseconds: 800);

  late final AppStorage _storage;
  late final SessionRefreshCoordinator _refresher;
  late final ApiClient Function(String baseUrl) _rawCreateApiClient;

  /// 页面创建的每个客户端都绑定这个取消令牌，dispose 时统一取消在途请求。
  final RequestCancellation _cancellation = RequestCancellation();

  ApiClient _createApiClient(String baseUrl) {
    final client = _rawCreateApiClient(baseUrl);
    client.bindCancellation(_cancellation);
    return client;
  }

  AuthSession? _session;
  String _baseUrl = ApiConfig.defaultBaseUrl;
  bool _booting = true;
  bool _loading = false;
  bool _errorVisible = false;
  String? _error;
  String? _notice;

  StatsOverview _stats = StatsOverview.empty;
  int _requestSeq = 0;

  bool _appVisible = true;
  StreamSubscription<RealtimeEvent>? _eventSub;
  Timer? _pollTimer;
  Timer? _refreshDebounce;

  bool get _shouldRun => widget.active && _appVisible && _session != null;

  @override
  void initState() {
    super.initState();
    _storage = widget.storage ?? AppStorage();
    _refresher = SessionRefreshCoordinator(
      storage: _storage,
      baseUrl: () => _baseUrl,
      readSession: () => _session,
      onRefreshed: _onSessionRefreshed,
      clientBuilder: widget.apiClientFactory,
    );
    _rawCreateApiClient =
        widget.apiClientFactory ??
        (String baseUrl) => ApiClient(baseUrl: baseUrl, refresher: _refresher);
    WidgetsBinding.instance.addObserver(this);
    widget.feedPolling?.addListener(_syncLiveLifecycle);
    _bootstrap();
  }

  @override
  void didUpdateWidget(covariant OverviewPage oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.active != widget.active) {
      _syncLiveLifecycle();
      if (widget.active) {
        unawaited(_load(silent: true));
      }
    }
    if (oldWidget.feedPolling != widget.feedPolling) {
      oldWidget.feedPolling?.removeListener(_syncLiveLifecycle);
      widget.feedPolling?.addListener(_syncLiveLifecycle);
      _syncLiveLifecycle();
    }
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    final visible = state == AppLifecycleState.resumed;
    if (visible == _appVisible) {
      return;
    }
    _appVisible = visible;
    _syncLiveLifecycle();
    if (visible && widget.active && _session != null) {
      unawaited(_load(silent: true));
    }
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _cancellation.cancel();
    widget.feedPolling?.removeListener(_syncLiveLifecycle);
    _refreshDebounce?.cancel();
    _pollTimer?.cancel();
    _eventSub?.cancel();
    super.dispose();
  }

  Future<void> _bootstrap() async {
    String baseUrl = ApiConfig.defaultBaseUrl;
    AuthSession? session;
    try {
      baseUrl = await _storage.loadBaseUrl() ?? ApiConfig.defaultBaseUrl;
      session = await _storage.loadSession();
    } catch (_) {
      // 本地存储不可用时退回默认地址与未登录态，页面仍可交互
    }
    if (!mounted) {
      return;
    }
    setState(() {
      _baseUrl = baseUrl;
      _session = session;
      _booting = false;
    });
    if (session != null) {
      await _load();
      _syncLiveLifecycle();
    }
  }

  // ---------------- 实时事件与轮询 ----------------

  /// 复用告警中心的前台订阅：本页不创建连接，只在可见时挂监听、按需轮询。
  void _syncLiveLifecycle() {
    if (!_shouldRun) {
      _eventSub?.cancel();
      _eventSub = null;
      _stopPolling();
      _refreshDebounce?.cancel();
      _refreshDebounce = null;
      return;
    }
    final events = widget.liveEvents;
    if (events != null && _eventSub == null) {
      _eventSub = events.listen(_onRealtimeEvent);
    }
    final needsPolling = widget.feedPolling?.value ?? true;
    if (needsPolling) {
      _startPolling();
    } else {
      _stopPolling();
    }
  }

  void _startPolling() {
    if (_pollTimer != null || !mounted) {
      return;
    }
    _pollTimer = Timer.periodic(_pollInterval, (_) {
      if (!_loading) {
        unawaited(_load(silent: true));
      }
    });
  }

  void _stopPolling() {
    _pollTimer?.cancel();
    _pollTimer = null;
  }

  /// 实时事件先做轻量增量（上报量 / 告警数），再防抖拉一次服务端快照校正，
  /// 保证界面既即时又不会与 REST 口径长期偏离。
  void _onRealtimeEvent(RealtimeEvent event) {
    if (!mounted || !_shouldRun) {
      return;
    }
    switch (event) {
      case ReportEvent():
        if (_stats.deviceTotal > 0) {
          final trend = List<OverviewTrendPoint>.of(_stats.trend);
          if (trend.isNotEmpty) {
            final last = trend.removeLast();
            trend.add(
              OverviewTrendPoint(
                hourStart: last.hourStart,
                reports: last.reports + 1,
                alerts: last.alerts,
              ),
            );
          }
          setState(() {
            _stats = StatsOverview(
              deviceTotal: _stats.deviceTotal,
              deviceOnline: _stats.deviceOnline,
              deviceOffline: _stats.deviceOffline,
              reportToday: _stats.reportToday + 1,
              alertOpen: _stats.alertOpen,
              credentialMissing: _stats.credentialMissing,
              warningOpen: _stats.warningOpen,
              criticalOpen: _stats.criticalOpen,
              trend: trend,
              trendComplete: _stats.trendComplete,
              firstRecordAt: _stats.firstRecordAt,
              abnormalDevices: _stats.abnormalDevices,
            );
          });
        }
      case AlertEvent(:final alert):
        if (_stats.deviceTotal > 0) {
          setState(() {
            _stats = StatsOverview(
              deviceTotal: _stats.deviceTotal,
              deviceOnline: _stats.deviceOnline,
              deviceOffline: _stats.deviceOffline,
              reportToday: _stats.reportToday,
              alertOpen: _stats.alertOpen + 1,
              credentialMissing: _stats.credentialMissing,
              warningOpen:
                  _stats.warningOpen + (alert.level == 'warning' ? 1 : 0),
              criticalOpen:
                  _stats.criticalOpen + (alert.level == 'critical' ? 1 : 0),
              trend: _stats.trend,
              trendComplete: _stats.trendComplete,
              firstRecordAt: _stats.firstRecordAt,
              abnormalDevices: _stats.abnormalDevices,
            );
          });
        }
      case StatusEvent():
      case GroupChangedEvent():
        break;
    }
    _scheduleServerRefresh();
  }

  void _scheduleServerRefresh() {
    _refreshDebounce?.cancel();
    _refreshDebounce = Timer(_eventRefreshDelay, () {
      if (mounted && _shouldRun) {
        unawaited(_load(silent: true));
      }
    });
  }

  // ---------------- 数据加载 ----------------

  Future<void> _load({bool silent = false}) async {
    final session = _session;
    if (session == null) {
      return;
    }
    final requestId = ++_requestSeq;
    if (!silent) {
      setState(() {
        _loading = true;
        _error = null;
        _errorVisible = false;
      });
    }
    final client = _createApiClient(_baseUrl);
    try {
      final stats = await client.fetchStatsOverview(token: session.token);
      if (!mounted || requestId != _requestSeq) {
        return;
      }
      setState(() {
        _stats = stats;
        _loading = false;
        _error = null;
        _errorVisible = false;
      });
    } on ApiException catch (error) {
      if (!mounted || requestId != _requestSeq) {
        return;
      }
      if (error.kind == ApiErrorKind.unauthorized) {
        await _handleExpired();
        return;
      }
      setState(() {
        _loading = false;
        _error = _describe(error);
        _errorVisible = true;
      });
    } catch (error) {
      if (!mounted || requestId != _requestSeq) {
        return;
      }
      setState(() {
        _loading = false;
        _error = '加载概览失败：$error';
        _errorVisible = true;
      });
    } finally {
      client.close();
    }
  }

  String _describe(ApiException error) {
    if (error.kind == ApiErrorKind.network ||
        error.kind == ApiErrorKind.timeout) {
      return '${error.userMessage}\n当前网关：$_baseUrl';
    }
    return error.userMessage;
  }

  void _onSessionRefreshed(AuthSession session) {
    if (!mounted) return;
    setState(() {
      _session = session;
      _notice = null;
    });
  }

  Future<void> _handleExpired() async {
    _stopPolling();
    _eventSub?.cancel();
    _eventSub = null;
    await _storage.clearSession();
    if (!mounted) {
      return;
    }
    setState(() {
      _session = null;
      _notice = '登录已失效，请重新登录';
      _stats = StatsOverview.empty;
      _loading = false;
      _error = null;
      _errorVisible = false;
      _requestSeq += 1;
    });
  }

  Future<void> _logout() async {
    _stopPolling();
    _eventSub?.cancel();
    _eventSub = null;
    final refreshToken = _session?.refreshToken ?? '';
    if (refreshToken.isNotEmpty) {
      try {
        await _createApiClient(_baseUrl)
            .logoutSession(refreshToken: refreshToken);
      } catch (_) {
        // 服务端不可达时仍允许本地退出
      }
    }
    await _storage.clearSession();
    if (!mounted) {
      return;
    }
    setState(() {
      _session = null;
      _stats = StatsOverview.empty;
      _notice = null;
    });
  }

  void _handleLoggedIn(AuthSession session, String baseUrl) {
    setState(() {
      _session = session;
      _baseUrl = baseUrl;
      _notice = null;
      _error = null;
      _errorVisible = false;
      _booting = false;
    });
    unawaited(_load());
    _syncLiveLifecycle();
  }

  @override
  Widget build(BuildContext context) {
    if (_booting) {
      return const Scaffold(body: Center(child: CircularProgressIndicator()));
    }
    final session = _session;
    if (session == null) {
      return LoginPage(
        initialBaseUrl: _baseUrl,
        notice: _notice,
        onLoggedIn: _handleLoggedIn,
        storage: _storage,
        apiClientFactory: _createApiClient,
      );
    }
    return Scaffold(
      appBar: AppBar(
        title: const Text('概览'),
        centerTitle: false,
        actions: [
          IconButton(
            tooltip: '退出登录',
            onPressed: _logout,
            icon: const Icon(Icons.logout),
          ),
        ],
      ),
      body: SafeArea(
        child: RefreshIndicator(
          onRefresh: () => _load(),
          child: SingleChildScrollView(
            physics: const AlwaysScrollableScrollPhysics(),
            padding: const EdgeInsets.fromLTRB(12, 8, 12, 16),
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.stretch,
              children: [
                if (_errorVisible && _error != null) _buildErrorBanner(),
                _buildAccountBar(session),
                const SizedBox(height: 8),
                _buildRealtimeBar(),
                const SizedBox(height: 12),
                if (_loading && _stats.deviceTotal == 0)
                  const Padding(
                    padding: EdgeInsets.symmetric(vertical: 48),
                    child: Center(child: CircularProgressIndicator()),
                  )
                else if (_stats.deviceTotal == 0)
                  _buildEmptyState()
                else ...[
                  _buildKpiGrid(),
                  const SizedBox(height: 12),
                  _buildAlertDistribution(),
                  const SizedBox(height: 12),
                  _buildTrendCard(),
                  const SizedBox(height: 12),
                  _buildAbnormalSection(),
                ],
                const SizedBox(height: 18),
                const Center(
                  child: Text(
                    '概览为只读视图，所有角色看到同一份数据；写操作在「看板」与「告警中心」按角色开放',
                    textAlign: TextAlign.center,
                    style: TextStyle(fontSize: 12, color: Colors.black45),
                  ),
                ),
                const SizedBox(height: 8),
              ],
            ),
          ),
        ),
      ),
    );
  }

  Widget _buildErrorBanner() {
    return AppInlineMessage(
      message: _error!,
      kind: FeedbackKind.error,
      actionLabel: '重试',
      onAction: _loading ? null : () => _load(),
    );
  }

  Widget _buildAccountBar(AuthSession session) {
    return Row(
      children: [
        CircleAvatar(
          radius: 14,
          child: Text(
            session.username.isEmpty
                ? '?'
                : session.username.substring(0, 1).toUpperCase(),
            style: const TextStyle(fontSize: 12),
          ),
        ),
        const SizedBox(width: 8),
        Expanded(
          child: Text(
            '${session.username} · ${session.role}',
            style: const TextStyle(fontSize: 12, color: Colors.black54),
          ),
        ),
        Text(
          _baseUrl,
          style: const TextStyle(fontSize: 11, color: Colors.black38),
        ),
      ],
    );
  }

  /// 顶部状态条：沿用看板/告警中心的状态文案，实时不可用时明确标注轮询。
  Widget _buildRealtimeBar() {
    final state = widget.feedState;
    final polling = widget.feedPolling;
    if (state == null) {
      return Row(
        key: const ValueKey('overview-realtime-bar'),
        children: [
          Container(
            width: 8,
            height: 8,
            decoration: const BoxDecoration(
              color: Colors.grey,
              shape: BoxShape.circle,
            ),
          ),
          const SizedBox(width: 6),
          const Expanded(
            child: Text(
              '实时未接入 · 概览使用 15 秒轮询',
              style: TextStyle(fontSize: 12, color: Colors.grey),
            ),
          ),
        ],
      );
    }
    return ValueListenableBuilder<RealtimeState>(
      valueListenable: state,
      builder: (context, value, _) {
        final isPolling = polling?.value ?? value.requiresPollingFallback;
        final color = switch (value) {
          RealtimeState.connected => Colors.green,
          RealtimeState.connecting => Colors.blueGrey,
          RealtimeState.reconnecting => Colors.orange,
          _ => Colors.grey,
        };
        final text = isPolling ? '${value.label} · 已切换轮询（每 15 秒）' : value.label;
        return Row(
          key: const ValueKey('overview-realtime-bar'),
          children: [
            Container(
              width: 8,
              height: 8,
              decoration: BoxDecoration(color: color, shape: BoxShape.circle),
            ),
            const SizedBox(width: 6),
            Expanded(
              child: Text(text, style: TextStyle(fontSize: 12, color: color)),
            ),
            const Text(
              '复用告警中心实时订阅',
              style: TextStyle(fontSize: 11, color: Colors.black38),
            ),
          ],
        );
      },
    );
  }

  Widget _buildKpiGrid() {
    final rate = _stats.deviceTotal == 0
        ? '—'
        : '${(_stats.deviceOnline * 100 / _stats.deviceTotal).round()}%';
    return Column(
      children: [
        Row(
          children: [
            Expanded(
              child: _kpiCard(
                key: const ValueKey('overview-device-total'),
                label: '设备总数',
                value: '${_stats.deviceTotal}',
                hint: '在线 ${_stats.deviceOnline} · 离线 ${_stats.deviceOffline}',
                color: Colors.blue,
                icon: Icons.devices_other,
              ),
            ),
            const SizedBox(width: 10),
            Expanded(
              child: _kpiCard(
                key: const ValueKey('overview-online-rate'),
                label: '在线率',
                value: rate,
                hint: '${_stats.deviceOnline}/${_stats.deviceTotal} 台在线',
                color: Colors.green,
                icon: Icons.wifi,
              ),
            ),
          ],
        ),
        const SizedBox(height: 10),
        Row(
          children: [
            Expanded(
              child: _kpiCard(
                key: const ValueKey('overview-alert-open'),
                label: '未处理告警',
                value: '${_stats.alertOpen}',
                hint:
                    'warning ${_stats.warningOpen} · critical ${_stats.criticalOpen}',
                color: _stats.criticalOpen > 0 ? Colors.red : Colors.orange,
                icon: Icons.notification_important_outlined,
              ),
            ),
            const SizedBox(width: 10),
            Expanded(
              child: _kpiCard(
                key: const ValueKey('overview-credential-missing'),
                label: '未签发凭证',
                value: '${_stats.credentialMissing}',
                hint: _stats.credentialMissing > 0
                    ? '这些设备无法接入 Broker'
                    : '全部设备已签发',
                color: _stats.credentialMissing > 0
                    ? Colors.deepOrange
                    : Colors.blueGrey,
                icon: Icons.key_off_outlined,
              ),
            ),
          ],
        ),
      ],
    );
  }

  Widget _kpiCard({
    required Key key,
    required String label,
    required String value,
    required String hint,
    required Color color,
    required IconData icon,
  }) {
    return Container(
      key: key,
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.06),
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: color.withValues(alpha: 0.25)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Icon(icon, size: 16, color: color),
              const SizedBox(width: 6),
              Text(label, style: TextStyle(fontSize: 12, color: color)),
            ],
          ),
          const SizedBox(height: 8),
          Text(
            value,
            style: TextStyle(
              fontSize: 22,
              fontWeight: FontWeight.w600,
              color: color,
            ),
          ),
          const SizedBox(height: 4),
          Text(
            hint,
            style: const TextStyle(fontSize: 11, color: Colors.black54),
          ),
        ],
      ),
    );
  }

  Widget _buildAlertDistribution() {
    final total = _stats.warningOpen + _stats.criticalOpen;
    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: Colors.black12),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          const Text(
            '未处理告警级别分布',
            style: TextStyle(fontSize: 13, fontWeight: FontWeight.w600),
          ),
          const SizedBox(height: 10),
          if (total == 0)
            const Text(
              '当前没有未处理告警',
              style: TextStyle(fontSize: 12, color: Colors.black54),
            )
          else ...[
            _levelBar('warning', _stats.warningOpen, total, Colors.orange),
            const SizedBox(height: 8),
            _levelBar('critical', _stats.criticalOpen, total, Colors.red),
          ],
        ],
      ),
    );
  }

  Widget _levelBar(String level, int count, int total, Color color) {
    final ratio = total == 0 ? 0.0 : count / total;
    return Row(
      children: [
        SizedBox(
          width: 64,
          child: Text(level, style: TextStyle(fontSize: 12, color: color)),
        ),
        Expanded(
          child: ClipRRect(
            borderRadius: BorderRadius.circular(4),
            child: LinearProgressIndicator(
              value: ratio,
              minHeight: 8,
              backgroundColor: color.withValues(alpha: 0.12),
              valueColor: AlwaysStoppedAnimation<Color>(color),
            ),
          ),
        ),
        const SizedBox(width: 8),
        SizedBox(
          width: 32,
          child: Text(
            '$count',
            textAlign: TextAlign.right,
            style: const TextStyle(fontSize: 12, fontWeight: FontWeight.w600),
          ),
        ),
      ],
    );
  }

  Widget _buildTrendCard() {
    final points = _stats.trend;
    final totalReports = points.fold<int>(0, (sum, p) => sum + p.reports);
    final totalAlerts = points.fold<int>(0, (sum, p) => sum + p.alerts);
    String note;
    if (points.isEmpty) {
      note = '暂无趋势数据';
    } else if (!_stats.trendComplete) {
      final first = _stats.firstRecordAt;
      note =
          '数据不足 24 小时${first == null ? '' : '（最早一条上报 ${_formatDateTime(first)}）'}，'
          '仅展示已有真实数据';
    } else if (totalReports == 0) {
      note = '近 24 小时没有上报';
    } else {
      note = '近 24 小时共 $totalReports 条上报 · $totalAlerts 条新增告警';
    }
    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: Colors.black12),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              const Expanded(
                child: Text(
                  '近 24 小时趋势',
                  style: TextStyle(fontSize: 13, fontWeight: FontWeight.w600),
                ),
              ),
              _legendDot(Colors.blue, '上报'),
              const SizedBox(width: 10),
              _legendDot(Colors.orange, '新增告警'),
            ],
          ),
          const SizedBox(height: 10),
          if (points.isEmpty || totalReports + totalAlerts == 0)
            const SizedBox(
              height: 100,
              child: Center(
                child: Text(
                  '暂无近 24 小时数据',
                  style: TextStyle(fontSize: 12, color: Colors.black54),
                ),
              ),
            )
          else
            SizedBox(
              key: const ValueKey('overview-trend-chart'),
              height: 120,
              width: double.infinity,
              child: CustomPaint(painter: _TrendPainter(points: points)),
            ),
          const SizedBox(height: 6),
          Text(
            note,
            key: const ValueKey('overview-trend-note'),
            style: const TextStyle(fontSize: 11, color: Colors.black54),
          ),
          if (points.isNotEmpty) ...[
            const SizedBox(height: 4),
            Row(
              children: [
                Text(
                  _formatHour(points.first.hourStart),
                  style: const TextStyle(fontSize: 10, color: Colors.black38),
                ),
                const Spacer(),
                Text(
                  _formatHour(points.last.hourStart),
                  style: const TextStyle(fontSize: 10, color: Colors.black38),
                ),
              ],
            ),
          ],
        ],
      ),
    );
  }

  Widget _legendDot(Color color, String label) {
    return Row(
      children: [
        Container(
          width: 8,
          height: 8,
          decoration: BoxDecoration(color: color, shape: BoxShape.circle),
        ),
        const SizedBox(width: 4),
        Text(
          label,
          style: const TextStyle(fontSize: 11, color: Colors.black54),
        ),
      ],
    );
  }

  Widget _buildAbnormalSection() {
    final items = _stats.abnormalDevices;
    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: Colors.black12),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              const Expanded(
                child: Text(
                  '异常设备',
                  style: TextStyle(fontSize: 13, fontWeight: FontWeight.w600),
                ),
              ),
              Text(
                '${items.length}',
                style: const TextStyle(fontSize: 12, color: Colors.black45),
              ),
            ],
          ),
          const SizedBox(height: 6),
          if (items.isEmpty)
            const Padding(
              padding: EdgeInsets.symmetric(vertical: 10),
              child: Text(
                '没有需要处理的设备',
                style: TextStyle(fontSize: 12, color: Colors.black54),
              ),
            )
          else
            ...items.map(_buildAbnormalTile),
        ],
      ),
    );
  }

  Widget _buildAbnormalTile(AbnormalDevice device) {
    final hasAlert = device.openAlertCount > 0;
    final offline = device.status != 'online';
    final color = hasAlert ? Colors.red : Colors.orange;
    return Material(
      color: Colors.transparent,
      child: ListTile(
        key: ValueKey('overview-abnormal-${device.deviceId}'),
        contentPadding: EdgeInsets.zero,
        dense: true,
        leading: Icon(
          hasAlert ? Icons.error_outline : Icons.cloud_off,
          color: color,
          size: 18,
        ),
        title: Text(
          device.name.isEmpty ? device.deviceId : device.name,
          style: const TextStyle(fontSize: 13),
        ),
        subtitle: Text(
          [
            device.deviceId,
            if (offline) '离线',
            if (hasAlert) '未处理告警 ${device.openAlertCount}',
          ].join(' · '),
          style: const TextStyle(fontSize: 11),
        ),
        trailing: const Icon(Icons.chevron_right, size: 18),
        onTap: () => _showDeviceDetail(device),
      ),
    );
  }

  Widget _buildEmptyState() {
    return Container(
      key: const ValueKey('overview-empty'),
      padding: const EdgeInsets.symmetric(vertical: 40, horizontal: 16),
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: Colors.black12),
      ),
      child: const Column(
        children: [
          Icon(Icons.inbox_outlined, size: 42, color: Colors.black26),
          SizedBox(height: 10),
          Text(
            '暂无设备数据',
            style: TextStyle(fontSize: 14, fontWeight: FontWeight.w600),
          ),
          SizedBox(height: 6),
          Text(
            '先在 Web 后台注册设备，或到「工具」启动设备模拟器上报真实数据。',
            textAlign: TextAlign.center,
            style: TextStyle(fontSize: 12, color: Colors.black54),
          ),
        ],
      ),
    );
  }

  void _showDeviceDetail(AbnormalDevice device) {
    final session = _session;
    showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      builder: (_) => _DeviceDetailSheet(
        device: device,
        loadReading: session == null
            ? null
            : () async {
                final client = _createApiClient(_baseUrl);
                try {
                  final readings = await client.fetchLatestReadings(
                    token: session.token,
                    deviceIds: <String>[device.deviceId],
                  );
                  return readings.isEmpty ? null : readings.first;
                } catch (_) {
                  return null;
                } finally {
                  client.close();
                }
              },
        onOpenBoard: widget.onOpenDeviceInBoard == null
            ? null
            : () {
                Navigator.of(context).maybePop();
                widget.onOpenDeviceInBoard!(device.deviceId);
              },
      ),
    );
  }
}

String _formatDateTime(DateTime time) {
  final local = time.toLocal();
  String two(int value) => value.toString().padLeft(2, '0');
  return '${local.year}-${two(local.month)}-${two(local.day)} '
      '${two(local.hour)}:${two(local.minute)}';
}

String _formatHour(DateTime time) {
  final local = time.toLocal();
  String two(int value) => value.toString().padLeft(2, '0');
  return '${two(local.month)}-${two(local.day)} ${two(local.hour)}:00';
}

/// 轻量趋势图：不引入第三方图表库，用 CustomPainter 直接绘制双柱，
/// 避免为一张 24 点小图增加包体与首帧解析成本。
class _TrendPainter extends CustomPainter {
  _TrendPainter({required this.points});

  final List<OverviewTrendPoint> points;

  @override
  void paint(Canvas canvas, Size size) {
    if (points.isEmpty) {
      return;
    }
    final maxValue = points.fold<int>(
      0,
      (max, p) => math.max(max, math.max(p.reports, p.alerts)),
    );
    if (maxValue <= 0) {
      return;
    }
    final baseline = size.height - 12;
    final chartHeight = size.height - 18;
    final slot = size.width / points.length;
    final barWidth = math.max(1.5, slot * 0.28);

    final reportPaint = Paint()..color = Colors.blue.withValues(alpha: 0.75);
    final alertPaint = Paint()..color = Colors.orange.withValues(alpha: 0.85);
    final baselinePaint = Paint()
      ..color = Colors.black12
      ..strokeWidth = 1;

    canvas.drawLine(
      Offset(0, baseline),
      Offset(size.width, baseline),
      baselinePaint,
    );

    for (var i = 0; i < points.length; i += 1) {
      final point = points[i];
      final center = slot * i + slot / 2;
      final reportHeight = chartHeight * point.reports / maxValue;
      final alertHeight = chartHeight * point.alerts / maxValue;
      if (point.reports > 0) {
        canvas.drawRRect(
          RRect.fromRectAndRadius(
            Rect.fromLTWH(
              center - barWidth - 1,
              baseline - reportHeight,
              barWidth,
              reportHeight,
            ),
            const Radius.circular(2),
          ),
          reportPaint,
        );
      }
      if (point.alerts > 0) {
        canvas.drawRRect(
          RRect.fromRectAndRadius(
            Rect.fromLTWH(
              center + 1,
              baseline - alertHeight,
              barWidth,
              alertHeight,
            ),
            const Radius.circular(2),
          ),
          alertPaint,
        );
      }
    }
  }

  @override
  bool shouldRepaint(covariant _TrendPainter oldDelegate) {
    return oldDelegate.points != points;
  }
}

/// 异常设备详情：只读展示，提供跳转到看板并聚焦该设备的入口。
class _DeviceDetailSheet extends StatefulWidget {
  const _DeviceDetailSheet({
    required this.device,
    required this.loadReading,
    this.onOpenBoard,
  });

  final AbnormalDevice device;
  final Future<SensorReading?> Function()? loadReading;
  final VoidCallback? onOpenBoard;

  @override
  State<_DeviceDetailSheet> createState() => _DeviceDetailSheetState();
}

class _DeviceDetailSheetState extends State<_DeviceDetailSheet> {
  bool _loading = true;
  SensorReading? _reading;

  @override
  void initState() {
    super.initState();
    _load();
  }

  Future<void> _load() async {
    final loader = widget.loadReading;
    if (loader == null) {
      if (mounted) {
        setState(() => _loading = false);
      }
      return;
    }
    final reading = await loader();
    if (!mounted) {
      return;
    }
    setState(() {
      _reading = reading;
      _loading = false;
    });
  }

  @override
  Widget build(BuildContext context) {
    final device = widget.device;
    final reading = _reading;
    return SafeArea(
      child: Padding(
        padding: const EdgeInsets.fromLTRB(16, 16, 16, 24),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Row(
              children: [
                Expanded(
                  child: Text(
                    device.name.isEmpty ? device.deviceId : device.name,
                    style: const TextStyle(
                      fontSize: 16,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                ),
                IconButton(
                  tooltip: '关闭',
                  onPressed: () => Navigator.of(context).maybePop(),
                  icon: const Icon(Icons.close),
                ),
              ],
            ),
            Text(
              '${device.deviceId} · ${device.status} · '
              '${device.reason == 'alert+offline'
                  ? '离线且有未处理告警'
                  : device.reason == 'alert'
                  ? '有未处理告警'
                  : '离线'}'
              '${device.openAlertCount > 0 ? ' · 未处理告警 ${device.openAlertCount}' : ''}',
              style: const TextStyle(fontSize: 12, color: Colors.black54),
            ),
            const SizedBox(height: 14),
            if (_loading)
              const Center(
                child: Padding(
                  padding: EdgeInsets.all(12),
                  child: CircularProgressIndicator(),
                ),
              )
            else if (reading == null)
              const Text(
                '该设备暂无最新上报数据',
                style: TextStyle(fontSize: 12, color: Colors.black54),
              )
            else
              Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  const Text(
                    '最新上报',
                    style: TextStyle(fontSize: 13, fontWeight: FontWeight.w600),
                  ),
                  const SizedBox(height: 8),
                  Wrap(
                    spacing: 12,
                    runSpacing: 8,
                    children: [
                      _metric('温度', '${reading.temperature} °C'),
                      _metric('湿度', '${reading.humidity} %'),
                      _metric('电压', '${reading.voltage} V'),
                      _metric('电量', '${reading.battery} %'),
                      _metric('信号', '${reading.rssi} dBm'),
                    ],
                  ),
                  const SizedBox(height: 8),
                  Text(
                    reading.ts == null
                        ? '上报时间未知'
                        : '上报时间 ${_formatDateTime(reading.ts!)}',
                    style: const TextStyle(fontSize: 11, color: Colors.black45),
                  ),
                ],
              ),
            if (widget.onOpenBoard != null) ...[
              const SizedBox(height: 16),
              SizedBox(
                width: double.infinity,
                child: FilledButton.tonalIcon(
                  onPressed: widget.onOpenBoard,
                  icon: const Icon(Icons.dashboard_outlined, size: 18),
                  label: const Text('在移动看板查看该设备'),
                ),
              ),
            ],
          ],
        ),
      ),
    );
  }

  Widget _metric(String label, String value) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 6),
      decoration: BoxDecoration(
        color: Colors.black.withValues(alpha: 0.04),
        borderRadius: BorderRadius.circular(8),
      ),
      child: Text('$label $value', style: const TextStyle(fontSize: 12)),
    );
  }
}
