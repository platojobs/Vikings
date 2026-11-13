import 'package:another_flushbar/flushbar.dart';
import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:iot_device_app/widgets/app_feedback.dart';

Widget _host({Widget? home}) => MaterialApp(
  navigatorKey: AppFeedback.navigatorKey,
  home: home ?? const Scaffold(body: SizedBox.shrink()),
);

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
      await tester.pump();
      await tester.pump(const Duration(milliseconds: 250));
      expect(find.byType(Flushbar), findsOneWidget);
      final bar = tester.widget<Flushbar>(find.byType(Flushbar));
      expect(bar.backgroundColor, entry.value, reason: entry.key.name);
    }
    AppFeedback.dismissAll();
    await tester.pump();
  });

  testWidgets('连续同类提示合并为一条并标注次数，不排队', (tester) async {
    await tester.pumpWidget(_host());
    final context = tester.element(find.byType(Scaffold));

    AppFeedback.error(context, '网络超时', mergeKey: 'net');
    await tester.pump();
    AppFeedback.error(context, '网络超时', mergeKey: 'net');
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 250));

    expect(find.text('网络超时（×2）'), findsOneWidget);
    expect(find.byType(Flushbar), findsOneWidget);
    AppFeedback.dismissAll();
    await tester.pump();
  });

  testWidgets('错误在页面替换后仍可见（根导航兜底）', (tester) async {
    await tester.pumpWidget(
      MaterialApp(
        navigatorKey: AppFeedback.navigatorKey,
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
    await tester.pumpAndSettle();
  });
}
