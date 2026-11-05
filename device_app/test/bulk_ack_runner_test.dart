import 'package:flutter_test/flutter_test.dart';
import 'package:iot_device_app/models/mobile_api_models.dart';
import 'package:iot_device_app/services/api_client.dart';
import 'package:iot_device_app/services/bulk_ack_runner.dart';

List<AlertItem> _alerts(int count) => List<AlertItem>.generate(
  count,
  (index) => AlertItem(
    alertId: 'alt-${index.toString().padLeft(3, '0')}',
    deviceId: 'dev-0001',
    level: 'warning',
    metric: 'temperature',
    value: 80,
    threshold: 75,
    message: 'temperature 越界',
    aiGenerated: false,
    status: 'open',
    createdAt: DateTime.utc(2026, 11, 4, 8),
  ),
);

void main() {
  test('150 条按 100 串行拆成 2 批，结果逐类累加', () async {
    final alerts = _alerts(150);
    final requestedBatches = <List<String>>[];
    final result = await runBulkAckInBatches(
      alertIds: alerts.map((item) => item.alertId).toList(),
      submit: (batch) async {
        requestedBatches.add(List<String>.of(batch));
        return BulkAckResult(
          acked: batch,
          alreadyAcked: const <String>[],
          notFound: const <String>[],
          requested: batch.length,
        );
      },
    );

    expect(requestedBatches.map((batch) => batch.length).toList(), <int>[100, 50]);
    expect(requestedBatches.first.first, 'alt-000');
    expect(requestedBatches.last.first, 'alt-100');
    expect(result.batchCount, 2);
    expect(result.acked.length, 150);
    expect(result.failedBatches, isEmpty);
    expect(result.submitted, 150);
    expect(result.unsubmitted, 0);
  });

  test('中途某批失败：保留已成功批次并在结果中标出失败批次', () async {
    var calls = 0;
    final result = await runBulkAckInBatches(
      alertIds: _alerts(150).map((item) => item.alertId).toList(),
      submit: (batch) async {
        calls += 1;
        if (calls == 2) {
          throw const ApiException(ApiErrorKind.network, '网络中断');
        }
        return BulkAckResult(
          acked: batch,
          alreadyAcked: const <String>[],
          notFound: const <String>[],
          requested: batch.length,
        );
      },
    );

    expect(result.batchCount, 2);
    expect(result.acked.length, 100);
    expect(result.failedBatches.single.batchNumber, 2);
    expect(result.failedBatches.single.ids.length, 50);
    expect(result.failedCount, 50);
    expect(result.submitted, 150);
    expect(result.unsubmitted, 0);
    expect(result.unauthorized, isFalse);
  });

  test('401 中止后续批次：未提交条数如实记录，不谎报成功', () async {
    var calls = 0;
    final result = await runBulkAckInBatches(
      alertIds: _alerts(250).map((item) => item.alertId).toList(),
      submit: (batch) async {
        calls += 1;
        if (calls == 2) {
          throw const ApiException(ApiErrorKind.unauthorized, '登录已失效');
        }
        return BulkAckResult(
          acked: batch,
          alreadyAcked: const <String>[],
          notFound: const <String>[],
          requested: batch.length,
        );
      },
    );

    expect(result.acked.length, 100);
    expect(result.failedBatches.single.batchNumber, 2);
    expect(result.unauthorized, isTrue);
    expect(result.submitted, 200);
    expect(result.unsubmitted, 50);
    expect(result.batchCount, 2);
  });
}
