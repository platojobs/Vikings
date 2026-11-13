import 'dart:async';

import 'package:another_flushbar/flushbar.dart';
import 'package:flutter/material.dart';

/// 提示语义：成功 / 失败 / 警告 / 中性。
///
/// 全 App 只允许通过 [AppFeedback] 发瞬时提示，页面不再各自拼 SnackBar 或
/// 私有 `_showError`。实现基于 `another_flushbar`（近 12 个月有发布、与当前
/// SDK 兼容），后续如需换实现只改本文件。
enum FeedbackKind { success, error, warning, info }

/// 统一的瞬时提示入口。
///
/// 语义与时长：
/// - 成功：2s，绿底，自动消失；
/// - 中性：3s，深灰底；
/// - 警告：4s，橙底，可滑动关闭；
/// - 失败：6s（长文本 8s），红底，带关闭按钮，可选操作按钮；
/// - 连续同类（相同 [mergeKey]）在 3 秒内合并为一条并标注次数，不排队刷屏。
class AppFeedback {
  AppFeedback._();

  /// 根导航 key：页面销毁后（如 401 已切到登录页）仍能显示提示。
  static final GlobalKey<NavigatorState> navigatorKey =
      GlobalKey<NavigatorState>();

  static const Duration mergeWindow = Duration(seconds: 3);

  static Flushbar? _current;
  static String? _lastKey;
  static DateTime _lastAt = DateTime.fromMillisecondsSinceEpoch(0);
  static int _repeat = 0;

  /// 待展示的提示：同一事件循环内多次调用只展示最后一条（就是合并后的那条）。
  ///
  /// 延后一个微任务再入栈有两个作用：
  /// 1. 不会在 Navigator 正在处理路由时 pop/push，撞上 `_debugLocked` 断言；
  /// 2. 保证「先跳转、后提示」，401 场景里 pushReplacement 到登录页后
  ///    提示才入栈，不会被新页面替换掉。
  static Flushbar? _pending;
  static BuildContext? _pendingFallback;
  static bool _scheduled = false;

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

  /// 关闭当前提示；测试与登出场景可主动清理，避免遗留计时器。
  static void dismissAll() {
    _pending = null;
    _pendingFallback = null;
    _remove(_current);
    _current = null;
    _lastKey = null;
    _repeat = 0;
    _lastAt = DateTime.fromMillisecondsSinceEpoch(0);
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

    final isLong = message.length > 80;
    final effective = duration ?? durationOf(kind, message);
    late final Flushbar bar;
    bar = Flushbar(
      messageText: Row(
        children: [
          Icon(_icons[kind], color: Colors.white, size: 20),
          const SizedBox(width: 10),
          Expanded(
            child: Semantics(
              liveRegion: true,
              child: Text(
                text,
                style: const TextStyle(color: Colors.white, fontSize: 13.5),
              ),
            ),
          ),
        ],
      ),
      duration: effective,
      isDismissible: true,
      dismissDirection: kind == FeedbackKind.error || isLong
          ? FlushbarDismissDirection.HORIZONTAL
          : FlushbarDismissDirection.VERTICAL,
      backgroundColor: _colors[kind]!,
      margin: const EdgeInsets.all(12),
      padding: const EdgeInsets.symmetric(horizontal: 16, vertical: 12),
      borderRadius: BorderRadius.circular(10),
      flushbarPosition: FlushbarPosition.BOTTOM,
      flushbarStyle: FlushbarStyle.FLOATING,
      animationDuration: const Duration(milliseconds: 220),
      mainButton: actionLabel == null
          ? null
          : TextButton(
              onPressed: () {
                dismissCurrent();
                onAction?.call();
              },
              child: Text(
                actionLabel,
                style: const TextStyle(
                  color: Colors.white,
                  fontWeight: FontWeight.w600,
                ),
              ),
            ),
      onStatusChanged: (status) {
        if (status == FlushbarStatus.DISMISSED && identical(_current, bar)) {
          _current = null;
        }
      },
    );

    // 同一事件循环内只保留最后一条，下个微任务再入栈。
    _pending = bar;
    _pendingFallback = context;
    if (_scheduled) {
      return;
    }
    _scheduled = true;
    scheduleMicrotask(() {
      _scheduled = false;
      final next = _pending;
      final fallback = _pendingFallback;
      _pending = null;
      _pendingFallback = null;
      if (next != null) {
        _present(next, fallback);
      }
    });
  }

  /// 移除旧提示并展示新提示。
  static void _present(Flushbar bar, BuildContext? fallback) {
    final context =
        navigatorKey.currentContext ??
        ((fallback != null && fallback.mounted) ? fallback : null);
    if (context == null) {
      return;
    }
    _remove(_current);
    _current = bar;
    unawaited(bar.show(context));
  }

  /// 直接摘除路由，不走 pop 动画，避免 Navigator 历史更新过程中的断言。
  static void _remove(Flushbar? bar) {
    if (bar == null) {
      return;
    }
    final route = bar.flushbarRoute;
    if (route != null && route.isActive) {
      route.navigator?.removeRoute(route);
    }
    if (identical(_current, bar)) {
      _current = null;
    }
  }

  /// 供操作按钮回调使用：关闭当前提示。
  static void dismissCurrent() {
    _remove(_current);
    _current = null;
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
    final color = AppFeedback._colors[kind]!;
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
          Icon(AppFeedback._icons[kind], color: color, size: 18),
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
