import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:iot_device_app/widgets/app_feedback.dart';

/// 与 main.dart 一致：最外层包 toastification，MaterialApp 挂根导航 key。
Widget _host({Widget? home}) => MaterialApp(
  navigatorKey: AppFeedback.navigatorKey,
  home: home ?? const Scaffold(body: SizedBox.shrink()),
);

/// 让 toast 的入场动画与关闭后的 50ms 清理窗口都走完，避免 pending timer。
Future<void> _settleToast(WidgetTester tester) async {
  await tester.pump();
  await tester.pumpAndSettle();
  await tester.pump(const Duration(milliseconds: 100));
}

void main() {
  TestWidgetsFlutterBinding.ensureInitialized();

  setUp(AppFeedback.dismissAll);
  tearDown(AppFeedback.dismissAll);

  test('四类提示的时长策略固定且可区分', () {
    expect(
      AppFeedback.durationOf(FeedbackKind.success, 'ok'),
      const Duration(seconds: 2),
    );
    expect(
      AppFeedback.durationOf(FeedbackKind.info, 'ok'),
      const Duration(seconds: 3),
    );
    expect(
      AppFeedback.durationOf(FeedbackKind.warning, 'ok'),
      const Duration(seconds: 4),
    );
    expect(
      AppFeedback.durationOf(FeedbackKind.error, 'short'),
      const Duration(seconds: 6),
    );
    // 长文本的失败提示多留 2 秒，便于阅读
    expect(
      AppFeedback.durationOf(FeedbackKind.error, 'x' * 81),
      const Duration(seconds: 8),
    );
  });

  testWidgets('四类提示使用不同的语义底色', (tester) async {
    await tester.pumpWidget(_host());
    final context = tester.element(find.byType(Scaffold));

    final expected = <FeedbackKind, Color>{
      FeedbackKind.success: const Color(0xFF2E7D32),
      FeedbackKind.error: const Color(0xFFC62828),
      FeedbackKind.warning: const Color(0xFFEF6C00),
      FeedbackKind.info: const Color(0xFF37474F),
    };
    void emit(FeedbackKind kind, String message) {
      switch (kind) {
        case FeedbackKind.success:
          AppFeedback.success(context, message);
        case FeedbackKind.error:
          AppFeedback.error(context, message);
        case FeedbackKind.warning:
          AppFeedback.warning(context, message);
        case FeedbackKind.info:
          AppFeedback.info(context, message);
      }
    }

    for (final entry in expected.entries) {
      AppFeedback.dismissAll();
      await tester.pump();
      emit(entry.key, '文案-${entry.key.name}');
      await _settleToast(tester);
      expect(find.byType(AppToastCard), findsOneWidget);
      final card = tester.widget<AppToastCard>(find.byType(AppToastCard));
      expect(card.kind, entry.key);
      expect(card.background, entry.value, reason: entry.key.name);
    }
    AppFeedback.dismissAll();
    await _settleToast(tester);
  });

  testWidgets('连续同类提示合并为一条并标注次数，不排队', (tester) async {
    await tester.pumpWidget(_host());
    final context = tester.element(find.byType(Scaffold));

    AppFeedback.error(context, '网络超时', mergeKey: 'net');
    await tester.pump();
    AppFeedback.error(context, '网络超时', mergeKey: 'net');
    await _settleToast(tester);

    expect(find.text('网络超时（×2）'), findsOneWidget);
    expect(find.byType(AppToastCard), findsOneWidget);
    AppFeedback.dismissAll();
    await _settleToast(tester);
  });

  testWidgets('连续多条错误提示串行展示，不丢失也不互相覆盖', (tester) async {
    await tester.pumpWidget(_host());
    final context = tester.element(find.byType(Scaffold));

    AppFeedback.error(context, '错误一：连接中断');
    AppFeedback.error(context, '错误二：指令超时');
    AppFeedback.error(context, '错误三：权限不足');
    await _settleToast(tester);

    // 第一条独占展示，后续两条排队等待，不会被挤掉
    expect(find.text('错误一：连接中断'), findsOneWidget);
    expect(find.text('错误二：指令超时'), findsNothing);
    expect(find.byType(AppToastCard), findsOneWidget);

    // 第一条 6s 自动关闭后，第二条自动接上
    await tester.pump(const Duration(seconds: 6, milliseconds: 400));
    await _settleToast(tester);
    expect(find.text('错误二：指令超时'), findsOneWidget);
    expect(find.text('错误一：连接中断'), findsNothing);

    // 第三条仍在队列里，继续接上
    await tester.pump(const Duration(seconds: 6, milliseconds: 400));
    await _settleToast(tester);
    expect(find.text('错误三：权限不足'), findsOneWidget);

    AppFeedback.dismissAll();
    await _settleToast(tester);
  });

  testWidgets('错误在页面替换后仍可见（根导航兜底）', (tester) async {
    await tester.pumpWidget(
      _host(
        home: Builder(
          builder: (context) => ElevatedButton(
            onPressed: () {
              AppFeedback.error(context, '登录已失效，请重新登录');
              Navigator.of(context).pushReplacement(
                MaterialPageRoute<void>(
                  builder: (_) => const Scaffold(body: Text('登录页')),
                ),
              );
            },
            child: const Text('触发'),
          ),
        ),
      ),
    );

    await tester.tap(find.text('触发'));
    await tester.pumpAndSettle();

    expect(find.text('登录页'), findsOneWidget);
    expect(find.text('登录已失效，请重新登录'), findsOneWidget);
    AppFeedback.dismissAll();
    await _settleToast(tester);
  });
}
