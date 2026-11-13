import 'dart:async';

import 'package:flutter/material.dart';
import 'package:toastification/toastification.dart';

/// 提示语义：成功 / 失败 / 警告 / 中性。
///
/// 全 App 只允许通过 [AppFeedback] 发瞬时提示，页面不再各自拼 SnackBar 或
/// 私有 `_showError`。实现基于 `toastification`（3.2.2，近 12 个月有发布、
/// 与当前 SDK 兼容），后续如需换实现只改本文件。
enum FeedbackKind { success, error, warning, info }

/// 统一的瞬时提示入口。
///
/// 语义与时长：
/// - 成功：2s，绿底，自动消失；
/// - 中性：3s，深灰底；
/// - 警告：4s，橙底；
/// - 失败：6s（长文本 8s），红底，带关闭按钮，可选操作按钮；
/// - 连续同类（相同 [mergeKey]）在 3 秒内合并为一条并标注次数，不排队刷屏；
/// - 不同内容的提示串行展示，前一条（尤其是错误）不会被后一条挤掉。
class AppFeedback {
  AppFeedback._();

  /// 根导航 key：页面销毁后（如 401 已切到登录页）仍能显示提示。
  static final GlobalKey<NavigatorState> navigatorKey =
      GlobalKey<NavigatorState>();

  static const Duration mergeWindow = Duration(seconds: 3);
  static const Duration _animationDuration = Duration(milliseconds: 220);

  static const Map<FeedbackKind, Color> _colors = {
    FeedbackKind.success: Color(0xFF2E7D32),
    FeedbackKind.error: Color(0xFFC62828),
    FeedbackKind.warning: Color(0xFFEF6C00),
    FeedbackKind.info: Color(0xFF37474F),
  };

  static const Map<FeedbackKind, IconData> _icons = {
    FeedbackKind.success: Icons.check_circle_outline,
    FeedbackKind.error: Icons.error_outline,
    FeedbackKind.warning: Icons.warning_amber_rounded,
    FeedbackKind.info: Icons.info_outline,
  };

  /// 当前正在展示的提示；非空时新提示进入队列，避免互相覆盖。
  static ToastificationItem? _current;
  static _PendingToast? _active;
  static final List<_PendingToast> _queue = <_PendingToast>[];

  static String? _lastKey;
  static DateTime _lastAt = DateTime.fromMillisecondsSinceEpoch(0);
  static int _repeat = 0;

  static Color colorOf(FeedbackKind kind) => _colors[kind]!;

  static IconData iconOf(FeedbackKind kind) => _icons[kind]!;

  static Duration durationOf(FeedbackKind kind, String message) {
    switch (kind) {
      case FeedbackKind.success:
        return const Duration(seconds: 2);
      case FeedbackKind.info:
        return const Duration(seconds: 3);
      case FeedbackKind.warning:
        return const Duration(seconds: 4);
      case FeedbackKind.error:
        return Duration(seconds: message.length > 80 ? 8 : 6);
    }
  }

  static void success(
    BuildContext context,
    String message, {
    String? mergeKey,
    Duration? duration,
  }) => _show(
    context,
    FeedbackKind.success,
    message,
    mergeKey: mergeKey,
    duration: duration,
  );

  static void info(
    BuildContext context,
    String message, {
    String? mergeKey,
    Duration? duration,
  }) => _show(
    context,
    FeedbackKind.info,
    message,
    mergeKey: mergeKey,
    duration: duration,
  );

  static void warning(
    BuildContext context,
    String message, {
    String? mergeKey,
    Duration? duration,
  }) => _show(
    context,
    FeedbackKind.warning,
    message,
    mergeKey: mergeKey,
    duration: duration,
  );

  static void error(
    BuildContext context,
    String message, {
    String? mergeKey,
    Duration? duration,
    String? actionLabel,
    VoidCallback? onAction,
  }) => _show(
    context,
    FeedbackKind.error,
    message,
    mergeKey: mergeKey,
    duration: duration,
    actionLabel: actionLabel,
    onAction: onAction,
  );

  /// 关闭当前提示与排队中的提示；测试与登出场景可主动清理，避免遗留计时器。
  static void dismissAll() {
    _queue.clear();
    final item = _current;
    _current = null;
    _active = null;
    if (item != null) {
      toastification.dismiss(item, showRemoveAnimation: false);
    }
    toastification.dismissAll(delayForAnimation: false);
    _lastKey = null;
    _repeat = 0;
    _lastAt = DateTime.fromMillisecondsSinceEpoch(0);
  }

  /// 供操作按钮或关闭按钮使用：只关掉当前这条，后续排队提示接着展示。
  static void dismissCurrent() {
    final item = _current;
    if (item == null) {
      return;
    }
    _advance(item);
  }

  static void _show(
    BuildContext? context,
    FeedbackKind kind,
    String message, {
    String? mergeKey,
    Duration? duration,
    String? actionLabel,
    VoidCallback? onAction,
  }) {
    // 优先用根导航的 context：页面被替换后提示仍挂在根 Overlay 上。
    // 未挂 navigatorKey 的场景（个别宿主）退回到传入的页面 context。
    if (navigatorKey.currentContext == null && context == null) {
      return;
    }

    final key = mergeKey ?? '${kind.name}|$message';
    final now = DateTime.now();
    if (_lastKey == key && now.difference(_lastAt) < mergeWindow) {
      _repeat += 1;
    } else {
      _repeat = 1;
    }
    _lastKey = key;
    _lastAt = now;
    final text = _repeat > 1 ? '$message（×$_repeat）' : message;

    final pending = _PendingToast(
      kind: kind,
      message: text,
      mergeKey: key,
      duration: duration ?? durationOf(kind, message),
      actionLabel: actionLabel,
      onAction: onAction,
      fallback: context,
    );

    // 同一 mergeKey 在同一窗口内直接替换那一条，保持「合并而不是排队」。
    if (_active != null && _active!.mergeKey == key) {
      _replace(pending);
      return;
    }
    final queued = _queue.indexWhere((item) => item.mergeKey == key);
    if (queued >= 0) {
      _queue[queued] = pending;
      return;
    }
    _queue.add(pending);
    _drain();
  }

  static void _replace(_PendingToast pending) {
    final old = _current;
    _current = null;
    _active = null;
    _present(pending);
    if (old != null) {
      // 先让新条目进列表，再撤掉旧的，避免 Overlay 中途被回收。
      WidgetsBinding.instance.addPostFrameCallback(
        (_) => toastification.dismiss(old, showRemoveAnimation: false),
      );
    }
  }

  static void _drain() {
    if (_current != null || _queue.isEmpty) {
      return;
    }
    _present(_queue.removeAt(0));
  }

  /// 结束当前这条：有排队就先把下一条插进列表，再移除旧的。
  ///
  /// toastification 在最后一条移除后会延迟回收 Overlay；若先移除再展示，
  /// 新条目可能落进已回收的 Overlay，导致后续错误提示永远不显示。
  static void _advance(ToastificationItem old) {
    if (!identical(_current, old)) {
      return;
    }
    _current = null;
    _active = null;

    if (_queue.isEmpty) {
      toastification.dismiss(old);
      return;
    }
    _present(_queue.removeAt(0));
    WidgetsBinding.instance.addPostFrameCallback(
      (_) => toastification.dismiss(old),
    );
  }

  static void _present(_PendingToast pending) {
    final overlay = navigatorKey.currentState?.overlay;
    final fallback = pending.fallback;
    final context = (fallback != null && fallback.mounted) ? fallback : null;
    if (overlay == null && context == null) {
      return;
    }

    _active = pending;
    late final ToastificationItem item;
    item = toastification.showCustom(
      overlayState: overlay,
      context: overlay == null ? context : null,
      alignment: Alignment.bottomCenter,
      animationDuration: _animationDuration,
      // 自动关闭交给卡片内的计时器，便于在两条提示交接时保证先后顺序。
      autoCloseDuration: null,
      builder: (context, holder) => AppToastCard(
        kind: pending.kind,
        message: pending.message,
        actionLabel: pending.actionLabel,
        onAction: () {
          dismissCurrent();
          pending.onAction?.call();
        },
        onClose: dismissCurrent,
        showClose: pending.kind == FeedbackKind.error || pending.isLong,
        // 计时器挂在卡片上：卡片销毁即取消，不会留下悬空计时器。
        autoCloseAfter: pending.duration,
        onAutoClose: () => _advance(item),
      ),
    );
    _current = item;
    // toastification 的 showCustom 只注册 post-frame 回调、不主动调度帧；
    // 若此刻没有其他动画，新提示会一直等不到首帧，这里显式请求一帧。
    WidgetsBinding.instance.ensureVisualUpdate();
  }
}

class _PendingToast {
  _PendingToast({
    required this.kind,
    required this.message,
    required this.mergeKey,
    required this.duration,
    required this.fallback,
    this.actionLabel,
    this.onAction,
  });

  final FeedbackKind kind;
  final String message;
  final String mergeKey;
  final Duration duration;
  final BuildContext? fallback;
  final String? actionLabel;
  final VoidCallback? onAction;

  bool get isLong => message.length > 80;
}

/// 单条提示的外观：语义底色 + 图标 + 可换行长文案 + 可选操作/关闭。
///
/// 由 [AppFeedback] 交给 toastification 渲染，页面不直接使用。
class AppToastCard extends StatefulWidget {
  const AppToastCard({
    super.key,
    required this.kind,
    required this.message,
    this.actionLabel,
    this.onAction,
    this.onClose,
    this.showClose = false,
    this.autoCloseAfter,
    this.onAutoClose,
  });

  final FeedbackKind kind;
  final String message;
  final String? actionLabel;
  final VoidCallback? onAction;
  final VoidCallback? onClose;
  final bool showClose;

  /// 展示多久后自动关闭；为空表示一直保留（由关闭按钮结束）。
  final Duration? autoCloseAfter;

  /// 自动关闭到期时回调，用于让 [AppFeedback] 接着展示队列里的下一条。
  final VoidCallback? onAutoClose;

  /// 语义底色；深色模式下会压暗一档，保证与深色背景有边界。
  Color get background => AppFeedback.colorOf(kind);

  @override
  State<AppToastCard> createState() => _AppToastCardState();
}

class _AppToastCardState extends State<AppToastCard> {
  Timer? _timer;

  @override
  void initState() {
    super.initState();
    _startTimer();
  }

  @override
  void didUpdateWidget(covariant AppToastCard oldWidget) {
    super.didUpdateWidget(oldWidget);
    if (oldWidget.autoCloseAfter != widget.autoCloseAfter) {
      _startTimer();
    }
  }

  void _startTimer() {
    _timer?.cancel();
    _timer = null;
    final delay = widget.autoCloseAfter;
    if (delay == null) {
      return;
    }
    _timer = Timer(delay, () => widget.onAutoClose?.call());
  }

  @override
  void dispose() {
    _timer?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    final isDark = theme.brightness == Brightness.dark;
    final color = isDark
        ? Color.alphaBlend(
            Colors.black.withValues(alpha: 0.18),
            widget.background,
          )
        : widget.background;
    return Container(
      margin: const EdgeInsets.symmetric(horizontal: 12, vertical: 6),
      padding: EdgeInsets.fromLTRB(
        16,
        12,
        widget.showClose || widget.actionLabel != null ? 6 : 16,
        12,
      ),
      decoration: BoxDecoration(
        color: color,
        borderRadius: BorderRadius.circular(10),
        border: isDark ? Border.all(color: Colors.white24) : null,
        boxShadow: isDark
            ? null
            : <BoxShadow>[
                BoxShadow(
                  color: Colors.black.withValues(alpha: 0.18),
                  blurRadius: 12,
                  offset: const Offset(0, 4),
                ),
              ],
      ),
      child: Row(
        children: [
          Icon(AppFeedback.iconOf(widget.kind), color: Colors.white, size: 20),
          const SizedBox(width: 10),
          Expanded(
            child: Semantics(
              liveRegion: true,
              child: Text(
                widget.message,
                // 不限制行数：长文案自动换行，不截断
                style: const TextStyle(
                  color: Colors.white,
                  fontSize: 13.5,
                  height: 1.35,
                ),
              ),
            ),
          ),
          if (widget.actionLabel != null)
            TextButton(
              onPressed: widget.onAction,
              style: TextButton.styleFrom(foregroundColor: Colors.white),
              child: Text(
                widget.actionLabel!,
                style: const TextStyle(fontWeight: FontWeight.w600),
              ),
            ),
          if (widget.showClose)
            IconButton(
              onPressed: widget.onClose,
              tooltip: '关闭',
              iconSize: 18,
              visualDensity: VisualDensity.compact,
              icon: const Icon(Icons.close, color: Colors.white70),
            ),
        ],
      ),
    );
  }
}

/// 页面内的持久提示（加载失败、只读禁用、登录失效说明）。
///
/// 这类信息需要一直可见，不能用会自动消失的 toast；
/// 统一在这里定义视觉，页面只传语义。
class AppInlineMessage extends StatelessWidget {
  const AppInlineMessage({
    super.key,
    required this.message,
    this.kind = FeedbackKind.info,
    this.actionLabel,
    this.onAction,
  });

  final String message;
  final FeedbackKind kind;
  final String? actionLabel;
  final VoidCallback? onAction;

  @override
  Widget build(BuildContext context) {
    final color = AppFeedback.colorOf(kind);
    return Container(
      margin: const EdgeInsets.only(bottom: 10),
      padding: const EdgeInsets.fromLTRB(12, 8, 8, 8),
      decoration: BoxDecoration(
        color: color.withValues(alpha: 0.08),
        borderRadius: BorderRadius.circular(10),
        border: Border.all(color: color.withValues(alpha: 0.3)),
      ),
      child: Row(
        children: [
          Icon(AppFeedback.iconOf(kind), color: color, size: 18),
          const SizedBox(width: 8),
          Expanded(
            child: Semantics(
              liveRegion: true,
              child: Text(
                message,
                style: TextStyle(color: color, fontSize: 13, height: 1.4),
              ),
            ),
          ),
          if (actionLabel != null)
            TextButton(onPressed: onAction, child: Text(actionLabel!)),
        ],
      ),
    );
  }
}
