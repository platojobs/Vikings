import 'dart:async';
import 'dart:math' as math;

import 'package:flutter/material.dart';

import '../config/api_config.dart';
import '../models/mobile_api_models.dart';
import '../models/realtime_payload.dart';
import '../services/api_client.dart';
import '../services/app_storage.dart';
import '../services/board_realtime_service.dart';
import '../services/realtime_transport.dart';
import 'login_page.dart';

/// 按网关地址创建 REST 客户端；测试可注入 fake。
typedef AlertApiClientFactory = ApiClient Function(String baseUrl);

/// 按实时地址创建 MQTT 服务；测试可注入假服务。
typedef AlertRealtimeFactory = BoardRealtimeService Function(String url);

/// 告警中心：HomeShell 的第三个 tab。
///
/// 能力：
/// - 按级别 / 状态 / 设备筛选，滚动分页（加载中 / 没有更多 / 空态）；
/// - 多选后一次请求批量确认（`POST /api/alerts/bulk-ack`），部分失败可见；
/// - 实时新告警插入列表顶部，未读角标在 tab 上展示（进入页面清零）；
/// - viewer 只读、operator/admin 可确认；403 禁用本轮写操作，401 回登录页；
/// - 实时断开时切 REST 轮询补齐，不依赖单条事件不漏告警。
class AlertCenterPage extends StatefulWidget {
  const AlertCenterPage({
    super.key,
    this.active = true,
    this.storage,
    this.apiClientFactory,
    this.realtimeServiceFactory,
    this.onUnreadChanged,
  });

  /// 页面是否处于前台 tab；仅用于「进入即清零」与切回时补齐，
  /// 实时订阅在 App 前台保持，以保证 tab 角标在后台 tab 上也能累加。
  final bool active;

  final AppStorage? storage;
  final AlertApiClientFactory? apiClientFactory;
  final AlertRealtimeFactory? realtimeServiceFactory;

  /// 未读告警数变化时回调 HomeShell 更新 tab 角标。
  final void Function(int unread)? onUnreadChanged;

  @override
  State<AlertCenterPage> createState() => _AlertCenterPageState();
}

class _AlertCenterPageState extends State<AlertCenterPage>
    with WidgetsBindingObserver {
  static const int _pageSize = 20;
  static const Duration _pollInterval = Duration(seconds: 15);

  /// P2-1（v1.21.0）：规则引擎实际只产出 warning / critical，
  /// 对外口径收窄为两级，不再展示永远为空的 info / notice。
  static const List<String> _levelOptions = <String>['warning', 'critical'];

  late final AppStorage _storage;
  late final AlertApiClientFactory _createApiClient;
  late final AlertRealtimeFactory _createRealtime;

  AuthSession? _session;
  String _baseUrl = ApiConfig.defaultBaseUrl;
  String _realtimeUrlOverride = '';
  String _mqttUsername = '';
  String _mqttPassword = '';
  bool _realtimeCredentialLoading = false;

  bool _booting = true;
  bool _loading = false;
  bool _acking = false;
  String? _error;
  String? _notice;

  /// 筛选条件；空串代表「全部」。
  String _level = '';
  String _status = 'open';
  String _deviceId = '';

  List<AlertItem> _alerts = const <AlertItem>[];
  List<DeviceSummary> _devices = const <DeviceSummary>[];
  final Set<String> _selected = <String>{};

  int _page = 1;
  int _total = 0;
  bool _hasMore = false;
  bool _loadingMore = false;

  /// 本地未读计数：初始为服务端 open 总数，进入页面清零，实时新增 +1。
  int _unread = 0;

  /// 用于去重实时事件与刷新结果，避免同一条告警重复累加角标。
  final Set<String> _seenAlertIds = <String>{};

  /// 服务端 403 后记录原因，直接禁用写操作，避免反复失败。
  String? _writeBlockedReason;

  final ScrollController _scrollController = ScrollController();
  int _requestSeq = 0;

  BoardRealtimeService? _realtime;
  StreamSubscription<RealtimeEvent>? _eventSub;
  Timer? _pollTimer;
  bool _appVisible = true;

  @override
  void initState() {
    super.initState();
    _storage = widget.storage ?? AppStorage();
    _createApiClient =
        widget.apiClientFactory ??
        (String baseUrl) => ApiClient(baseUrl: baseUrl);
    _createRealtime =
        widget.realtimeServiceFactory ??
        (String url) => BoardRealtimeService(
          url: url,
          transportFactory: (uri, clientId) => MqttWebSocketTransport(
            url: uri,
            clientId: clientId,
            username: _mqttUsername,
            password: _mqttPassword,
          ),
        );
    WidgetsBinding.instance.addObserver(this);
    _scrollController.addListener(_onScroll);
    _bootstrap();
  }

  @override
  void didUpdateWidget(covariant AlertCenterPage oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.active != widget.active) {
      if (widget.active) {
        // 进入页面即清零未读；HomeShell 同步把 tab 角标归零，
        // 这里不再回调，避免在父级 build 期间触发 setState。
        _unread = 0;
        if (_session != null) {
          unawaited(_reload(silent: true));
        }
      }
      _syncRealtimeLifecycle();
    }
  }

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    final visible = state == AppLifecycleState.resumed;
    if (visible == _appVisible) {
      return;
    }
    _appVisible = visible;
    _syncRealtimeLifecycle();
  }

  @override
  void dispose() {
    WidgetsBinding.instance.removeObserver(this);
    _scrollController.removeListener(_onScroll);
    _scrollController.dispose();
    _pollTimer?.cancel();
    _eventSub?.cancel();
    unawaited(_realtime?.dispose());
    _realtime = null;
    super.dispose();
  }

  // ---------------- 初始化与登录 ----------------

  Future<void> _bootstrap() async {
    String baseUrl = ApiConfig.defaultBaseUrl;
    String? realtimeUrl;
    AuthSession? session;
    try {
      baseUrl = await _storage.loadBaseUrl() ?? ApiConfig.defaultBaseUrl;
      realtimeUrl = await _storage.loadRealtimeUrl();
      session = await _storage.loadSession();
    } catch (_) {
      // 本地存储不可用时按未登录处理，页面仍可展示登录入口
    }
    if (!mounted) {
      return;
    }
    setState(() {
      _baseUrl = baseUrl;
      _realtimeUrlOverride = realtimeUrl ?? '';
      _session = session;
      _booting = false;
    });
    if (session != null) {
      _syncRealtimeLifecycle();
      await _reload();
    }
  }

  void _onLoggedIn(AuthSession session, String baseUrl) {
    setState(() {
      _session = session;
      _baseUrl = baseUrl;
      _notice = null;
      _error = null;
    });
    _syncRealtimeLifecycle();
    unawaited(_reload());
  }

  /// 401：停止实时与轮询，回到登录页并保留可读提示。
  void _handleUnauthorized() {
    _stopRealtime();
    _stopPolling();
    if (!mounted) {
      return;
    }
    setState(() {
      _session = null;
      _notice = '登录已失效，请重新登录';
      _selected.clear();
      _unread = 0;
    });
    _notifyUnread();
  }

  ApiClient _client() => _createApiClient(_baseUrl);

  // ---------------- 数据加载 ----------------

  Future<void> _reload({bool silent = false}) async {
    final session = _session;
    if (session == null) {
      return;
    }
    final requestId = ++_requestSeq;
    if (!silent && mounted) {
      setState(() {
        _loading = true;
        _error = null;
      });
    }
    final client = _client();
    try {
      final page = await client.fetchAlerts(
        token: session.token,
        page: 1,
        pageSize: _pageSize,
        level: _level,
        status: _status,
        deviceId: _deviceId,
      );
      AlertSummary summary = AlertSummary.empty;
      try {
        summary = await client.fetchAlertSummary(token: session.token);
      } on ApiException catch (_) {
        // 汇总失败不影响列表；角标沿用实时计数
      }
      if (_devices.isEmpty) {
        try {
          final devices = await client.fetchDevices(
            token: session.token,
            page: 1,
            pageSize: 200,
          );
          _devices = devices.items;
        } on ApiException catch (_) {
          // 设备筛选是辅助能力，拉取失败不阻塞告警列表
        }
      }
      if (!mounted || requestId != _requestSeq) {
        return;
      }
      setState(() {
        _alerts = page.items;
        _page = page.page <= 0 ? 1 : page.page;
        _total = page.total;
        _hasMore = _page * _pageSize < _total;
        _loading = false;
        _loadingMore = false;
        _seenAlertIds.addAll(page.items.map((item) => item.alertId));
        // 页面不可见时以服务端 open 总数为基线补齐，页面可见时保持「进入即清零」口径
        if (!widget.active) {
          _unread = math.max(_unread, summary.open);
        }
      });
      _notifyUnread();
    } on ApiException catch (error) {
      if (!mounted || requestId != _requestSeq) {
        return;
      }
      if (error.kind == ApiErrorKind.unauthorized) {
        _handleUnauthorized();
        return;
      }
      setState(() {
        _loading = false;
        _loadingMore = false;
        _error = error.userMessage;
      });
    } finally {
      client.close();
      if (mounted && requestId == _requestSeq && _loading) {
        setState(() => _loading = false);
      }
    }
  }

  void _onScroll() {
    if (!_scrollController.hasClients) {
      return;
    }
    if (_scrollController.position.extentAfter < 240) {
      unawaited(_loadMore());
    }
  }

  Future<void> _loadMore() async {
    final session = _session;
    if (session == null || _loading || _loadingMore || !_hasMore) {
      return;
    }
    final requestId = _requestSeq;
    setState(() => _loadingMore = true);
    final client = _client();
    try {
      final page = await client.fetchAlerts(
        token: session.token,
        page: _page + 1,
        pageSize: _pageSize,
        level: _level,
        status: _status,
        deviceId: _deviceId,
      );
      if (!mounted || requestId != _requestSeq) {
        return;
      }
      setState(() {
        final known = _alerts.map((item) => item.alertId).toSet();
        _alerts = <AlertItem>[
          ..._alerts,
          ...page.items.where((item) => !known.contains(item.alertId)),
        ];
        _page += 1;
        _total = page.total;
        _hasMore = _page * _pageSize < _total;
        _seenAlertIds.addAll(page.items.map((item) => item.alertId));
        _loadingMore = false;
      });
    } on ApiException catch (error) {
      if (!mounted || requestId != _requestSeq) {
        return;
      }
      if (error.kind == ApiErrorKind.unauthorized) {
        _handleUnauthorized();
        return;
      }
      setState(() {
        _loadingMore = false;
        _notice = error.userMessage;
      });
    } finally {
      client.close();
    }
  }

  void _applyLevel(String level) {
    if (_level == level) {
      return;
    }
    setState(() {
      _level = level;
      _selected.clear();
      _requestSeq += 1;
      _alerts = const <AlertItem>[];
      _loading = false;
      _loadingMore = false;
    });
    unawaited(_reload());
  }

  void _applyStatus(String status) {
    if (_status == status) {
      return;
    }
    setState(() {
      _status = status;
      _selected.clear();
      _requestSeq += 1;
      _alerts = const <AlertItem>[];
      _loading = false;
      _loadingMore = false;
    });
    unawaited(_reload());
  }

  void _applyDevice(String deviceId) {
    if (_deviceId == deviceId) {
      return;
    }
    setState(() {
      _deviceId = deviceId;
      _selected.clear();
      _requestSeq += 1;
      _alerts = const <AlertItem>[];
      _loading = false;
      _loadingMore = false;
    });
    unawaited(_reload());
  }

  // ---------------- 实时链路 ----------------

  void _syncRealtimeLifecycle() {
    final shouldRun = _appVisible && _session != null;
    if (!shouldRun) {
      _stopRealtime();
      _stopPolling();
      return;
    }
    if (_realtime == null && !_realtimeCredentialLoading) {
      unawaited(_startRealtime());
    }
  }

  String get _effectiveRealtimeUrl => _realtimeUrlOverride.isNotEmpty
      ? _realtimeUrlOverride
      : ApiConfig.realtimeUrl(_baseUrl);

  Future<void> _startRealtime() async {
    final session = _session;
    if (session == null) {
      return;
    }
    _stopRealtime();
    final url = _effectiveRealtimeUrl;
    if (url.isEmpty) {
      return;
    }

    // 自定义注入的实时服务（测试/离线）由调用方负责凭证；默认实现必须
    // 先从网关换取只读凭证，Broker 已关闭匿名，禁止匿名降级。
    if (widget.realtimeServiceFactory == null) {
      _realtimeCredentialLoading = true;
      final credential = await _fetchRealtimeCredential(session);
      _realtimeCredentialLoading = false;
      if (!mounted || _session == null) {
        return;
      }
      if (credential == null) {
        _startPolling();
        return;
      }
      _mqttUsername = credential.username;
      _mqttPassword = credential.password;
    }

    if (_realtime != null) {
      return;
    }
    final service = _createRealtime(url);
    _realtime = service;
    _eventSub = service.events.listen(_onRealtimeEvent);
    service.state.addListener(_onRealtimeStateChanged);
    service.start();
  }

  /// 用登录 token 换取 9001 只读凭证；失败时返回 null 并提示，降级为 REST 轮询。
  Future<MqttClientCredential?> _fetchRealtimeCredential(
    AuthSession session,
  ) async {
    final api = _createApiClient(_baseUrl);
    try {
      return await api.fetchMqttCredentials(
        token: session.token,
        client: 'mobile',
      );
    } catch (error) {
      if (!mounted) {
        return null;
      }
      setState(() {
        _notice = '实时凭证获取失败，已切换为 REST 轮询：$error';
      });
      return null;
    } finally {
      api.close();
    }
  }

  void _stopRealtime() {
    _eventSub?.cancel();
    _eventSub = null;
    final service = _realtime;
    _realtime = null;
    if (service != null) {
      service.state.removeListener(_onRealtimeStateChanged);
      unawaited(service.dispose());
    }
  }

  void _onRealtimeStateChanged() {
    final state = _realtime?.state.value;
    if (state == null) {
      return;
    }
    if (state == RealtimeState.connected) {
      _stopPolling();
    } else if (state.requiresPollingFallback &&
        _appVisible &&
        _session != null) {
      _startPolling();
    }
  }

  /// 实时不可用时轮询第 1 页 + 汇总；即使页面不可见也继续，
  /// 用于保证 tab 角标不因断开而漏计。
  void _startPolling() {
    if (_pollTimer != null || !mounted) {
      return;
    }
    _pollTimer = Timer.periodic(_pollInterval, (_) {
      if (!_loading && _session != null) {
        unawaited(_reload(silent: true));
      }
    });
  }

  void _stopPolling() {
    _pollTimer?.cancel();
    _pollTimer = null;
  }

  void _onRealtimeEvent(RealtimeEvent event) {
    if (!mounted) {
      return;
    }
    if (event is! AlertEvent) {
      return;
    }
    final alert = event.alert;
    if (!_seenAlertIds.add(alert.alertId)) {
      return;
    }
    final matches = _matchesCurrentFilter(alert);
    setState(() {
      _unread += 1;
      if (matches) {
        _alerts = <AlertItem>[alert, ..._alerts];
        _total += 1;
        _hasMore = _page * _pageSize < _total;
      }
    });
    _notifyUnread();
  }

  bool _matchesCurrentFilter(AlertItem alert) {
    if (_level.isNotEmpty && alert.level != _level) {
      return false;
    }
    if (_status.isNotEmpty && alert.status != _status) {
      return false;
    }
    if (_deviceId.isNotEmpty && alert.deviceId != _deviceId) {
      return false;
    }
    return true;
  }

  void _notifyUnread() {
    widget.onUnreadChanged?.call(_unread);
  }

  // ---------------- 批量确认 ----------------

  bool get _canWrite {
    final role = _session?.role;
    final allowedRole = role == 'admin' || role == 'operator';
    return allowedRole && _writeBlockedReason == null;
  }

  void _toggleSelected(String alertId, bool selected) {
    setState(() {
      if (selected) {
        _selected.add(alertId);
      } else {
        _selected.remove(alertId);
      }
    });
  }

  Future<void> _confirmBulkAck() async {
    final session = _session;
    if (session == null || _selected.isEmpty || _acking || !_canWrite) {
      return;
    }
    final note = await showDialog<String>(
      context: context,
      builder: (context) => _BulkAckDialog(count: _selected.length),
    );
    if (note == null) {
      return;
    }
    final ids = _selected.toList(growable: false);
    setState(() {
      _acking = true;
      _notice = null;
    });
    final client = _client();
    try {
      final result = await client.bulkAckAlerts(
        token: session.token,
        alertIds: ids,
        note: note.isEmpty ? null : note,
      );
      if (!mounted) {
        return;
      }
      final acked = result.acked.toSet();
      setState(() {
        _acking = false;
        _selected.clear();
        _alerts = _alerts
            .where((item) => !acked.contains(item.alertId))
            .toList(growable: false);
        _total = math.max(0, _total - result.acked.length);
        _hasMore = _page * _pageSize < _total;
        _unread = math.max(0, _unread - result.acked.length);
        _notice = _formatAckResult(result);
      });
      _notifyUnread();
    } on ApiException catch (error) {
      if (!mounted) {
        return;
      }
      if (error.kind == ApiErrorKind.unauthorized) {
        setState(() => _acking = false);
        _handleUnauthorized();
        return;
      }
      setState(() {
        _acking = false;
        if (error.kind == ApiErrorKind.forbidden) {
          _writeBlockedReason = error.userMessage;
        }
        _notice = error.kind == ApiErrorKind.forbidden
            ? '${error.userMessage}，本轮写操作已禁用'
            : error.userMessage;
      });
    } finally {
      client.close();
    }
  }

  String _formatAckResult(BulkAckResult result) {
    final buffer = StringBuffer('已确认 ${result.acked.length} 条告警');
    if (result.alreadyAcked.isNotEmpty) {
      buffer.write('；${result.alreadyAcked.length} 条此前已确认');
    }
    if (result.notFound.isNotEmpty) {
      buffer.write('；${result.notFound.length} 条不存在');
    }
    if (result.acked.isEmpty) {
      buffer.write('；本次没有状态变化');
    }
    return buffer.toString();
  }

  // ---------------- 视图 ----------------

  @override
  Widget build(BuildContext context) {
    if (_booting) {
      return const Center(child: CircularProgressIndicator());
    }
    final session = _session;
    if (session == null) {
      return LoginPage(
        initialBaseUrl: _baseUrl,
        notice: _notice,
        storage: _storage,
        apiClientFactory: _createApiClient,
        onLoggedIn: _onLoggedIn,
      );
    }
    return Scaffold(
      appBar: AppBar(
        title: const Text('告警中心'),
        centerTitle: false,
        actions: [
          IconButton(
            tooltip: '刷新',
            onPressed: _loading ? null : () => unawaited(_reload()),
            icon: const Icon(Icons.refresh),
          ),
        ],
      ),
      body: SafeArea(
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.stretch,
          children: [
            _buildFilterBar(),
            if (_notice != null) _buildBanner(_notice!, Colors.blue),
            if (_error != null) _buildBanner(_error!, Colors.red),
            if (!_canWrite && _session?.role == 'viewer')
              _buildBanner('当前账号为只读（viewer），批量确认已禁用。', Colors.orange),
            if (_session?.role != 'viewer' && _writeBlockedReason != null)
              _buildBanner('$_writeBlockedReason，本轮写操作已禁用。', Colors.orange),
            Expanded(child: _buildAlertList()),
            if (_selected.isNotEmpty) _buildSelectionBar(),
          ],
        ),
      ),
    );
  }

  Widget _buildFilterBar() {
    final theme = Theme.of(context);
    return Container(
      padding: const EdgeInsets.fromLTRB(12, 8, 12, 4),
      decoration: BoxDecoration(
        color: theme.colorScheme.surfaceContainerLowest,
        border: Border(
          bottom: BorderSide(color: theme.dividerColor.withValues(alpha: 0.4)),
        ),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          SingleChildScrollView(
            scrollDirection: Axis.horizontal,
            child: Row(
              children: [
                _levelChip('全部级别', ''),
                ..._levelOptions.map(
                  (level) => _levelChip(_levelLabel(level), level),
                ),
              ],
            ),
          ),
          const SizedBox(height: 4),
          SingleChildScrollView(
            scrollDirection: Axis.horizontal,
            child: Row(
              children: [
                _statusChip('全部状态', ''),
                _statusChip('未处理 open', 'open'),
                _statusChip('已确认 acked', 'acked'),
              ],
            ),
          ),
          Padding(
            padding: const EdgeInsets.symmetric(vertical: 6),
            child: DropdownButtonFormField<String>(
              key: const ValueKey('device-filter'),
              initialValue: _deviceId,
              isDense: true,
              decoration: const InputDecoration(
                labelText: '设备筛选',
                border: OutlineInputBorder(),
                contentPadding: EdgeInsets.symmetric(
                  horizontal: 12,
                  vertical: 8,
                ),
              ),
              items: [
                const DropdownMenuItem(value: '', child: Text('全部设备')),
                ..._devices.map(
                  (device) => DropdownMenuItem(
                    value: device.deviceId,
                    child: Text('${device.name}（${device.deviceId}）'),
                  ),
                ),
              ],
              onChanged: (value) => _applyDevice(value ?? ''),
            ),
          ),
        ],
      ),
    );
  }

  Widget _levelChip(String label, String value) {
    return Padding(
      padding: const EdgeInsets.only(right: 8),
      child: ChoiceChip(
        key: ValueKey('level-${value.isEmpty ? 'all' : value}'),
        label: Text(label),
        selected: _level == value,
        onSelected: (_) => _applyLevel(value),
      ),
    );
  }

  Widget _statusChip(String label, String value) {
    return Padding(
      padding: const EdgeInsets.only(right: 8),
      child: ChoiceChip(
        key: ValueKey('status-${value.isEmpty ? 'all' : value}'),
        label: Text(label),
        selected: _status == value,
        onSelected: (_) => _applyStatus(value),
      ),
    );
  }

  String _levelLabel(String level) => switch (level) {
    'warning' => 'warning 警告',
    'critical' => 'critical 严重',
    _ => level,
  };

  Widget _buildBanner(String text, Color color) {
    return Container(
      margin: const EdgeInsets.fromLTRB(12, 8, 12, 0),
      padding: const EdgeInsets.all(10),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.08),
        borderRadius: BorderRadius.circular(8),
        border: Border.all(color: color.withValues(alpha: 0.3)),
      ),
      child: Text(text, style: TextStyle(color: color, fontSize: 12)),
    );
  }

  Widget _buildAlertList() {
    if (_loading) {
      return const Center(child: Text('加载中…'));
    }
    if (_alerts.isEmpty) {
      return Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              const Icon(
                Icons.notifications_off_outlined,
                size: 36,
                color: Colors.black38,
              ),
              const SizedBox(height: 8),
              Text(_status == 'open' ? '当前筛选下没有未处理告警' : '当前筛选下没有告警'),
              const SizedBox(height: 4),
              const Text(
                '保持实时连接，新告警会自动出现在这里。',
                style: TextStyle(fontSize: 12, color: Colors.black54),
              ),
            ],
          ),
        ),
      );
    }
    return ListView.builder(
      controller: _scrollController,
      padding: const EdgeInsets.fromLTRB(12, 8, 12, 16),
      itemCount: _alerts.length + 1,
      itemBuilder: (context, index) {
        if (index == _alerts.length) {
          return _buildFooter();
        }
        return _buildAlertRow(_alerts[index]);
      },
    );
  }

  Widget _buildAlertRow(AlertItem alert) {
    final selectable = alert.status == 'open';
    final selected = _selected.contains(alert.alertId);
    final color = _levelColor(alert.level);
    final device = _deviceFor(alert.deviceId);
    final deviceLabel = device == null
        ? alert.deviceId
        : '${device.name}（${alert.deviceId}）';
    return Card(
      key: ValueKey('alert-row-${alert.alertId}'),
      margin: const EdgeInsets.only(bottom: 8),
      child: Padding(
        padding: const EdgeInsets.fromLTRB(4, 4, 12, 8),
        child: Row(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Checkbox(
              key: ValueKey('select-${alert.alertId}'),
              value: selected,
              // viewer 也允许勾选查看，但底部确认按钮禁用并说明原因
              onChanged: selectable
                  ? (value) => _toggleSelected(alert.alertId, value ?? false)
                  : null,
            ),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Row(
                    children: [
                      Container(
                        padding: const EdgeInsets.symmetric(
                          horizontal: 6,
                          vertical: 2,
                        ),
                        decoration: BoxDecoration(
                          color: color.withValues(alpha: 0.12),
                          borderRadius: BorderRadius.circular(4),
                        ),
                        child: Text(
                          alert.level,
                          style: TextStyle(color: color, fontSize: 11),
                        ),
                      ),
                      const SizedBox(width: 6),
                      Expanded(
                        child: Text(
                          deviceLabel,
                          maxLines: 1,
                          overflow: TextOverflow.ellipsis,
                          style: const TextStyle(
                            fontWeight: FontWeight.w600,
                            fontSize: 13,
                          ),
                        ),
                      ),
                      Text(
                        alert.status == 'open' ? '未处理' : '已确认',
                        style: TextStyle(
                          fontSize: 11,
                          color: alert.status == 'open'
                              ? Colors.orange
                              : Colors.green,
                        ),
                      ),
                    ],
                  ),
                  const SizedBox(height: 4),
                  Text(
                    '${alert.metric}: ${_formatNumber(alert.value)}'
                    '（阈值 ${_formatNumber(alert.threshold)}）',
                    style: const TextStyle(fontSize: 12),
                  ),
                  if (alert.message.isNotEmpty)
                    Text(
                      alert.message,
                      style: const TextStyle(
                        fontSize: 12,
                        color: Colors.black54,
                      ),
                    ),
                  const SizedBox(height: 2),
                  Text(
                    _formatTime(alert.createdAt),
                    style: const TextStyle(fontSize: 11, color: Colors.black45),
                  ),
                  if (alert.status == 'acked') ...[
                    const SizedBox(height: 4),
                    Text(
                      alert.ackedBy == null || alert.ackedBy!.isEmpty
                          ? '历史确认（无处置记录）'
                          : '确认人 ${alert.ackedBy}'
                                '${alert.ackedAt == null ? '' : ' · ${_formatTime(alert.ackedAt)}'}',
                      key: ValueKey('ack-meta-${alert.alertId}'),
                      style: const TextStyle(
                        fontSize: 11,
                        color: Colors.black54,
                      ),
                    ),
                    if ((alert.ackNote ?? '').isNotEmpty)
                      Padding(
                        padding: const EdgeInsets.only(top: 2),
                        child: Text(
                          '处置备注：${alert.ackNote}',
                          key: ValueKey('ack-note-${alert.alertId}'),
                          style: const TextStyle(
                            fontSize: 11,
                            color: Colors.black54,
                          ),
                        ),
                      ),
                  ],
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildFooter() {
    final theme = Theme.of(context);
    if (_loadingMore) {
      return const Padding(
        padding: EdgeInsets.all(12),
        child: Center(
          child: Row(
            mainAxisSize: MainAxisSize.min,
            children: [
              SizedBox(
                width: 14,
                height: 14,
                child: CircularProgressIndicator(strokeWidth: 2),
              ),
              SizedBox(width: 8),
              Text('加载中…', style: TextStyle(fontSize: 12)),
            ],
          ),
        ),
      );
    }
    if (_hasMore) {
      return Padding(
        padding: const EdgeInsets.all(12),
        child: Center(
          child: Text(
            '继续上拉加载更多（已显示 ${_alerts.length} / $_total）',
            key: const ValueKey('alert-footer-more'),
            style: TextStyle(fontSize: 12, color: theme.colorScheme.primary),
          ),
        ),
      );
    }
    return const Padding(
      padding: EdgeInsets.all(12),
      child: Center(
        child: Text(
          '没有更多了',
          key: ValueKey('alert-footer-end'),
          style: TextStyle(fontSize: 12, color: Colors.black45),
        ),
      ),
    );
  }

  Widget _buildSelectionBar() {
    return Material(
      elevation: 6,
      child: Padding(
        padding: const EdgeInsets.fromLTRB(12, 8, 12, 8),
        child: Row(
          children: [
            Expanded(
              child: Text(
                '已选择 ${_selected.length} 条',
                style: const TextStyle(fontSize: 13),
              ),
            ),
            if (!_canWrite && _session?.role == 'viewer')
              const Padding(
                padding: EdgeInsets.only(right: 8),
                child: Text(
                  '只读账号不可确认',
                  style: TextStyle(fontSize: 12, color: Colors.orange),
                ),
              ),
            FilledButton.icon(
              key: const ValueKey('bulk-ack-button'),
              onPressed: _canWrite && !_acking ? _confirmBulkAck : null,
              icon: _acking
                  ? const SizedBox(
                      width: 14,
                      height: 14,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    )
                  : const Icon(Icons.done_all, size: 18),
              label: Text(_acking ? '确认中…' : '批量确认'),
            ),
          ],
        ),
      ),
    );
  }

  DeviceSummary? _deviceFor(String deviceId) {
    for (final device in _devices) {
      if (device.deviceId == deviceId) {
        return device;
      }
    }
    return null;
  }

  Color _levelColor(String level) => switch (level) {
    'critical' => Colors.red,
    'warning' => Colors.orange,
    _ => Colors.grey,
  };

  String _formatNumber(double value) {
    if (value == value.roundToDouble()) {
      return value.toStringAsFixed(0);
    }
    return value.toStringAsFixed(1);
  }

  String _formatTime(DateTime? time) {
    if (time == null) {
      return '时间未知';
    }
    final local = time.toLocal();
    String two(int value) => value.toString().padLeft(2, '0');
    return '${local.year}-${two(local.month)}-${two(local.day)} '
        '${two(local.hour)}:${two(local.minute)}:${two(local.second)}';
  }
}

/// 批量确认弹窗：备注可选，确认返回 trim 后的文本，取消返回 null。
/// 用 StatefulWidget 持有 TextEditingController，确保关闭动画结束前不提前 dispose。
class _BulkAckDialog extends StatefulWidget {
  const _BulkAckDialog({required this.count});

  final int count;

  @override
  State<_BulkAckDialog> createState() => _BulkAckDialogState();
}

class _BulkAckDialogState extends State<_BulkAckDialog> {
  final TextEditingController _noteController = TextEditingController();

  @override
  void dispose() {
    _noteController.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      title: const Text('批量确认告警'),
      content: SingleChildScrollView(
        child: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text('确认选中的 ${widget.count} 条告警？确认后状态变为 acked。'),
            const SizedBox(height: 12),
            TextField(
              key: const ValueKey('bulk-ack-note'),
              controller: _noteController,
              maxLength: 500,
              maxLines: 3,
              minLines: 1,
              decoration: const InputDecoration(
                labelText: '处置备注（可选）',
                hintText: '例如：已联系现场，重启设备后恢复',
                border: OutlineInputBorder(),
              ),
            ),
          ],
        ),
      ),
      actions: [
        TextButton(
          onPressed: () => Navigator.of(context).pop(),
          child: const Text('取消'),
        ),
        FilledButton(
          key: const ValueKey('bulk-ack-confirm'),
          onPressed: () =>
              Navigator.of(context).pop(_noteController.text.trim()),
          child: const Text('确认'),
        ),
      ],
    );
  }
}
