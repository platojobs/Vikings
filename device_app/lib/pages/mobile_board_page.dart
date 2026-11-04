import 'dart:async';

import 'package:flutter/material.dart';

import '../config/api_config.dart';
import '../models/command_request.dart';
import '../models/mobile_api_models.dart';
import '../models/realtime_payload.dart';
import '../services/api_client.dart';
import '../services/app_storage.dart';
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
  const MobileBoardPage({super.key, this.active = true});

  /// 页面是否处于前台可见。切到设备模拟或 App 进入后台时置为 false，
  /// 用于断开实时连接、停止轮询省电。
  final bool active;

  @override
  State<MobileBoardPage> createState() => _MobileBoardPageState();
}

class _MobileBoardPageState extends State<MobileBoardPage>
    with WidgetsBindingObserver {
  final AppStorage _storage = AppStorage();

  AuthSession? _session;
  String _baseUrl = ApiConfig.defaultBaseUrl;
  List<DeviceSummary> _devices = const <DeviceSummary>[];
  List<SensorReading> _readings = const <SensorReading>[];
  List<AlertItem> _alerts = const <AlertItem>[];
  bool _booting = true;
  bool _loading = false;
  String? _error;
  String? _notice;

  /// 实时通道收到的最新在线状态，优先于 REST 快照。
  final Map<String, String> _liveStatus = <String, String>{};

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

  static const Duration _pollInterval = Duration(seconds: 15);

  @override
  void initState() {
    super.initState();
    WidgetsBinding.instance.addObserver(this);
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
    _pollTimer?.cancel();
    _eventSub?.cancel();
    unawaited(_realtime?.dispose());
    _realtime = null;
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
    if (_realtime == null) {
      _startRealtime();
    }
  }

  String get _effectiveRealtimeUrl => _realtimeUrlOverride.isNotEmpty
      ? _realtimeUrlOverride
      : ApiConfig.realtimeUrl(_baseUrl);

  void _startRealtime() {
    if (_session == null) {
      return;
    }
    _stopRealtime();
    final url = _effectiveRealtimeUrl;
    if (url.isEmpty) {
      return;
    }
    final service = BoardRealtimeService(
      url: url,
      transportFactory: (uri, clientId) =>
          MqttWebSocketTransport(url: uri, clientId: clientId),
    );
    _realtime = service;
    _eventSub = service.events.listen(_onRealtimeEvent);
    service.state.addListener(_onRealtimeStateChanged);
    service.start();
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
          _alerts = <AlertItem>[alert, ..._alerts].take(50).toList();
        });
      case ReportEvent(:final reading):
        setState(() {
          _readings = <SensorReading>[
            reading,
            ..._readings.where((item) => item.deviceId != reading.deviceId),
          ];
          _liveStatus[reading.deviceId] = 'online';
        });
      case StatusEvent(:final deviceId, :final status):
        setState(() => _liveStatus[deviceId] = status);
    }
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
    final client = ApiClient(baseUrl: _baseUrl);
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
    final client = ApiClient(baseUrl: _baseUrl);
    try {
      await client.ackAlert(token: session.token, alertId: alert.alertId);
      if (!mounted) {
        return;
      }
      setState(() {
        _alerts = _alerts
            .where((item) => item.alertId != alert.alertId)
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

  Future<void> _loadData({bool silent = false}) async {
    final session = _session;
    if (session == null) {
      return;
    }
    if (!silent) {
      setState(() {
        _loading = true;
        _error = null;
      });
    }

    final client = ApiClient(baseUrl: _baseUrl);
    try {
      final results = await Future.wait<Object>([
        client.fetchDevices(token: session.token),
        client.fetchLatestReadings(token: session.token),
        client.fetchOpenAlerts(token: session.token),
      ]);
      if (!mounted) {
        return;
      }
      setState(() {
        _devices = results[0] as List<DeviceSummary>;
        _readings = results[1] as List<SensorReading>;
        _alerts = results[2] as List<AlertItem>;
        // REST 快照比本地实时状态更新，清空后再由新事件补齐
        _liveStatus.clear();
        _error = null;
      });
    } on ApiException catch (error) {
      if (error.kind == ApiErrorKind.unauthorized) {
        await _handleExpired();
      } else if (mounted) {
        setState(() => _error = _describe(error));
      }
    } catch (_) {
      if (mounted) {
        setState(() => _error = '加载失败，请稍后重试');
      }
    } finally {
      client.close();
      if (mounted) {
        setState(() => _loading = false);
      }
    }
  }

  String _describe(ApiException error) {
    if (error.kind == ApiErrorKind.network ||
        error.kind == ApiErrorKind.timeout) {
      return '${error.userMessage}\n当前网关：$_baseUrl';
    }
    return error.userMessage;
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
      _loading = false;
      _error = null;
      _writeBlockedReason = null;
      _ackingAlertId = null;
    });
  }

  Future<void> _logout() async {
    _stopRealtime();
    _stopPolling();
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
      );
    }
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
              const SizedBox(height: 14),
              _buildSectionTitle('设备与实时值'),
              if (_loading && _devices.isEmpty)
                const Padding(
                  padding: EdgeInsets.symmetric(vertical: 24),
                  child: Center(child: CircularProgressIndicator()),
                ),
              if (!_loading && _devices.isEmpty)
                _buildEmpty('暂无设备。请先在 Web 后台注册设备，或启动设备模拟器上报。'),
              ..._devices.map(_buildDeviceCard),
              const SizedBox(height: 8),
              _buildSectionTitle('未处理告警（${_alerts.length}）'),
              if (_alerts.isEmpty) _buildEmpty('当前没有未处理告警'),
              ..._alerts.map(_buildAlertCard),
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

  Widget _buildSummary() {
    final online = _devices.where(_isOnline).length;
    return Container(
      padding: const EdgeInsets.symmetric(vertical: 14, horizontal: 8),
      decoration: BoxDecoration(
        color: Colors.white,
        borderRadius: BorderRadius.circular(12),
        border: Border.all(color: const Color(0xFFE2E8F0)),
      ),
      child: Row(
        children: [
          _summaryItem('设备总数', '${_devices.length}'),
          _summaryItem('在线', '$online'),
          _summaryItem('未处理告警', '${_alerts.length}'),
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
    required this.onIssueCommand,
  });

  final DeviceSummary device;
  final SensorReading? reading;
  final bool online;
  final String baseUrl;
  final String token;

  /// 当前会话是否可写（viewer 或服务端 403 后为 false）。
  final bool canWrite;

  /// 页面层统一处理指令弹窗、提交与 401/403，返回网关回执。
  final Future<CommandItem?> Function() onIssueCommand;

  @override
  State<_DeviceDetailSheet> createState() => _DeviceDetailSheetState();
}

class _DeviceDetailSheetState extends State<_DeviceDetailSheet> {
  List<CommandItem> _commands = const <CommandItem>[];
  List<SensorReading> _records = const <SensorReading>[];
  bool _loading = true;
  bool _issuing = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    _loadDetail();
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

  Future<void> _loadDetail() async {
    final client = ApiClient(baseUrl: widget.baseUrl);
    try {
      final results = await Future.wait<Object>([
        client.fetchCommands(
          token: widget.token,
          deviceId: widget.device.deviceId,
        ),
        client.fetchDeviceRecords(
          token: widget.token,
          deviceId: widget.device.deviceId,
        ),
      ]);
      if (mounted) {
        setState(() {
          _commands = results[0] as List<CommandItem>;
          _records = results[1] as List<SensorReading>;
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

  @override
  Widget build(BuildContext context) {
    final reading = widget.reading;
    return SafeArea(
      child: SizedBox(
        height: MediaQuery.sizeOf(context).height * 0.72,
        child: ListView(
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
              '最近上报（最多 10 条）',
              style: TextStyle(fontSize: 15, fontWeight: FontWeight.w600),
            ),
            const SizedBox(height: 8),
            if (_loading)
              const Padding(
                padding: EdgeInsets.symmetric(vertical: 12),
                child: Center(child: CircularProgressIndicator()),
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
            else
              ..._records.map(_buildRecordRow),
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

  Widget _buildRecordRow(SensorReading record) {
    return Container(
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

String _formatTime(DateTime? time) {
  if (time == null) {
    return '—';
  }
  final local = time.toLocal();
  String two(int value) => value.toString().padLeft(2, '0');
  return '${local.year}-${two(local.month)}-${two(local.day)} '
      '${two(local.hour)}:${two(local.minute)}:${two(local.second)}';
}
