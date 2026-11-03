import 'package:flutter_test/flutter_test.dart';
import 'package:iot_device_app/main.dart';

void main() {
  testWidgets('设备页渲染核心控件', (WidgetTester tester) async {
    await tester.pumpWidget(const IotDeviceApp());

    expect(find.text('IoT 设备端模拟器'), findsOneWidget);
    expect(find.text('启动循环'), findsOneWidget);
    expect(find.text('手动上报'), findsOneWidget);
    expect(find.text('事件日志'), findsOneWidget);
  });
}
