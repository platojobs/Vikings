import '../models/mobile_api_models.dart';
import 'api_client.dart';

/// 网关 `POST /api/alerts/bulk-ack` 的单次上限（见 `AlertBulkAckRequest`）。
const int bulkAckBatchSize = 100;

/// 某一批提交失败：保留批次序号、该批 ID 与可读原因，便于摘要如实展示。
class BulkAckBatchFailure {
  const BulkAckBatchFailure({
    required this.batchNumber,
    required this.ids,
    required this.message,
    required this.kind,
  });

  /// 从 1 开始的分批序号。
  final int batchNumber;
  final List<String> ids;
  final String message;

  /// 失败类型：页面据此区分 403（禁用本轮写操作）与普通网络/服务端错误。
  final ApiErrorKind kind;
}

/// 分批批量确认的合并结果。
///
/// 成功批次按 ID 累加；失败批次单独记录，不把失败条数算进 `acked`，
/// 也不因单批失败回滚已成功批次。
class BulkAckRunResult {
  const BulkAckRunResult({
    required this.acked,
    required this.alreadyAcked,
    required this.notFound,
    required this.failedBatches,
    required this.submitted,
    required this.unsubmitted,
    required this.batchCount,
    required this.unauthorized,
  });

  final List<String> acked;
  final List<String> alreadyAcked;
  final List<String> notFound;
  final List<BulkAckBatchFailure> failedBatches;

  /// 实际发往网关的 ID 条数（失败批次也计入）。
  final int submitted;

  /// 因登录失效而中止、从未发出的 ID 条数。
  final int unsubmitted;

  /// 本次计划的分批数。
  final int batchCount;

  /// 是否因 401 中止；页面据此清会话回登录页。
  final bool unauthorized;

  int get failedCount =>
      failedBatches.fold(0, (sum, batch) => sum + batch.ids.length);
}

/// 按 [batchSize] 串行分批提交并合并结果。
///
/// 顺序与并发度：严格串行，第 N 批完成后再发第 N+1 批，避免对网关造成瞬时压力；
/// 单批失败只记录该批，继续尝试后续批次（401 除外：登录已失效，后续批次不再发出，
/// 计入 [BulkAckRunResult.unsubmitted]，页面随后清会话回登录页）。
Future<BulkAckRunResult> runBulkAckInBatches({
  required List<String> alertIds,
  required Future<BulkAckResult> Function(List<String> ids) submit,
  int batchSize = bulkAckBatchSize,
}) async {
  assert(batchSize > 0);
  final acked = <String>[];
  final alreadyAcked = <String>[];
  final notFound = <String>[];
  final failures = <BulkAckBatchFailure>[];
  var submitted = 0;
  var batchCount = 0;
  var unauthorized = false;

  for (var start = 0; start < alertIds.length; start += batchSize) {
    final end = start + batchSize < alertIds.length
        ? start + batchSize
        : alertIds.length;
    final batch = alertIds.sublist(start, end);
    batchCount += 1;
    submitted += batch.length;
    try {
      final result = await submit(batch);
      acked.addAll(result.acked);
      alreadyAcked.addAll(result.alreadyAcked);
      notFound.addAll(result.notFound);
    } on ApiException catch (error) {
      failures.add(
        BulkAckBatchFailure(
          batchNumber: batchCount,
          ids: batch,
          message: error.userMessage,
          kind: error.kind,
        ),
      );
      if (error.kind == ApiErrorKind.unauthorized) {
        unauthorized = true;
        break;
      }
    }
  }

  final unsubmitted = alertIds.length - submitted;
  return BulkAckRunResult(
    acked: acked,
    alreadyAcked: alreadyAcked,
    notFound: notFound,
    failedBatches: failures,
    submitted: submitted,
    unsubmitted: unsubmitted,
    batchCount: batchCount,
    unauthorized: unauthorized,
  );
}
