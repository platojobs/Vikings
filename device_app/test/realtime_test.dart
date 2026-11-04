import 'dart:async';
import 'dart:convert';

import 'package:flutter_test/flutter_test.dart';
import 'package:iot_device_app/models/realtime_payload.dart';
import 'package:iot_device_app/services/board_realtime_service.dart';
import 'package:iot_device_app/services/realtime_transport.dart';

/// 假连接：可指定首次连接失败，也可手动推送报文 / 模拟掉线。
class _FakeTransport implements RealtimeTransport {
  _FakeTransport({this.failConnect = false});

  final bool failConnect;
  final StreamController<RealtimeMessage> messagesController =
      StreamController<RealtimeMessage>.broadcast();
  final StreamController<void> disconnectedController =
      StreamController<void>.broadcast();

  int connectCalls = 0;

  @override
  Stream<RealtimeMessage> get messages => messagesController.stream;

  @override
  Stream<void> get disconnected => disconnectedController.stream;

  @override
  Future<void> connect() async {
    connectCalls += 1;
    if (failConnect) {
      throw const RealtimeConnectException('模拟连接失败');
    }
  }

  void push(String topic, Object payload) {
    messagesController.add(
      RealtimeMessage(topic, payload is String ? payload : jsonEncode(payload)),
    );
  }

  void drop() {
    disconnectedController.add(null);
  }

  @override
  Future<void> disconnect() async {
    if (!messagesController.isClosed) {
      await messagesController.close();
    }
    if (!disconnectedController.isClosed) {
      await disconnectedController.close();
    }
  }
}

Future<void> _waitFor(
  bool Function() predicate, {
  Duration timeout = const Duration(seconds: 3),
}) async {
  final deadline = DateTime.now().add(timeout);
  while (!predicate()) {
    if (DateTime.now().isAfter(deadline)) {
      fail('等待条件超时');
    }
    await Future<void>.delayed(const Duration(milliseconds: 5));
  }
}

void main() {
  group('实时报文解析', () {
    const parser = RealtimeParser();

    test('device/system/alert 解析为告警事件', () {
      final event = parser.parse(
        'device/system/alert',
        jsonEncode(<String, dynamic>{
          'alertId': 'alt-1',
          'deviceId': 'dev-0001',
          'level': 'critical',
          'metric': 'voltage',
          'value': 2.91,
          'threshold': 3.2,
          'message': '电压过低，请更换电池',
          'aiGenerated': true,
          'ts': 1762137600000,
        }),
      );

      expect(event, isA<AlertEvent>());
      final alert = (event as AlertEvent).alert;
      expect(alert.alertId, 'alt-1');
      expect(alert.critical, isTrue);
      expect(alert.aiGenerated, isTrue);
      expect(alert.status, 'open');
      expect(alert.createdAt?.isUtc, isTrue);
    });

    test('sensor/report 解析为最新值，且缺失设备 ID 时回退 Topic', () {
      final event = parser.parse(
        'device/dev-0002/sensor/report',
        jsonEncode(<String, dynamic>{
          'temperature': 24.6,
          'humidity': 55,
          'voltage': 3.72,
          'rssi': -63,
          'battery': 86,
          'seq': 1024,
          'ts': 1762137600000,
        }),
      );

      expect(event, isA<ReportEvent>());
      final reading = (event as ReportEvent).reading;
      expect(reading.deviceId, 'dev-0002');
      expect(reading.battery, 86);
      expect(reading.temperature, 24.6);
    });

    test('status 解析为上下线事件，非法状态被丢弃', () {
      final online = parser.parse(
        'device/dev-0001/status',
        jsonEncode(<String, dynamic>{
          'deviceId': 'dev-0001',
          'status': 'online',
          'reason': 'boot',
        }),
      );
      final invalid = parser.parse(
        'device/dev-0001/status',
        jsonEncode(<String, dynamic>{'deviceId': 'dev-0001', 'status': 'x'}),
      );

      expect(online, isA<StatusEvent>());
      expect((online as StatusEvent).online, isTrue);
      expect(invalid, isNull);
    });

    test('device/system/group 解析为分组变更事件', () {
      final event = parser.parse(
        'device/system/group',
        jsonEncode(<String, dynamic>{
          'deviceId': 'dev-0001',
          'groupName': '二号车间',
          'previousGroupName': '一号车间',
          'operator': 'admin',
          'ts': 1762243200000,
        }),
      );

      expect(event, isA<GroupChangedEvent>());
      final group = event as GroupChangedEvent;
      expect(group.deviceId, 'dev-0001');
      expect(group.groupName, '二号车间');
      expect(group.previousGroupName, '一号车间');
      expect(group.operator, 'admin');
    });

    test('分组事件缺 deviceId 或 groupName 类型不对时返回 null', () {
      expect(
        parser.parse(
          'device/system/group',
          jsonEncode(<String, dynamic>{'groupName': '一号车间'}),
        ),
        isNull,
      );
      expect(
        parser.parse(
          'device/system/group',
          jsonEncode(<String, dynamic>{'deviceId': 'dev-1'}),
        ),
        isNull,
      );
      expect(
        parser.parse(
          'device/system/group',
          jsonEncode(<String, dynamic>{'deviceId': 'dev-1', 'groupName': 1}),
        ),
        isNull,
      );
    });

    test('非 JSON、缺关键字段与无关 Topic 均返回 null', () {
      expect(parser.parse('device/system/alert', 'not-json'), isNull);
      expect(
        parser.parse('device/system/alert', jsonEncode(<String, dynamic>{'deviceId': 'dev-1'}),
        ),
        isNull,
      );
      expect(parser.parse('device/dev-1/cmd', jsonEncode(<String, dynamic>{})), isNull);
    });
  });

  group('实时连接与断线重连', () {
    test('首次连接失败后自动重试并恢复为已连接', () async {
      final transports = <_FakeTransport>[];
      final service = BoardRealtimeService(
        url: 'ws://127.0.0.1:9001',
        retryBase: const Duration(milliseconds: 2),
        retryMax: const Duration(milliseconds: 50),
        transportFactory: (_, _) {
          final transport = _FakeTransport(failConnect: transports.isEmpty);
          transports.add(transport);
          return transport;
        },
      );
      final states = <RealtimeState>[];
      service.state.addListener(() => states.add(service.state.value));

      service.start();
      await _waitFor(() => service.state.value == RealtimeState.connected);

      expect(transports.length, 2);
      expect(states.first, RealtimeState.connecting);
      expect(states, contains(RealtimeState.reconnecting));
      expect(service.failure.value, isNull);
      expect(service.attempts.value, 0);

      await service.dispose();
    });

    test('连接失败后进入重连态并启用轮询降级', () async {
      final service = BoardRealtimeService(
        url: 'ws://127.0.0.1:9001',
        retryBase: const Duration(milliseconds: 300),
        transportFactory: (_, _) => _FakeTransport(failConnect: true),
      );

      service.start();
      await _waitFor(() => service.state.value == RealtimeState.reconnecting);

      expect(service.state.value.requiresPollingFallback, isTrue);
      expect(service.failure.value, contains('模拟连接失败'));
      expect(service.attempts.value, 1);

      await service.dispose();
    });

    test('轮询降级判定：仅连接态不降级，其余状态均需轮询', () {
      expect(RealtimeState.connected.requiresPollingFallback, isFalse);
      expect(RealtimeState.connecting.requiresPollingFallback, isFalse);
      expect(RealtimeState.reconnecting.requiresPollingFallback, isTrue);
      expect(RealtimeState.stopped.requiresPollingFallback, isTrue);
      expect(RealtimeState.idle.requiresPollingFallback, isFalse);
    });

    test('连接后掉线会重新建连，并持续投递实时事件', () async {
      final transports = <_FakeTransport>[];
      final service = BoardRealtimeService(
        url: 'ws://127.0.0.1:9001',
        retryBase: const Duration(milliseconds: 2),
        transportFactory: (_, _) {
          final transport = _FakeTransport();
          transports.add(transport);
          return transport;
        },
      );
      final events = <RealtimeEvent>[];
      final states = <RealtimeState>[];
      service.state.addListener(() => states.add(service.state.value));
      final sub = service.events.listen(events.add);

      service.start();
      await _waitFor(() => service.state.value == RealtimeState.connected);
      expect(transports.length, 1);

      transports.single.push('device/system/alert', <String, dynamic>{
        'alertId': 'alt-2',
        'deviceId': 'dev-0001',
        'level': 'warning',
        'metric': 'humidity',
        'value': 90,
        'threshold': 85,
        'message': '湿度过高',
        'aiGenerated': false,
        'ts': 1762137600000,
      });
      await _waitFor(() => events.isNotEmpty);
      expect(events.single, isA<AlertEvent>());

      transports.single.drop();
      await _waitFor(
        () =>
            service.state.value == RealtimeState.connected &&
            transports.length == 2,
      );

      expect(states, contains(RealtimeState.reconnecting));
      expect(service.failure.value, isNull);

      await sub.cancel();
      await service.dispose();
    });
  });
}
