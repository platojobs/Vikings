import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
import 'package:flutter/services.dart';

import '../config/api_config.dart';
import '../models/command_request.dart';
import '../models/mobile_api_models.dart';
import '../models/realtime_payload.dart';
import '../services/api_client.dart';
import '../services/app_storage.dart';
import '../services/session_refresh.dart';
import '../services/board_realtime_service.dart';
import '../services/realtime_transport.dart';
import '../widgets/command_dialog.dart';
import '../widgets/online_badge.dart';
import '../widgets/sensor_card.dart';
import 'login_page.dart';

/// 移动看板：手机端展示网关数据（设备、实时值、告警、指令回执），
/// 并为 admin / operator 提供受限写操作（指令下发、告警确认）。
///
/// 设计约束：
/// - 首屏与手动刷新走既有 REST，实时增量走 MQTT over WebSocket（9001）；
/// - 写操作严格复用网关既有端点，不复制 RBAC：角色以登录会话为准，
///   服务端 403 时立即禁用写按钮并提示，不崩溃；
/// - 网关与 Web 零改动，订阅 Topic 与后台看板一致；
/// - 保留本地设备模拟器为默认入口，两套 MQTT 连接互不影响；
/// - 未登录、后端不可达、token 失效、实时断线均给出可读提示，不白屏、不崩溃。
class MobileBoardPage extends StatefulWidget {
  const MobileBoardPage({
    super.key,
    this.active = true,
    this.storage,
    this.apiClientFactory,
    this.realtimeServiceFactory,
  });

  /// 页面是否处于前台可见。切到设备模拟或 App 进入后台时置为 false，
  /// 用于断开实时连接、停止轮询省电。
  final bool active;

  /// 可选的存储与网络依赖，仅用于测试注入；为空时沿用生产默认实现。
  final AppStorage? storage;
  final BoardApiClientFactory? apiClientFactory;
  final BoardRealtimeFactory? realtimeServiceFactory;

  @override
  State<MobileBoardPage> createState() => _MobileBoardPageState();
}

/// 按网关地址创建 REST 客户端；测试可注入 fake，生产默认走真实网络。
typedef BoardApiClientFactory = ApiClient Function(String baseUrl);

/// 按实时地址创建 MQTT 服务；测试可注入假服务，生产默认走 WebSocket。
typedef BoardRealtimeFactory = BoardRealtimeService Function(String url);

class _MobileBoardPageState extends State<MobileBoardPage>
    with WidgetsBindingObserver {
  late final AppStorage _storage;
  late final SessionRefreshCoordinator _refresher;
  late final BoardApiClientFactory _createApiClient;
  late final BoardRealtimeFactory _createRealtime;

  AuthSession? _session;
  String _baseUrl = ApiConfig.defaultBaseUrl;
  String _mqttUsername = '';
  String _mqttPassword = '';
  bool _realtimeCredentialLoading = false;
  List<DeviceSummary> _devices = const <DeviceSummary>[];
  List<SensorReading> _readings = const <SensorReading>[];
  List<AlertItem> _alerts = const <AlertItem>[];
  bool _booting = true;
  bool _loading = false;
  String? _error;
  String? _notice;

  /// 实时通道收到的最新在线状态，优先于 REST 快照。
  final Map<String, String> _liveStatus = <String, String>{};

  /// 实时上报广播：详情弹层打开时订阅，新数据插到列表顶部。
  final StreamController<SensorReading> _reportStream =
      StreamController<SensorReading>.broadcast();

  BoardRealtimeService? _realtime;
  StreamSubscription<RealtimeEvent>? _eventSub;
  Timer? _pollTimer;

  /// 自定义实时地址；为空时按网关地址自动推导。
  String _realtimeUrlOverride = '';

  /// App 生命周期可见性（后台时断开实时）。
  bool _appVisible = true;

  /// 服务端返回 403 后记录原因，直接禁用写操作，避免反复失败。
  String? _writeBlockedReason;

  /// 正在确认的告警 ID，用于防重复点击与按钮 loading。
  String? _ackingAlertId;

  /// 顶部搜索框与状态筛选：只影响列表渲染，不影响实时事件落库。
  final TextEditingController _searchController = TextEditingController();
  Timer? _searchDebounce;

  /// 分组变更后与服务端对账的防抖定时器（连续改组只拉一次）。
  Timer? _groupSyncDebounce;
  String _keyword = '';
  _BoardStatusFilter _statusFilter = _BoardStatusFilter.all;

  /// 分组筛选：空字符串表示全部分组；「未分组」由网关统一归并。
  String _groupFilter = '';
  List<DeviceGroup> _groups = const <DeviceGroup>[];

  /// 服务端分页状态：只加载当前页，滚动到底追加下一页，不再受首屏 100 台上限约束。
  final ScrollController _scrollController = ScrollController();
  final GlobalKey _deviceFooterKey = GlobalKey();
  int _devicePage = 1;
  int _deviceTotal = 0;
  bool _deviceHasMore = false;
  bool _loadingMoreDevices = false;

  /// 未处理告警同样按页加载，`_alertTotal` 为服务端全量未处理数（不是已加载条数）。
  int _alertPage = 1;
  int _alertTotal = 0;
  bool _loadingMoreAlerts = false;

  /// 汇总卡的服务端全量统计，与设备分页口径解耦（只看第 1 页也不失真）。
  StatsOverview _stats = StatsOverview.empty;

  /// 请求序号：搜索 / 筛选快速切换时丢弃过期响应，避免旧页覆盖新页。
  int _requestSeq = 0;

  static const int _boardPageSize = 20;
  static const int _alertPageSize = 20;
  static const Duration _searchDebounceDelay = Duration(milliseconds: 300);

  static const Duration _pollInterval = Duration(seconds: 15);

  @override
  void initState() {
    super.initState();
    _storage = widget.storage ?? AppStorage();
    _refresher = SessionRefreshCoordinator(
      storage: _storage,
      baseUrl: () => _baseUrl,
      readSession: () => _session,
      onRefreshed: _onSessionRefreshed,
      // 测试注入的 fake 同时作为刷新客户端，避免单测触发真实网络
      clientBuilder: widget.apiClientFactory,
    );
    _createApiClient =
        widget.apiClientFactory ??
        (String baseUrl) =>
            ApiClient(baseUrl: baseUrl, refresher: _refresher);
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
  void didUpdateWidget(covariant MobileBoardPage oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.active != widget.active) {
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
    _searchDebounce?.cancel();
    _groupSyncDebounce?.cancel();
    _searchController.dispose();
    _scrollController.removeListener(_onScroll);
    _scrollController.dispose();
    _pollTimer?.cancel();
    _eventSub?.cancel();
    unawaited(_realtime?.dispose());
    _realtime = null;
    unawaited(_reportStream.close());
    super.dispose();
  }

  Future<void> _bootstrap() async {
    String baseUrl = ApiConfig.defaultBaseUrl;
    AuthSession? session;
    String? realtimeUrl;
    try {
      baseUrl = await _storage.loadBaseUrl() ?? ApiConfig.defaultBaseUrl;
      realtimeUrl = await _storage.loadRealtimeUrl();
      session = await _storage.loadSession();
    } catch (_) {
      // 本地存储不可用时退回默认地址与未登录态，保证页面仍可交互
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
      await _loadData();
      _syncRealtimeLifecycle();
    }
  }

  // ---------------- 实时链路 ----------------

  /// 页面可见且有登录态时才保持连接，否则断开省电。
  void _syncRealtimeLifecycle() {
    final shouldRun = widget.active && _appVisible && _session != null;
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

    // 默认实现先从网关换取 9001 只读凭证；注入的测试服务自行负责凭证
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

  /// 实时不可用时切到 REST 轮询兜底，恢复连接后自动停止轮询。
  void _onRealtimeStateChanged() {
    final state = _realtime?.state.value;
    if (state == null) {
      return;
    }
    if (state == RealtimeState.connected) {
      _stopPolling();
    } else if (state.requiresPollingFallback &&
        widget.active &&
        _appVisible &&
        _session != null) {
      _startPolling();
    }
  }

  void _startPolling() {
    if (_pollTimer != null || !mounted) {
      return;
    }
    _pollTimer = Timer.periodic(_pollInterval, (_) {
      if (!_loading) {
        _loadData(silent: true);
      }
    });
    setState(() {});
  }

  void _stopPolling() {
    if (_pollTimer == null) {
      return;
    }
    _pollTimer?.cancel();
    _pollTimer = null;
    if (mounted) {
      setState(() {});
    }
  }

  void _onRealtimeEvent(RealtimeEvent event) {
    if (!mounted) {
      return;
    }
    switch (event) {
      case AlertEvent(:final alert):
        if (_alerts.any((item) => item.alertId == alert.alertId)) {
          return;
        }
        setState(() {
          _alerts = <AlertItem>[alert, ..._alerts];
          // 计数以服务端全量为准，实时新增同样累加，不再被 50 条上限截断
          _alertTotal += 1;
          _stats = _stats.copyWith(alertOpen: _stats.alertOpen + 1);
          // 已加载设备立即更新角标；未加载设备由下次刷新补齐
          _devices = _devices
              .map(
                (device) => device.deviceId == alert.deviceId
                    ? device.copyWith(
                        openAlertCount: device.openAlertCount + 1,
                      )
                    : device,
              )
              .toList(growable: false);
        });
      case ReportEvent(:final reading):
        setState(() {
          _readings = <SensorReading>[
            reading,
            ..._readings.where((item) => item.deviceId != reading.deviceId),
          ];
          _liveStatus[reading.deviceId] = 'online';
        });
        // 详情弹层按设备过滤后插到顶部；无订阅者时广播流直接丢弃
        _reportStream.add(reading);
      case StatusEvent(:final deviceId, :final status):
        setState(() => _liveStatus[deviceId] = status);
      case GroupChangedEvent(:final deviceId, :final groupName):
        _applyGroupChange(deviceId, groupName);
    }
  }

  /// 分组变更：先就地更新，保证改组设备立即体现在当前筛选结果中；
  /// 再防抖重拉服务端快照，让分组 chips、`total` 与汇总卡回到 REST 真实口径。
  ///
  /// 取舍：chips 采用「重新拉取 `GET /api/devices/groups`」而非本地增减，
  /// 因为分组可能被并发改名、旧分组可能恰好清空，本地增量容易与服务端不一致；
  /// 分组变更是低频操作，一次整页刷新（与轮询兜底同样是第 1 页）可以接受。
  void _applyGroupChange(String deviceId, String groupName) {
    final index = _devices.indexWhere((device) => device.deviceId == deviceId);
    if (index >= 0) {
      setState(() {
        final updated = _devices[index].copyWith(groupName: groupName);
        if (_groupFilter.isEmpty || _deviceGroupName(updated) == _groupFilter) {
          _devices = List<DeviceSummary>.of(_devices)..[index] = updated;
        } else {
          // 改组后已不属于当前筛选分组：立即移出，total 同步减一，
          // 避免「筛选结果」与列表条数自相矛盾；随后由服务端刷新校正。
          _devices = List<DeviceSummary>.of(_devices)..removeAt(index);
          _deviceTotal = _deviceTotal > 0 ? _deviceTotal - 1 : 0;
          _deviceHasMore = _devices.length < _deviceTotal;
        }
      });
    }
    _scheduleGroupSync();
  }

  /// 连续改组事件合并为一次刷新；刷新同时更新 chips、total 与汇总卡。
  void _scheduleGroupSync() {
    _groupSyncDebounce?.cancel();
    _groupSyncDebounce = Timer(const Duration(milliseconds: 250), () {
      if (mounted) {
        unawaited(_loadData(silent: true));
      }
    });
  }

  bool _isOnline(DeviceSummary device) =>
      (_liveStatus[device.deviceId] ?? device.status) == 'online';

  // ---------------- 写操作（指令 / 告警确认） ----------------

  /// 写权限只认登录会话角色；服务端 403 后叠加本地禁用标记。
  bool get _canWrite {
    final session = _session;
    return session != null &&
        session.canIssueCommand &&
        _writeBlockedReason == null;
  }

  String get _writeDisabledHint {
    if (_writeBlockedReason != null) {
      return _writeBlockedReason!;
    }
    final session = _session;
    if (session == null) {
      return '未登录，写操作不可用';
    }
    if (!session.canIssueCommand) {
      return '当前账号为只读（${session.role}），指令下发与告警确认已禁用';
    }
    return '';
  }

  void _markWriteBlocked(String reason) {
    if (!mounted) {
      return;
    }
    setState(() => _writeBlockedReason = reason);
    ScaffoldMessenger.of(context).showSnackBar(SnackBar(content: Text(reason)));
  }

  /// 打开指令下发弹窗；成功后刷新指令回执（由详情页自行处理）。
  Future<CommandItem?> _openCommandDialog(DeviceSummary device) async {
    if (!_canWrite) {
      _markWriteBlocked(_writeDisabledHint);
      return null;
    }
    final result = await showDialog<CommandItem>(
      context: context,
      builder: (dialogContext) => CommandDialog(
        deviceId: device.deviceId,
        deviceName: device.name,
        onSubmit: (request) => _submitCommand(device, request),
      ),
    );
    if (result != null && mounted) {
      ScaffoldMessenger.of(context).showSnackBar(
        SnackBar(content: Text('指令已下发：${result.cmdId}（${result.state}）')),
      );
    }
    return result;
  }

  Future<CommandItem> _submitCommand(
    DeviceSummary device,
    CommandRequest request,
  ) async {
    final session = _session;
    if (session == null) {
      throw const ApiException(
        ApiErrorKind.unauthorized,
        '登录已失效，请重新登录',
        statusCode: 401,
      );
    }
    final client = _createApiClient(_baseUrl);
    try {
      return await client.sendCommand(
        token: session.token,
        deviceId: device.deviceId,
        type: request.type.wireName,
        payload: request.payload,
      );
    } on ApiException catch (error) {
      if (error.kind == ApiErrorKind.forbidden) {
        _markWriteBlocked('服务端返回 403：当前账号无权下发指令');
      } else if (error.kind == ApiErrorKind.unauthorized) {
        await _handleExpired();
      }
      rethrow;
    } finally {
      client.close();
    }
  }

  /// 告警二次确认：成功后立即从「未处理」列表移除。
  Future<void> _confirmAck(AlertItem alert) async {
    if (!_canWrite || _ackingAlertId != null) {
      return;
    }
    final session = _session;
    if (session == null) {
      return;
    }
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: const Text('确认告警'),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            Text(alert.message, style: const TextStyle(fontSize: 14)),
            const SizedBox(height: 10),
            Text(
              '设备：${alert.deviceId}\n'
              '级别：${alert.level} · 指标：${alert.metric}\n'
              '当前值 ${alert.value} / 阈值 ${alert.threshold}',
              style: const TextStyle(fontSize: 12, color: Colors.black54),
            ),
            const SizedBox(height: 12),
            const Text(
              '确认后该告警将从「未处理」列表移除，并作为当前账号记入审计日志。',
              style: TextStyle(fontSize: 12, color: Colors.black45),
            ),
          ],
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(dialogContext).pop(false),
            child: const Text('取消'),
          ),
          FilledButton(
            onPressed: () => Navigator.of(dialogContext).pop(true),
            child: const Text('确认'),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted) {
      return;
    }

    setState(() => _ackingAlertId = alert.alertId);
    final client = _createApiClient(_baseUrl);
    try {
      await client.ackAlert(token: session.token, alertId: alert.alertId);
      if (!mounted) {
        return;
      }
      setState(() {
        _alerts = _alerts
            .where((item) => item.alertId != alert.alertId)
            .toList(growable: false);
        if (_alertTotal > 0) {
          _alertTotal -= 1;
        }
        _stats = _stats.copyWith(
          alertOpen: _stats.alertOpen > 0 ? _stats.alertOpen - 1 : 0,
        );
        _devices = _devices
            .map(
              (device) => device.deviceId == alert.deviceId
                  ? device.copyWith(
                      openAlertCount: device.openAlertCount > 0
                          ? device.openAlertCount - 1
                          : 0,
                    )
                  : device,
            )
            .toList(growable: false);
      });
      ScaffoldMessenger.of(context)
          .showSnackBar(const SnackBar(content: Text('告警已确认')));
    } on ApiException catch (error) {
      if (!mounted) {
        return;
      }
      if (error.kind == ApiErrorKind.forbidden) {
        _markWriteBlocked('服务端返回 403：当前账号无权确认告警');
      } else if (error.kind == ApiErrorKind.unauthorized) {
        await _handleExpired();
      } else {
        ScaffoldMessenger.of(context)
            .showSnackBar(SnackBar(content: Text('确认失败：${error.userMessage}')));
      }
    } catch (_) {
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(const SnackBar(content: Text('确认失败，请稍后重试')));
      }
    } finally {
      client.close();
      if (mounted) {
        setState(() => _ackingAlertId = null);
      }
    }
  }

  /// 拉取第 1 页设备 + 当前页实时值 + 第 1 页告警 + 服务端汇总。
  ///
  /// 搜索 / 状态筛选全部作为请求参数下发并重置到第 1 页；实时事件只更新已加载设备，
  /// 不触发整页重拉。汇总卡使用 `/api/stats/overview` 的全量口径，与分页解耦。
  Future<void> _loadData({bool silent = false}) async {
    final session = _session;
    if (session == null) {
      return;
    }
    final requestId = ++_requestSeq;
    if (!silent) {
      setState(() {
        _loading = true;
        _error = null;
      });
    }

    final client = _createApiClient(_baseUrl);
    try {
      final page = await client.fetchDevices(
        token: session.token,
        page: 1,
        pageSize: _boardPageSize,
        status: _requestStatus,
        keyword: _keyword,
        group: _groupFilter.isEmpty ? null : _groupFilter,
        hasAlert: _statusFilter == _BoardStatusFilter.alert,
        withAlertCount: true,
      );
      final deviceIds = page.items
          .map((device) => device.deviceId)
          .toList(growable: false);
      final results = await Future.wait<Object>([
        client.fetchLatestReadings(
          token: session.token,
          deviceIds: deviceIds,
        ),
        client.fetchOpenAlerts(
          token: session.token,
          page: 1,
          pageSize: _alertPageSize,
        ),
        client.fetchStatsOverview(token: session.token),
        client.fetchGroups(token: session.token),
      ]);
      if (!mounted || requestId != _requestSeq) {
        return;
      }
      final alertPage = results[1] as AlertPage;
      setState(() {
        _devices = page.items;
        _devicePage = 1;
        _deviceTotal = page.total;
        _deviceHasMore = page.items.length < page.total;
        _readings = results[0] as List<SensorReading>;
        _alerts = alertPage.items;
        _alertPage = 1;
        _alertTotal = alertPage.total;
        _stats = results[2] as StatsOverview;
        _groups = results[3] as List<DeviceGroup>;
        // REST 快照比本地实时状态更新，清空后再由新事件补齐
        _liveStatus.clear();
        _error = null;
      });
    } on ApiException catch (error) {
      if (error.kind == ApiErrorKind.unauthorized) {
        await _handleExpired();
      } else if (mounted && requestId == _requestSeq) {
        setState(() => _error = _describe(error));
      }
    } catch (_) {
      if (mounted && requestId == _requestSeq) {
        setState(() => _error = '加载失败，请稍后重试');
      }
    } finally {
      client.close();
      if (mounted && requestId == _requestSeq) {
        setState(() => _loading = false);
      }
    }
  }

  /// 滚动到底加载下一页设备：追加结果，不重置滚动位置，也不打断已加载卡片。
  Future<void> _loadMoreDevices() async {
    final session = _session;
    if (session == null || _loadingMoreDevices || !_deviceHasMore) {
      return;
    }
    final requestId = _requestSeq;
    setState(() => _loadingMoreDevices = true);
    final client = _createApiClient(_baseUrl);
    try {
      final page = await client.fetchDevices(
        token: session.token,
        page: _devicePage + 1,
        pageSize: _boardPageSize,
        status: _requestStatus,
        keyword: _keyword,
        group: _groupFilter.isEmpty ? null : _groupFilter,
        hasAlert: _statusFilter == _BoardStatusFilter.alert,
        withAlertCount: true,
      );
      final newIds = page.items
          .map((device) => device.deviceId)
          .toList(growable: false);
      final readings = await client.fetchLatestReadings(
        token: session.token,
        deviceIds: newIds,
      );
      if (!mounted || requestId != _requestSeq) {
        return;
      }
      setState(() {
        _devices = <DeviceSummary>[..._devices, ...page.items];
        _devicePage = page.page > 0 ? page.page : _devicePage + 1;
        _deviceTotal = page.total;
        _deviceHasMore = _devices.length < page.total;
        _readings = _mergeReadings(_readings, readings);
      });
    } on ApiException catch (error) {
      if (error.kind == ApiErrorKind.unauthorized) {
        await _handleExpired();
      } else if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('加载更多失败：${error.userMessage}')),
        );
      }
    } catch (_) {
      if (mounted) {
        ScaffoldMessenger.of(context)
            .showSnackBar(const SnackBar(content: Text('加载更多失败，请稍后重试')));
      }
    } finally {
      client.close();
      if (mounted) {
        setState(() => _loadingMoreDevices = false);
      }
    }
  }

  /// 未处理告警按页追加，`_alertTotal` 始终是服务端全量数。
  Future<void> _loadMoreAlerts() async {
    final session = _session;
    if (session == null || _loadingMoreAlerts || _alerts.length >= _alertTotal) {
      return;
    }
    setState(() => _loadingMoreAlerts = true);
    final client = _createApiClient(_baseUrl);
    try {
      final page = await client.fetchOpenAlerts(
        token: session.token,
        page: _alertPage + 1,
        pageSize: _alertPageSize,
      );
      if (!mounted) {
        return;
      }
      setState(() {
        _alerts = <AlertItem>[..._alerts, ...page.items];
        _alertPage = page.page > 0 ? page.page : _alertPage + 1;
        _alertTotal = page.total;
      });
    } on ApiException catch (error) {
      if (error.kind == ApiErrorKind.unauthorized) {
        await _handleExpired();
      } else if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('加载告警失败：${error.userMessage}')),
        );
      }
    } finally {
      client.close();
      if (mounted) {
        setState(() => _loadingMoreAlerts = false);
      }
    }
  }

  List<SensorReading> _mergeReadings(
    List<SensorReading> current,
    List<SensorReading> incoming,
  ) {
    final byDevice = <String, SensorReading>{
      for (final reading in current) reading.deviceId: reading,
    };
    for (final reading in incoming) {
      byDevice[reading.deviceId] = reading;
    }
    return byDevice.values.toList(growable: false);
  }

  /// 设备页脚进入视口即触发下一页；页脚位于设备区末尾，不会在滚动告警区时误触发。
  void _onScroll() {
    if (!_deviceHasMore || _loadingMoreDevices) {
      return;
    }
    final footerContext = _deviceFooterKey.currentContext;
    if (footerContext == null) {
      return;
    }
    final box = footerContext.findRenderObject();
    if (box is! RenderBox) {
      return;
    }
    final viewport = RenderAbstractViewport.maybeOf(box);
    if (viewport == null || !_scrollController.hasClients) {
      return;
    }
    final footerOffset = viewport.getOffsetToReveal(box, 0).offset;
    final position = _scrollController.position;
    if (position.pixels + position.viewportDimension >= footerOffset - 32) {
      unawaited(_loadMoreDevices());
    }
  }

  /// 当前状态筛选对应的请求参数。
  String? get _requestStatus => switch (_statusFilter) {
    _BoardStatusFilter.online => 'online',
    _BoardStatusFilter.offline => 'offline',
    _ => null,
  };

  String _describe(ApiException error) {
    if (error.kind == ApiErrorKind.network ||
        error.kind == ApiErrorKind.timeout) {
      return '${error.userMessage}\n当前网关：$_baseUrl';
    }
    return error.userMessage;
  }

  /// 单飞 refresh 成功：同步内存会话，列表继续用新 access 重放原请求。
  void _onSessionRefreshed(AuthSession session) {
    if (!mounted) return;
    setState(() {
      _session = session;
      _notice = null;
    });
  }

  Future<void> _handleExpired() async {
    _stopRealtime();
    _stopPolling();
    await _storage.clearSession();
    if (!mounted) {
      return;
    }
    setState(() {
      _session = null;
      _notice = '登录已失效，请重新登录';
      _devices = const <DeviceSummary>[];
      _readings = const <SensorReading>[];
      _alerts = const <AlertItem>[];
      _devicePage = 1;
      _deviceTotal = 0;
      _deviceHasMore = false;
      _alertPage = 1;
      _alertTotal = 0;
      _stats = StatsOverview.empty;
      _groupFilter = '';
      _groups = const <DeviceGroup>[];
      _requestSeq += 1;
      _loading = false;
      _error = null;
      _writeBlockedReason = null;
      _ackingAlertId = null;
    });
  }

  Future<void> _logout() async {
    _stopRealtime();
    _stopPolling();
    final refreshToken = _session?.refreshToken ?? '';
    if (refreshToken.isNotEmpty) {
      // 先吊销服务端 refresh（离线/失败不阻塞本地退出），再清本地会话
      try {
        await _createApiClient(_baseUrl).logoutSession(
          refreshToken: refreshToken,
        );
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
      _notice = '已退出登录';
      _devices = const <DeviceSummary>[];
      _readings = const <SensorReading>[];
      _alerts = const <AlertItem>[];
      _devicePage = 1;
      _deviceTotal = 0;
      _deviceHasMore = false;
      _alertPage = 1;
      _alertTotal = 0;
      _stats = StatsOverview.empty;
      _groupFilter = '';
      _groups = const <DeviceGroup>[];
      _requestSeq += 1;
      _error = null;
      _writeBlockedReason = null;
      _ackingAlertId = null;
    });
  }

  Future<void> _editGateway() async {
    final gatewayController = TextEditingController(text: _baseUrl);
    final realtimeController = TextEditingController(
      text: _realtimeUrlOverride,
    );
    final fallback = ApiConfig.realtimeUrl(_baseUrl);
    final value = await showDialog<_GatewaySettings>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: const Text('网关与实时地址'),
        content: Column(
          mainAxisSize: MainAxisSize.min,
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            TextField(
              controller: gatewayController,
              autofocus: true,
              decoration: const InputDecoration(
                labelText: '网关地址',
                hintText: 'http://192.168.1.8:8000',
                border: OutlineInputBorder(),
              ),
              keyboardType: TextInputType.url,
            ),
            const SizedBox(height: 12),
            TextField(
              controller: realtimeController,
              decoration: InputDecoration(
                labelText: '实时地址（可留空）',
                hintText: fallback.isEmpty ? 'ws://192.168.1.8:9001' : fallback,
                border: const OutlineInputBorder(),
              ),
              keyboardType: TextInputType.url,
            ),
            const SizedBox(height: 8),
            Text(
              '真机需填电脑局域网 IP；实时地址留空则按网关主机自动推导 '
              'ws://<网关主机>:9001。',
              style: const TextStyle(fontSize: 12, color: Colors.black54),
            ),
          ],
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.of(dialogContext).pop(),
            child: const Text('取消'),
          ),
          FilledButton(
            onPressed: () => Navigator.of(dialogContext).pop(
              _GatewaySettings(
                baseUrl: gatewayController.text.trim(),
                realtimeUrl: realtimeController.text.trim(),
              ),
            ),
            child: const Text('保存'),
          ),
        ],
      ),
    );
    gatewayController.dispose();
    realtimeController.dispose();
    if (value == null || !mounted) {
      return;
    }
    if (!ApiConfig.isValid(value.baseUrl)) {
      ScaffoldMessenger.of(context)
          .showSnackBar(const SnackBar(content: Text('网关地址无效，未保存')));
      return;
    }
    if (value.realtimeUrl.isNotEmpty &&
        !ApiConfig.isValidRealtimeUrl(value.realtimeUrl)) {
      ScaffoldMessenger.of(context).showSnackBar(
        const SnackBar(content: Text('实时地址需以 ws:// 或 wss:// 开头，未保存')),
      );
      return;
    }
    final normalized = ApiConfig.normalize(value.baseUrl);
    await _storage.saveBaseUrl(normalized);
    await _storage.saveRealtimeUrl(value.realtimeUrl);
    if (!mounted) {
      return;
    }
    setState(() {
      _baseUrl = normalized;
      _realtimeUrlOverride = value.realtimeUrl;
    });
    if (_session != null) {
      await _loadData();
      _startRealtime();
    }
  }

  void _handleLoggedIn(AuthSession session, String baseUrl) {
    setState(() {
      _session = session;
      _baseUrl = baseUrl;
      _notice = null;
      _error = null;
      _booting = false;
      _writeBlockedReason = null;
      _ackingAlertId = null;
    });
    _loadData();
    _syncRealtimeLifecycle();
  }

  // ---------------- 搜索与状态筛选 ----------------

  /// 输入防抖：300ms 内连续输入只触发一次；关键词作为请求参数并重置到第 1 页。
  void _onSearchChanged(String value) {
    // 立即重建一次以同步输入框清除按钮显隐，实际请求等防抖结束后执行。
    if (mounted) {
      setState(() {});
    }
    _searchDebounce?.cancel();
    _searchDebounce = Timer(_searchDebounceDelay, () {
      if (!mounted) {
        return;
      }
      final keyword = value.trim();
      if (keyword == _keyword) {
        return;
      }
      setState(() => _keyword = keyword);
      unawaited(_loadData(silent: true));
    });
  }

  void _clearSearch() {
    _searchDebounce?.cancel();
    _searchController.clear();
    if (_keyword.isEmpty) {
      setState(() {});
      return;
    }
    setState(() => _keyword = '');
    unawaited(_loadData(silent: true));
  }

  /// 切换筛选后按新参数重拉第 1 页，避免把上一页结果拼到新筛选里。
  void _onStatusChanged(_BoardStatusFilter filter) {
    if (_statusFilter == filter) {
      return;
    }
    setState(() => _statusFilter = filter);
    unawaited(_loadData(silent: true));
  }

  /// 切换分组同样按新参数重拉第 1 页，与搜索、状态筛选、有告警组合生效。
  void _onGroupChanged(String groupName) {
    if (_groupFilter == groupName) {
      return;
    }
    setState(() => _groupFilter = groupName);
    unawaited(_loadData(silent: true));
  }

  /// 空分组名统一显示为「未分组」，与网关的分组概览口径一致。
  String _deviceGroupName(DeviceSummary device) {
    final name = device.groupName.trim();
    return name.isEmpty ? '未分组' : name;
  }

  /// 服务端已按状态 / 关键词 / 分组 / 有告警完成过滤与分页；这里只做实时状态修正：
  /// 上下线事件能立即改变「在线 / 离线」结果；「有告警」直接使用服务端聚合的
  /// `openAlertCount`，不再依赖未处理告警列表的前 50 条。
  List<DeviceSummary> get _filteredDevices {
    final keyword = _keyword.toLowerCase();
    return _devices.where((device) {
      if (_groupFilter.isNotEmpty && _deviceGroupName(device) != _groupFilter) {
        return false;
      }
      if (keyword.isNotEmpty) {
        final id = device.deviceId.toLowerCase();
        final name = device.name.toLowerCase();
        if (!id.contains(keyword) && !name.contains(keyword)) {
          return false;
        }
      }
      return switch (_statusFilter) {
        _BoardStatusFilter.all => true,
        _BoardStatusFilter.online => _isOnline(device),
        _BoardStatusFilter.offline => !_isOnline(device),
        _BoardStatusFilter.alert => device.openAlertCount > 0,
      };
    }).toList(growable: false);
  }

  bool get _filterActive =>
      _keyword.isNotEmpty ||
      _statusFilter != _BoardStatusFilter.all ||
      _groupFilter.isNotEmpty;

  SensorReading? _readingFor(String deviceId) {
    for (final reading in _readings) {
      if (reading.deviceId == deviceId) {
        return reading;
      }
    }
    return null;
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
    final filtered = _filteredDevices;
    return Scaffold(
      appBar: AppBar(
        title: const Text('移动看板'),
        centerTitle: false,
        actions: [
          IconButton(
            tooltip: '网关地址',
            onPressed: _editGateway,
            icon: const Icon(Icons.dns_outlined),
          ),
          IconButton(
            tooltip: '退出登录',
            onPressed: _logout,
            icon: const Icon(Icons.logout),
          ),
        ],
      ),
      body: SafeArea(
        child: RefreshIndicator(
          onRefresh: _loadData,
          child: ListView(
            controller: _scrollController,
            padding: const EdgeInsets.all(12),
            children: [
              if (_error != null) _buildErrorBanner(),
              _buildAccountBar(session),
              const SizedBox(height: 6),
              _buildRealtimeBar(),
              if (_writeDisabledHint.isNotEmpty) ...[
                const SizedBox(height: 8),
                _buildWriteHint(_writeDisabledHint),
              ],
              const SizedBox(height: 10),
              _buildSummary(),
              const SizedBox(height: 12),
              _buildFilterPanel(),
              const SizedBox(height: 12),
              _buildSectionTitle('设备与实时值'),
              if (_loading && _devices.isEmpty)
                const Padding(
                  padding: EdgeInsets.symmetric(vertical: 24),
                  child: Center(child: CircularProgressIndicator()),
                ),
              if (!_loading && _devices.isEmpty)
                _filterActive
                    ? _buildFilterEmpty()
                    : _buildEmpty('暂无设备。请先在 Web 后台注册设备，或启动设备模拟器上报。'),
              if (!_loading && _devices.isNotEmpty && filtered.isEmpty)
                _buildFilterEmpty(),
              ...filtered.map(_buildDeviceCard),
              if (filtered.isNotEmpty) _buildDeviceFooter(),
              const SizedBox(height: 8),
              _buildSectionTitle('未处理告警（$_alertTotal）'),
              if (_alerts.isEmpty) _buildEmpty('当前没有未处理告警'),
              ..._alerts.map(_buildAlertCard),
              if (_alerts.length < _alertTotal)
                Center(
                  child: TextButton(
                    onPressed: _loadingMoreAlerts ? null : _loadMoreAlerts,
                    child: Text(
                      _loadingMoreAlerts
                          ? '告警加载中…'
                          : '加载更多告警（已显示 ${_alerts.length} / $_alertTotal）',
                    ),
                  ),
                ),
              const SizedBox(height: 18),
              const Center(
                child: Text(
                  '写操作按登录角色开放：admin / operator 可下发指令与确认告警，viewer 只读',
                  textAlign: TextAlign.center,
                  style: TextStyle(fontSize: 12, color: Colors.black45),
                ),
              ),
              const SizedBox(height: 12),
            ],
          ),
        ),
      ),
    );
  }

  Widget _buildErrorBanner() {
    return Container(
      margin: const EdgeInsets.only(bottom: 10),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: Colors.red.withValues(alpha: 0.08),
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: Colors.red.withValues(alpha: 0.3)),
      ),
      child: Row(
        children: [
          const Icon(Icons.error_outline, color: Colors.red, size: 18),
          const SizedBox(width: 8),
          Expanded(
            child: Text(
              _error!,
              style: const TextStyle(color: Colors.red, fontSize: 13),
            ),
          ),
          TextButton(
            onPressed: _loading ? null : _loadData,
            child: const Text('重试'),
          ),
        ],
      ),
    );
  }

  Widget _buildAccountBar(AuthSession session) {
    return Row(
      children: [
        const Icon(Icons.account_circle_outlined, size: 18, color: Colors.teal),
        const SizedBox(width: 6),
        Expanded(
          child: Text(
            '${session.username} · ${session.role}',
            style: const TextStyle(fontSize: 13, color: Colors.black54),
          ),
        ),
        Flexible(
          child: Text(
            _baseUrl,
            overflow: TextOverflow.ellipsis,
            textAlign: TextAlign.right,
            style: const TextStyle(fontSize: 12, color: Colors.black45),
          ),
        ),
      ],
    );
  }

  /// 实时链路状态条：已连接 / 连接中 / 重连中；实时不可用时提示已切换轮询。
  Widget _buildRealtimeBar() {
    final service = _realtime;
    if (service == null) {
      return const SizedBox.shrink();
    }
    return ValueListenableBuilder<RealtimeState>(
      valueListenable: service.state,
      builder: (context, state, _) {
        final polling = _pollTimer != null;
        final color = switch (state) {
          RealtimeState.connected => Colors.green,
          RealtimeState.connecting => Colors.blueGrey,
          RealtimeState.reconnecting => Colors.orange,
          _ => Colors.grey,
        };
        final dot =
            state == RealtimeState.connecting ||
                state == RealtimeState.reconnecting
            ? const SizedBox(
                width: 10,
                height: 10,
                child: CircularProgressIndicator(strokeWidth: 2),
              )
            : Container(
                width: 8,
                height: 8,
                decoration: BoxDecoration(color: color, shape: BoxShape.circle),
              );
        final baseText = polling
            ? '${state.label} · 已切换轮询（每 15 秒）'
            : state.label;
        return Row(
          children: [
            dot,
            const SizedBox(width: 6),
            Expanded(
              child: AnimatedBuilder(
                animation: Listenable.merge([
                  service.failure,
                  service.attempts,
                ]),
                builder: (context, _) {
                  final failure = service.failure.value;
                  final attempts = service.attempts.value;
                  final text =
                      state == RealtimeState.reconnecting && failure != null
                      ? '$failure${attempts > 0 ? '（第 $attempts 次）' : ''}'
                      : baseText;
                  return Text(
                    text,
                    style: TextStyle(fontSize: 12, color: color),
                  );
                },
              ),
            ),
            Text(
              service.url,
              style: const TextStyle(fontSize: 11, color: Colors.black38),
            ),
          ],
        );
      },
    );
  }

  Widget _buildWriteHint(String text) {
    return Container(
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 7),
      decoration: BoxDecoration(
        color: Colors.orange.withValues(alpha: 0.08),
        borderRadius: BorderRadius.circular(8),
        border: Border.all(color: Colors.orange.withValues(alpha: 0.25)),
      ),
      child: Row(
        children: [
          const Icon(Icons.lock_outline, size: 15, color: Colors.orange),
          const SizedBox(width: 6),
          Expanded(
            child: Text(
              text,
              style: const TextStyle(fontSize: 12, color: Colors.black87),
            ),
          ),
        ],
      ),
    );
  }

  /// 汇总卡：设备总数 / 在线 / 未处理告警均来自服务端全量统计，
  /// 不因只加载第 1 页而失真；「筛选结果」为当前筛选的服务端 total。
  Widget _buildSummary() {
    final total = _stats.deviceTotal;
    return Container(
      padding: const EdgeInsets.symmetric(vertical: 14, horizontal: 8),
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: const Color(0xFFE2E8F0)),
      ),
      child: Column(
        children: [
          Row(
            children: [
              _summaryItem('设备总数', '$total'),
              _summaryItem('在线', '${_stats.deviceOnline}'),
              _summaryItem('未处理告警', '${_stats.alertOpen}'),
            ],
          ),
          const SizedBox(height: 10),
          Container(height: 1, color: const Color(0xFFE2E8F0)),
          const SizedBox(height: 8),
          Text(
            _filterActive
                ? '筛选结果：$_deviceTotal 台 / 全部 $total 台'
                : '全部 $total 台设备，实时值随 MQTT 增量更新',
            style: const TextStyle(fontSize: 12, color: Colors.black54),
          ),
        ],
      ),
    );
  }

  /// 设备分页页脚：进入视口自动加载下一页，加载中显示进度，末页显示「没有更多了」。
  Widget _buildDeviceFooter() {
    final Widget content;
    if (_loadingMoreDevices) {
      content = Row(
        mainAxisAlignment: MainAxisAlignment.center,
        children: const [
          SizedBox(
            width: 16,
            height: 16,
            child: CircularProgressIndicator(strokeWidth: 2),
          ),
          SizedBox(width: 8),
          Text('加载中…', style: TextStyle(fontSize: 12, color: Colors.black54)),
        ],
      );
    } else if (_deviceHasMore) {
      content = Text(
        '上拉加载更多（已显示 ${_devices.length} / $_deviceTotal）',
        style: const TextStyle(fontSize: 12, color: Colors.black45),
      );
    } else {
      content = const Text(
        '没有更多了',
        style: TextStyle(fontSize: 12, color: Colors.black38),
      );
    }
    return Padding(
      key: _deviceFooterKey,
      padding: const EdgeInsets.symmetric(vertical: 10),
      child: Center(child: content),
    );
  }

  /// 顶部搜索框 + 状态筛选；筛选只作用于列表，汇总卡保留全量口径。
  Widget _buildFilterPanel() {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        TextField(
          controller: _searchController,
          onChanged: _onSearchChanged,
          textInputAction: TextInputAction.search,
          decoration: InputDecoration(
            hintText: '搜索设备名称或 ID',
            isDense: true,
            prefixIcon: const Icon(Icons.search, size: 20),
            suffixIcon: _searchController.text.isEmpty
                ? null
                : IconButton(
                    tooltip: '清空搜索',
                    onPressed: _clearSearch,
                    icon: const Icon(Icons.close, size: 18),
                  ),
            border: OutlineInputBorder(borderRadius: BorderRadius.circular(10)),
          ),
        ),
        const SizedBox(height: 8),
        SingleChildScrollView(
          scrollDirection: Axis.horizontal,
          child: Row(
            children: [
              for (final filter in _BoardStatusFilter.values) ...[
                ChoiceChip(
                  label: Text(filter.label),
                  selected: _statusFilter == filter,
                  onSelected: (_) => _onStatusChanged(filter),
                ),
                const SizedBox(width: 8),
              ],
            ],
          ),
        ),
        const SizedBox(height: 8),
        SingleChildScrollView(
          scrollDirection: Axis.horizontal,
          child: Row(
            children: [
              const Padding(
                padding: EdgeInsets.only(right: 8, top: 8),
                child: Text(
                  '分组：',
                  style: TextStyle(fontSize: 12, color: Colors.black54),
                ),
              ),
              ChoiceChip(
                label: const Text('全部分组'),
                selected: _groupFilter.isEmpty,
                onSelected: (_) => _onGroupChanged(''),
              ),
              for (final group in _groups) ...[
                const SizedBox(width: 8),
                ChoiceChip(
                  label: Text(group.groupName),
                  selected: _groupFilter == group.groupName,
                  onSelected: (_) => _onGroupChanged(group.groupName),
                ),
              ],
            ],
          ),
        ),
      ],
    );
  }

  /// 筛选无结果时的可读空状态：明确说明原因是筛选，而不是设备不存在。
  Widget _buildFilterEmpty() {
    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: const Color(0xFFE2E8F0)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              const Icon(Icons.search_off, size: 18, color: Colors.black45),
              const SizedBox(width: 8),
              Text(
                '没有匹配的设备',
                style: const TextStyle(
                  fontSize: 14,
                  fontWeight: FontWeight.w600,
                ),
              ),
            ],
          ),
          const SizedBox(height: 6),
          Text(
            '当前筛选：${_statusFilter.label}'
            '${_groupFilter.isEmpty ? '' : ' · 分组「$_groupFilter」'}'
            '${_keyword.isEmpty ? '' : ' · 关键词「$_keyword」'}；'
            '可清空关键词或切回「全部」。上方汇总卡仍为全部 ${_stats.deviceTotal} 台设备。',
            style: const TextStyle(fontSize: 12, color: Colors.black54),
          ),
        ],
      ),
    );
  }

  Widget _summaryItem(String label, String value) {
    return Expanded(
      child: Column(
        children: [
          Text(
            value,
            style: const TextStyle(fontSize: 22, fontWeight: FontWeight.w600),
          ),
          const SizedBox(height: 2),
          Text(
            label,
            style: const TextStyle(fontSize: 12, color: Colors.black54),
          ),
        ],
      ),
    );
  }

  Widget _buildSectionTitle(String text) {
    return Padding(
      padding: const EdgeInsets.only(bottom: 8),
      child: Text(
        text,
        style: const TextStyle(fontSize: 15, fontWeight: FontWeight.w600),
      ),
    );
  }

  Widget _buildEmpty(String text) {
    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.all(16),
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: const Color(0xFFE2E8F0)),
      ),
      child: Text(
        text,
        style: const TextStyle(fontSize: 13, color: Colors.black54),
      ),
    );
  }

  Widget _buildDeviceCard(DeviceSummary device) {
    final reading = _readingFor(device.deviceId);
    return Container(
      margin: const EdgeInsets.only(bottom: 10),
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: const Color(0xFFE2E8F0)),
      ),
      child: InkWell(
        borderRadius: BorderRadius.circular(12),
        onTap: () => _showDeviceDetail(device, reading),
        child: Padding(
          padding: const EdgeInsets.all(12),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              Row(
                children: [
                  Expanded(
                    child: Text(
                      device.name,
                      style: const TextStyle(
                        fontSize: 15,
                        fontWeight: FontWeight.w600,
                      ),
                    ),
                  ),
                  if (device.openAlertCount > 0) ...[
                    Container(
                      padding: const EdgeInsets.symmetric(
                        horizontal: 6,
                        vertical: 2,
                      ),
                      decoration: BoxDecoration(
                        color: Colors.red.withValues(alpha: 0.1),
                        borderRadius: BorderRadius.circular(6),
                      ),
                      child: Text(
                        '告警 ${device.openAlertCount}',
                        style: const TextStyle(
                          fontSize: 11,
                          color: Colors.red,
                          fontWeight: FontWeight.w600,
                        ),
                      ),
                    ),
                    const SizedBox(width: 6),
                  ],
                  OnlineBadge(online: _isOnline(device)),
                ],
              ),
              const SizedBox(height: 4),
              Text(
                '${device.deviceId} · ${device.groupName} · FW ${device.firmwareVersion}',
                style: const TextStyle(fontSize: 12, color: Colors.black45),
              ),
              const SizedBox(height: 8),
              if (reading == null)
                const Text(
                  '暂无上报数据',
                  style: TextStyle(fontSize: 13, color: Colors.black45),
                )
              else
                Text(
                  '温度 ${reading.temperature.toStringAsFixed(1)}℃ · '
                  '湿度 ${reading.humidity.toStringAsFixed(0)}% · '
                  '电量 ${reading.battery}% · RSSI ${reading.rssi}dBm',
                  style: const TextStyle(fontSize: 13, color: Colors.black87),
                ),
              const SizedBox(height: 6),
              Text(
                reading?.ts == null
                    ? '最近上报：${_formatTime(device.lastReportAt)}'
                    : '最新上报：${_formatTime(reading!.ts)}',
                style: const TextStyle(fontSize: 12, color: Colors.black45),
              ),
            ],
          ),
        ),
      ),
    );
  }

  Widget _buildAlertCard(AlertItem alert) {
    final color = alert.critical ? Colors.red : Colors.orange;
    return Container(
      margin: const EdgeInsets.only(bottom: 10),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.05),
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: color.withValues(alpha: 0.25)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Container(
                padding: const EdgeInsets.symmetric(horizontal: 7, vertical: 2),
                decoration: BoxDecoration(
                  color: color.withValues(alpha: 0.15),
                  borderRadius: BorderRadius.circular(6),
                ),
                child: Text(
                  alert.level,
                  style: TextStyle(
                    color: color,
                    fontSize: 11,
                    fontWeight: FontWeight.w600,
                  ),
                ),
              ),
              const SizedBox(width: 8),
              Expanded(
                child: Text(
                  alert.message,
                  style: const TextStyle(fontSize: 13),
                ),
              ),
            ],
          ),
          const SizedBox(height: 6),
          Text(
            '${alert.deviceId} · ${alert.metric} '
            '${alert.value} / 阈值 ${alert.threshold} · '
            '${_formatTime(alert.createdAt)}',
            style: const TextStyle(fontSize: 12, color: Colors.black45),
          ),
          const SizedBox(height: 2),
          Align(
            alignment: Alignment.centerRight,
            child: _ackingAlertId == alert.alertId
                ? const Padding(
                    padding: EdgeInsets.symmetric(vertical: 6, horizontal: 8),
                    child: SizedBox(
                      width: 16,
                      height: 16,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    ),
                  )
                : TextButton.icon(
                    onPressed: _canWrite && _ackingAlertId == null
                        ? () => _confirmAck(alert)
                        : null,
                    icon: const Icon(Icons.check_circle_outline, size: 16),
                    label: const Text('确认告警'),
                  ),
          ),
        ],
      ),
    );
  }

  void _showDeviceDetail(DeviceSummary device, SensorReading? reading) {
    final session = _session;
    if (session == null) {
      return;
    }
    showModalBottomSheet<void>(
      context: context,
      isScrollControlled: true,
      showDragHandle: true,
      builder: (_) => _DeviceDetailSheet(
        device: device,
        reading: reading,
        online: _isOnline(device),
        baseUrl: _baseUrl,
        token: session.token,
        canWrite: _canWrite,
        isAdmin: session.role == 'admin',
        apiClientFactory: _createApiClient,
        reportStream: _reportStream.stream,
        onIssueCommand: () => _openCommandDialog(device),
      ),
    );
  }
}

class _DeviceDetailSheet extends StatefulWidget {
  const _DeviceDetailSheet({
    required this.device,
    required this.reading,
    required this.online,
    required this.baseUrl,
    required this.token,
    required this.canWrite,
    required this.isAdmin,
    required this.apiClientFactory,
    required this.reportStream,
    required this.onIssueCommand,
  });

  final DeviceSummary device;
  final SensorReading? reading;
  final bool online;
  final String baseUrl;
  final String token;

  /// 当前会话是否可写（viewer 或服务端 403 后为 false）。
  final bool canWrite;

  /// 轮换/吊销会导致设备掉线，仅 admin 可执行（首次签发 operator 可用）。
  final bool isAdmin;

  /// 与页面共用同一 REST 客户端工厂，测试可注入 fake。
  final BoardApiClientFactory apiClientFactory;

  /// 页面实时上报广播；弹层按设备过滤后插入列表顶部。
  final Stream<SensorReading> reportStream;

  /// 页面层统一处理指令弹窗、提交与 401/403，返回网关回执。
  final Future<CommandItem?> Function() onIssueCommand;

  @override
  State<_DeviceDetailSheet> createState() => _DeviceDetailSheetState();
}

class _DeviceDetailSheetState extends State<_DeviceDetailSheet> {
  /// 每页条数；请求 limit = _pageSize + 1 作为「是否还有更早数据」的探测。
  static const int _pageSize = 10;

  List<CommandItem> _commands = const <CommandItem>[];
  /// 历史上报按时间倒序（最新在前），实时新上报插入顶部。
  List<SensorReading> _records = const <SensorReading>[];
  bool _loading = true;
  bool _issuing = false;
  bool _credentialBusy = false;
  bool _loadingMore = false;
  bool _hasMore = false;
  String? _error;

  /// 凭证操作后会重新拉取该设备，保证版本/时间/指纹前缀与服务端一致。
  late DeviceSummary _device = widget.device;

  /// 已从网关取回的条数（不含实时插入），作为下一页 offset；
  /// 实时插入会加长列表，但不能让服务端游标跟着漂移。
  int _serverCount = 0;

  final ScrollController _scrollController = ScrollController();

  /// 列表首行的 key：插入前测高，插入后按该行高度补偿滚动位置。
  final GlobalKey _firstRowKey = GlobalKey();

  StreamSubscription<SensorReading>? _reportSub;

  @override
  void initState() {
    super.initState();
    _scrollController.addListener(_onScroll);
    _reportSub = widget.reportStream.listen(_onRealtimeReport);
    _loadDetail();
  }

  @override
  void dispose() {
    _reportSub?.cancel();
    _scrollController.removeListener(_onScroll);
    _scrollController.dispose();
    super.dispose();
  }

  /// 滚到接近底部时自动取下一页；没有更早数据时为空操作。
  void _onScroll() {
    if (!_scrollController.hasClients) {
      return;
    }
    final position = _scrollController.position;
    if (position.pixels >= position.maxScrollExtent - 80) {
      _loadOlder();
    }
  }

  /// 打开指令弹窗；成功后刷新回执列表，让新指令出现在顶部。
  Future<void> _handleIssueCommand() async {
    if (!widget.canWrite || _issuing) {
      return;
    }
    setState(() => _issuing = true);
    final result = await widget.onIssueCommand();
    if (!mounted) {
      return;
    }
    setState(() => _issuing = false);
    if (result != null) {
      await _loadDetail();
    }
  }

  String get _credentialStatusLabel {
    switch (_device.mqttCredentialStatus) {
      case 'active':
        return '已签发';
      case 'revoked':
        return '已吊销';
      default:
        return '未签发';
    }
  }

  String _credentialImpactText(bool rotation) {
    if (!rotation) {
      return '确认为设备 ${_device.deviceId} 签发 MQTT 凭证？\n\n'
          '签发后设备即可使用该用户名/口令接入 Broker；新口令只显示一次。';
    }
    return '确认轮换设备 ${_device.deviceId} 的 MQTT 凭证？\n\n'
        '影响：\n'
        '1. 旧凭证对新连接立即失效；已建立的连接可能维持到下次断线重连，设备在重连前可能仍显示在线；\n'
        '2. 设备必须更新配置中的口令后才能重连，未更新前一旦断线将持续离线；\n'
        '3. 若 Broker 重载失败，新口令已写入但旧凭证可能仍然有效，需按提示人工重载。\n\n'
        '新口令只显示一次，服务端只保存指纹，关闭后无法再查询。';
  }

  void _showCredentialMessage(String message) {
    if (!mounted) {
      return;
    }
    ScaffoldMessenger.of(context)
      ..hideCurrentSnackBar()
      ..showSnackBar(SnackBar(content: Text(message)));
  }

  /// 签发或轮换：二次确认 → 调网关 → 一次性口令弹窗 → 刷新设备凭证状态。
  Future<void> _handleIssueOrRotateCredential() async {
    if (_credentialBusy || !widget.canWrite) {
      return;
    }
    final rotation = _device.mqttCredentialStatus == 'active';
    if (rotation && !widget.isAdmin) {
      _showCredentialMessage('轮换已有凭证会导致设备掉线，仅管理员可执行');
      return;
    }

    final confirmed = await showDialog<bool>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: Text(rotation ? '轮换设备凭证' : '签发设备凭证'),
        content: SingleChildScrollView(child: Text(_credentialImpactText(rotation))),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogContext, false),
            child: const Text('取消'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(dialogContext, true),
            child: Text(rotation ? '确认轮换' : '确认签发'),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted) {
      return;
    }

    setState(() => _credentialBusy = true);
    final client = widget.apiClientFactory(widget.baseUrl);
    DeviceCredentialResult? result;
    try {
      result = await client.issueDeviceCredential(
        token: widget.token,
        deviceId: _device.deviceId,
      );
    } on ApiException catch (error) {
      _showCredentialMessage(error.message);
    } finally {
      if (mounted) {
        setState(() => _credentialBusy = false);
      }
    }
    if (result == null || !mounted) {
      return;
    }

    await _refreshCredential();
    if (!mounted) {
      return;
    }
    await _showOneTimePassword(result);
  }

  /// 吊销凭证（仅 admin）：吊销后设备必须重新签发才能接入。
  Future<void> _handleRevokeCredential() async {
    if (_credentialBusy || !widget.isAdmin) {
      return;
    }
    final confirmed = await showDialog<bool>(
      context: context,
      builder: (dialogContext) => AlertDialog(
        title: const Text('吊销设备凭证'),
        content: Text(
          '确认吊销设备 ${_device.deviceId} 的 MQTT 凭证？\n\n'
          '吊销后 Broker 账号被删除，设备立即无法接入，直到重新签发凭证；\n'
          '若 Broker 重载失败，旧凭证可能仍然有效，需按提示人工重载。',
        ),
        actions: [
          TextButton(
            onPressed: () => Navigator.pop(dialogContext, false),
            child: const Text('取消'),
          ),
          FilledButton(
            onPressed: () => Navigator.pop(dialogContext, true),
            child: const Text('确认吊销'),
          ),
        ],
      ),
    );
    if (confirmed != true || !mounted) {
      return;
    }

    setState(() => _credentialBusy = true);
    final client = widget.apiClientFactory(widget.baseUrl);
    try {
      final result = await client.revokeDeviceCredential(
        token: widget.token,
        deviceId: _device.deviceId,
      );
      _showCredentialMessage(
        result.reloaded
            ? '凭证已吊销，Broker 已重载'
            : '凭证已吊销，但 Broker 未重载，旧凭证可能仍然有效：${result.reloadDetail}',
      );
      await _refreshCredential();
    } on ApiException catch (error) {
      _showCredentialMessage(error.message);
    } finally {
      if (mounted) {
        setState(() => _credentialBusy = false);
      }
    }
  }

  /// 重新拉取该设备：不信任本地推断的版本/吊销状态。
  Future<void> _refreshCredential() async {
    final client = widget.apiClientFactory(widget.baseUrl);
    try {
      final page = await client.fetchDevices(
        token: widget.token,
        keyword: _device.deviceId,
        pageSize: 10,
      );
      final match = page.items
          .where((item) => item.deviceId == _device.deviceId)
          .cast<DeviceSummary?>()
          .firstWhere((item) => item != null, orElse: () => null);
      if (match != null && mounted) {
        setState(() => _device = match);
      }
    } on ApiException catch (error) {
      _showCredentialMessage('凭证状态刷新失败：${error.message}');
    }
  }

  /// 一次性口令弹窗：不可点遮罩/返回关闭，只能点「我已保存」；
  /// 口令只存在于本次弹窗的局部变量，关闭后无法从界面再次读取。
  Future<void> _showOneTimePassword(DeviceCredentialResult result) async {
    await showDialog<void>(
      context: context,
      barrierDismissible: false,
      builder: (dialogContext) => PopScope(
        canPop: false,
        child: AlertDialog(
          title: const Text('请立即保存新凭证口令'),
          content: SingleChildScrollView(
            child: Column(
              crossAxisAlignment: CrossAxisAlignment.start,
              mainAxisSize: MainAxisSize.min,
              children: [
                Text(
                  '口令只显示这一次：服务端只保存 SHA-256 指纹，关闭后无法再次查询，'
                  '如需新口令只能重新轮换。',
                  style: const TextStyle(fontSize: 12, color: Colors.black54),
                ),
                const SizedBox(height: 10),
                SelectableText(
                  result.password,
                  key: const Key('one-time-credential-password'),
                  style: const TextStyle(
                    fontFamily: 'monospace',
                    fontSize: 13,
                  ),
                ),
                const SizedBox(height: 10),
                Row(
                  children: [
                    OutlinedButton.icon(
                      onPressed: () async {
                        await Clipboard.setData(
                          ClipboardData(text: result.password),
                        );
                        _showCredentialMessage('口令已复制到剪贴板');
                      },
                      icon: const Icon(Icons.copy, size: 16),
                      label: const Text('复制口令'),
                    ),
                  ],
                ),
                if (!result.reloaded) ...[
                  const SizedBox(height: 8),
                  Container(
                    key: const Key('credential-reload-failed'),
                    padding: const EdgeInsets.all(10),
                    decoration: BoxDecoration(
                      color: const Color(0xFFFFF3CD),
                      borderRadius: BorderRadius.circular(6),
                    ),
                    child: Text(
                      '已写入新凭证，但 Broker 未重载，旧凭证可能仍然有效。\n'
                      '补救：在 Broker 主机执行重载（systemctl reload mosquitto '
                      '或 docker compose restart mqtt），确认成功后再让设备改用新口令。\n'
                      '重载详情：${result.reloadDetail}',
                      style: const TextStyle(fontSize: 12, color: Color(0xFF8A6D3B)),
                    ),
                  ),
                ],
              ],
            ),
          ),
          actions: [
            FilledButton(
              key: const Key('one-time-credential-ack'),
              onPressed: () => Navigator.pop(dialogContext),
              child: const Text('我已保存，关闭'),
            ),
          ],
        ),
      ),
    );
  }

  /// 首屏：指令回执 + 第一页历史上报。
  Future<void> _loadDetail() async {
    final client = widget.apiClientFactory(widget.baseUrl);
    try {
      final results = await Future.wait<Object>([
        client.fetchCommands(
          token: widget.token,
          deviceId: widget.device.deviceId,
        ),
        client.fetchDeviceRecords(
          token: widget.token,
          deviceId: widget.device.deviceId,
          limit: _pageSize + 1,
        ),
      ]);
      final batches = results[1] as List<SensorReading>;
      // 网关按时间正序返回，最新一条在末尾；探测多取的一条要丢掉
      final page = batches.length > _pageSize
          ? batches.sublist(batches.length - _pageSize)
          : batches;
      if (mounted) {
        setState(() {
          _commands = results[0] as List<CommandItem>;
          _records = _newestFirst(page);
          _serverCount = page.length;
          _hasMore = batches.length > _pageSize;
          _error = null;
        });
      }
    } on ApiException catch (error) {
      if (mounted) {
        setState(() => _error = error.userMessage);
      }
    } finally {
      client.close();
      if (mounted) {
        setState(() => _loading = false);
      }
    }
  }

  /// 加载更早一页，接在列表末尾；到底后展示「没有更早数据」。
  Future<void> _loadOlder() async {
    if (_loading || _loadingMore || !_hasMore) {
      return;
    }
    setState(() => _loadingMore = true);
    final client = widget.apiClientFactory(widget.baseUrl);
    try {
      final batches = await client.fetchDeviceRecords(
        token: widget.token,
        deviceId: widget.device.deviceId,
        limit: _pageSize + 1,
        offset: _serverCount,
      );
      final known = _records.map(_recordKey).toSet();
      final older = _newestFirst(batches)
          .where((item) => !known.contains(_recordKey(item)))
          .take(_pageSize)
          .toList(growable: false);
      if (mounted) {
        setState(() {
          _records = <SensorReading>[..._records, ...older];
          _serverCount += older.length;
          _hasMore = batches.length > _pageSize;
        });
      }
    } on ApiException catch (error) {
      if (mounted) {
        setState(() => _error = error.userMessage);
      }
    } finally {
      client.close();
      if (mounted) {
        setState(() => _loadingMore = false);
      }
    }
  }

  /// 实时新上报：插到顶部并保持当前查看位置。
  ///
  /// 停在顶部时不做补偿（新数据直接可见）；已向下浏览时按插入行高补回偏移，
  /// 避免正在看的历史数据被顶走。
  Future<void> _onRealtimeReport(SensorReading reading) async {
    if (reading.deviceId != widget.device.deviceId) {
      return;
    }
    if (_records.any((item) => _recordKey(item) == _recordKey(reading))) {
      return;
    }
    final offsetBefore = _scrollController.hasClients
        ? _scrollController.offset
        : 0.0;
    final insertedExtent = _firstRowExtent();
    setState(() => _records = <SensorReading>[reading, ..._records]);
    if (offsetBefore <= 0) {
      return;
    }
    await WidgetsBinding.instance.endOfFrame;
    if (!mounted || !_scrollController.hasClients) {
      return;
    }
    _scrollController.jumpTo(offsetBefore + insertedExtent);
  }

  double _firstRowExtent() {
    final render = _firstRowKey.currentContext?.findRenderObject();
    if (render is RenderBox && render.hasSize) {
      return render.size.height;
    }
    return 52;
  }

  String _recordKey(SensorReading record) =>
      '${record.ts?.toIso8601String() ?? ''}#${record.seq}';

  /// 统一按时间倒序（无时间戳时用 seq 兜底），保证实时插入与分页顺序一致。
  List<SensorReading> _newestFirst(List<SensorReading> rows) {
    final sorted = List<SensorReading>.of(rows)..sort((a, b) {
      final at = a.ts;
      final bt = b.ts;
      if (at != null && bt != null) {
        final byTime = bt.compareTo(at);
        if (byTime != 0) {
          return byTime;
        }
      }
      return b.seq.compareTo(a.seq);
    });
    return List<SensorReading>.unmodifiable(sorted);
  }

  Widget _credentialRow(String label, String value) => Padding(
    padding: const EdgeInsets.symmetric(vertical: 2),
    child: Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        SizedBox(
          width: 72,
          child: Text(
            label,
            style: const TextStyle(fontSize: 12, color: Colors.black45),
          ),
        ),
        Expanded(
          child: Text(value, style: const TextStyle(fontSize: 12)),
        ),
      ],
    ),
  );

  /// 凭证状态区：只展示元数据与短前缀，绝不渲染口令或完整指纹。
  Widget _buildCredentialSection() {
    final status = _device.mqttCredentialStatus;
    final issued = status != 'missing';
    final revoked = status == 'revoked';
    final active = status == 'active';
    final statusColor = active
        ? Colors.green
        : revoked
        ? Colors.red
        : Colors.grey;

    return Container(
      key: const Key('device-credential-section'),
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        border: Border.all(color: Colors.black12),
        borderRadius: BorderRadius.circular(8),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              const Text(
                'MQTT 凭证',
                style: TextStyle(fontSize: 15, fontWeight: FontWeight.w600),
              ),
              const SizedBox(width: 8),
              Chip(
                key: const Key('credential-status-chip'),
                label: Text(
                  _credentialStatusLabel,
                  style: const TextStyle(fontSize: 11, color: Colors.white),
                ),
                backgroundColor: statusColor,
                visualDensity: VisualDensity.compact,
                materialTapTargetSize: MaterialTapTargetSize.shrinkWrap,
              ),
            ],
          ),
          const SizedBox(height: 4),
          if (!issued)
            Text(
              '该设备尚未签发凭证，无法接入 Broker',
              key: const Key('credential-unissued-tip'),
              style: const TextStyle(fontSize: 12, color: Colors.red),
            ),
          if (revoked)
            const Text(
              '该设备凭证已吊销，当前无法接入 Broker；如需恢复请重新签发。',
              key: Key('credential-revoked-tip'),
              style: TextStyle(fontSize: 12, color: Colors.red),
            ),
          if (issued) ...[
            _credentialRow('用户名', _device.mqttUsername ?? _device.deviceId),
            _credentialRow('版本', '${_device.mqttCredentialVersion}'),
            _credentialRow('首次签发', _formatTime(_device.mqttCredentialIssuedAt)),
            _credentialRow('最近轮换', _formatTime(_device.mqttCredentialRotatedAt)),
            if (revoked)
              _credentialRow('吊销时间', _formatTime(_device.mqttCredentialRevokedAt)),
            _credentialRow(
              '指纹前缀',
              _device.mqttCredentialFingerprintPrefix == null
                  ? '—'
                  : '${_device.mqttCredentialFingerprintPrefix}…',
            ),
            const SizedBox(height: 4),
            const Text(
              '仅展示指纹前 8 位，用于人工对账；服务端不保存口令明文，也不会返回完整指纹。',
              style: TextStyle(fontSize: 11, color: Colors.black45),
            ),
          ],
          const SizedBox(height: 8),
          Wrap(
            spacing: 8,
            runSpacing: 6,
            crossAxisAlignment: WrapCrossAlignment.center,
            children: [
              if (!issued)
                FilledButton(
                  key: const Key('credential-issue-button'),
                  onPressed: widget.canWrite && !_credentialBusy
                      ? _handleIssueOrRotateCredential
                      : null,
                  child: Text(revoked ? '重新签发凭证' : '签发凭证'),
                ),
              if (active) ...[
                FilledButton.tonal(
                  key: const Key('credential-rotate-button'),
                  onPressed: widget.isAdmin && !_credentialBusy
                      ? _handleIssueOrRotateCredential
                      : null,
                  child: const Text('轮换凭证'),
                ),
                TextButton(
                  key: const Key('credential-revoke-button'),
                  onPressed: widget.isAdmin && !_credentialBusy
                      ? _handleRevokeCredential
                      : null,
                  child: const Text(
                    '吊销凭证',
                    style: TextStyle(color: Colors.red),
                  ),
                ),
                if (!widget.isAdmin)
                  const Text(
                    '轮换/吊销仅管理员可执行',
                    style: TextStyle(fontSize: 11, color: Colors.black45),
                  ),
              ],
            ],
          ),
        ],
      ),
    );
  }

  @override
  Widget build(BuildContext context) {
    final reading = widget.reading;
    return SafeArea(
      child: SizedBox(
        height: MediaQuery.sizeOf(context).height * 0.72,
        child: ListView(
          controller: _scrollController,
          padding: const EdgeInsets.fromLTRB(16, 0, 16, 16),
          children: [
            Row(
              children: [
                Expanded(
                  child: Text(
                    widget.device.name,
                    style: const TextStyle(
                      fontSize: 17,
                      fontWeight: FontWeight.w600,
                    ),
                  ),
                ),
                OnlineBadge(online: widget.online),
              ],
            ),
            const SizedBox(height: 4),
            Text(
              '${widget.device.deviceId} · FW ${widget.device.firmwareVersion}',
              style: const TextStyle(fontSize: 12, color: Colors.black45),
            ),
            const SizedBox(height: 10),
            Row(
              children: [
                FilledButton.tonalIcon(
                  onPressed: widget.canWrite && !_issuing
                      ? _handleIssueCommand
                      : null,
                  icon: _issuing
                      ? const SizedBox(
                          width: 14,
                          height: 14,
                          child: CircularProgressIndicator(strokeWidth: 2),
                        )
                      : const Icon(Icons.send, size: 16),
                  label: Text(_issuing ? '提交中…' : '下发指令'),
                ),
                const SizedBox(width: 10),
                if (!widget.canWrite)
                  const Expanded(
                    child: Text(
                      '当前无写权限',
                      style: TextStyle(fontSize: 12, color: Colors.black45),
                    ),
                  ),
              ],
            ),
            const SizedBox(height: 12),
            _buildCredentialSection(),
            const SizedBox(height: 12),
            if (reading == null)
              const Text('暂无上报数据', style: TextStyle(color: Colors.black45))
            else
              Wrap(
                spacing: 8,
                runSpacing: 8,
                children: [
                  SensorCard(
                    label: '温度',
                    value: reading.temperature.toStringAsFixed(1),
                    unit: '℃',
                    icon: Icons.thermostat,
                    color: Colors.deepOrange,
                  ),
                  SensorCard(
                    label: '湿度',
                    value: reading.humidity.toStringAsFixed(0),
                    unit: '%',
                    icon: Icons.water_drop,
                    color: Colors.blue,
                  ),
                  SensorCard(
                    label: '电压',
                    value: reading.voltage.toStringAsFixed(2),
                    unit: 'V',
                    icon: Icons.bolt,
                    color: Colors.amber.shade700,
                  ),
                  SensorCard(
                    label: '电量',
                    value: '${reading.battery}',
                    unit: '%',
                    icon: Icons.battery_full,
                    color: Colors.green,
                  ),
                ],
              ),
            const SizedBox(height: 14),
            const Text(
              '指令回执',
              style: TextStyle(fontSize: 15, fontWeight: FontWeight.w600),
            ),
            const SizedBox(height: 8),
            if (!_loading && _error == null && _commands.isEmpty)
              const Text(
                '暂无指令记录',
                style: TextStyle(fontSize: 13, color: Colors.black45),
              )
            else if (!_loading && _error == null)
              ..._commands.map(_buildCommandRow),
            const SizedBox(height: 14),
            const Text(
              '历史上报',
              style: TextStyle(fontSize: 15, fontWeight: FontWeight.w600),
            ),
            const SizedBox(height: 8),
            if (_loading)
              const Padding(
                padding: EdgeInsets.symmetric(vertical: 12),
                child: Column(
                  children: [
                    CircularProgressIndicator(),
                    SizedBox(height: 8),
                    Text(
                      '加载中…',
                      style: TextStyle(fontSize: 12, color: Colors.black45),
                    ),
                  ],
                ),
              )
            else if (_error != null)
              Text(
                _error!,
                style: const TextStyle(color: Colors.red, fontSize: 13),
              )
            else if (_records.isEmpty)
              const Text(
                '暂无历史上报',
                style: TextStyle(fontSize: 13, color: Colors.black45),
              )
            else ...[
              for (var index = 0; index < _records.length; index += 1)
                _buildRecordRow(
                  _records[index],
                  key: index == 0
                      ? _firstRowKey
                      : ValueKey<String>(_recordKey(_records[index])),
                ),
              _buildRecordFooter(),
            ],
            const SizedBox(height: 12),
            Text(
              widget.canWrite
                  ? '写操作会作为当前账号记入审计日志；重启 / OTA 前请确认目标设备。'
                  : '当前账号只读，仅展示数据与回执；如需下发指令请用 admin / operator 登录。',
              style: const TextStyle(fontSize: 12, color: Colors.black45),
            ),
          ],
        ),
      ),
    );
  }

  /// 历史上报的页脚：加载中 / 可加载更早 / 已到底，三种状态都可读。
  Widget _buildRecordFooter() {
    if (_loadingMore) {
      return const Padding(
        padding: EdgeInsets.symmetric(vertical: 10),
        child: Row(
          mainAxisAlignment: MainAxisAlignment.center,
          children: [
            SizedBox(
              width: 14,
              height: 14,
              child: CircularProgressIndicator(strokeWidth: 2),
            ),
            SizedBox(width: 8),
            Text(
              '正在加载更早数据…',
              style: TextStyle(fontSize: 12, color: Colors.black45),
            ),
          ],
        ),
      );
    }
    if (!_hasMore) {
      return const Padding(
        padding: EdgeInsets.symmetric(vertical: 10),
        child: Center(
          child: Text(
            '没有更早数据',
            style: TextStyle(fontSize: 12, color: Colors.black45),
          ),
        ),
      );
    }
    return Center(
      child: TextButton.icon(
        onPressed: _loadOlder,
        icon: const Icon(Icons.history, size: 16),
        label: const Text('加载更早'),
      ),
    );
  }

  Widget _buildRecordRow(SensorReading record, {Key? key}) {
    return Container(
      key: key,
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.symmetric(horizontal: 10, vertical: 8),
      decoration: BoxDecoration(
        color: Colors.black.withValues(alpha: 0.03),
        borderRadius: BorderRadius.circular(8),
      ),
      child: Row(
        children: [
          Expanded(
            child: Text(
              '${record.temperature.toStringAsFixed(1)}℃ / '
              '${record.humidity.toStringAsFixed(0)}% / ${record.battery}%',
              style: const TextStyle(fontSize: 13),
            ),
          ),
          Text(
            _formatTime(record.ts),
            style: const TextStyle(fontSize: 12, color: Colors.black45),
          ),
        ],
      ),
    );
  }

  Widget _buildCommandRow(CommandItem command) {
    final color = switch (command.state) {
      'success' => Colors.green,
      'failed' => Colors.red,
      _ => Colors.blueGrey,
    };
    return Container(
      margin: const EdgeInsets.only(bottom: 8),
      padding: const EdgeInsets.all(10),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.06),
        borderRadius: BorderRadius.circular(8),
        border: Border.all(color: color.withValues(alpha: 0.25)),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Row(
            children: [
              Expanded(
                child: Text(
                  '${command.type} · ${command.state}',
                  style: TextStyle(
                    fontSize: 13,
                    fontWeight: FontWeight.w600,
                    color: color,
                  ),
                ),
              ),
              if (command.progress > 0)
                Text(
                  '${command.progress}%',
                  style: const TextStyle(fontSize: 12, color: Colors.black54),
                ),
            ],
          ),
          const SizedBox(height: 4),
          Text(
            '${command.message ?? '无回执说明'} · ${command.operator} · '
            '${_formatTime(command.createdAt)}',
            style: const TextStyle(fontSize: 12, color: Colors.black45),
          ),
        ],
      ),
    );
  }
}

/// 设置弹窗返回值：网关地址 + 自定义实时地址（空串表示自动推导）。
class _GatewaySettings {
  const _GatewaySettings({required this.baseUrl, required this.realtimeUrl});

  final String baseUrl;
  final String realtimeUrl;
}

/// 顶部状态筛选：全部 / 在线 / 离线 / 有告警。
enum _BoardStatusFilter {
  all('全部'),
  online('在线'),
  offline('离线'),
  alert('有告警');

  const _BoardStatusFilter(this.label);

  final String label;
}

String _formatTime(DateTime? time) {
  if (time == null) {
    return '—';
  }
  final local = time.toLocal();
  String two(int value) => value.toString().padLeft(2, '0');
  return '${local.year}-${two(local.month)}-${two(local.day)} '
      '${two(local.hour)}:${two(local.minute)}:${two(local.second)}';
}
