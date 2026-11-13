import 'dart:async';
import 'dart:convert';
import 'dart:typed_data';

import 'package:dio/dio.dart';
import 'package:iot_device_app/services/api_client.dart';

/// 命中 mock 适配器的一次请求快照（替代原先 `http.Request`）。
class CapturedRequest {
  CapturedRequest({
    required this.method,
    required this.uri,
    required Map<String, dynamic> rawHeaders,
    required this.body,
  }) : headers = <String, String>{
         for (final entry in rawHeaders.entries)
           entry.key.toLowerCase(): '${entry.value}',
       };

  final String method;
  final Uri uri;

  /// 键统一小写：dio 内部按 `Headers.contentTypeHeader` 写入，大小写不保证一致。
  final Map<String, String> headers;
  final String body;

  String? header(String name) => headers[name.toLowerCase()];
}

class MockResponse {
  MockResponse(this.body, {this.statusCode = 200, this.headers});

  /// 已编码的响应体文本；适配器统一按 UTF-8 转字节。
  final String body;
  final int statusCode;
  final Map<String, List<String>>? headers;
}

/// 让假适配器抛出指定 dio 失败类型（超时/连接失败），用于验证错误映射与重试。
class MockDioFailure implements Exception {
  const MockDioFailure(this.type, {this.message});

  final DioExceptionType type;
  final String? message;
}

/// dio 假适配器：记录请求并按回调返回响应，不产生真实网络 IO。
class MockDioAdapter implements HttpClientAdapter {
  MockDioAdapter(this.handler);

  final FutureOr<MockResponse> Function(CapturedRequest request) handler;

  final List<CapturedRequest> requests = <CapturedRequest>[];

  int get calls => requests.length;

  CapturedRequest get last => requests.last;

  @override
  Future<ResponseBody> fetch(
    RequestOptions options,
    Stream<Uint8List>? requestStream,
    Future<void>? cancelFuture,
  ) async {
    final builder = BytesBuilder(copy: false);
    if (requestStream != null) {
      await for (final chunk in requestStream) {
        builder.add(chunk);
      }
    }
    final captured = CapturedRequest(
      method: options.method,
      uri: options.uri,
      rawHeaders: options.headers,
      body: utf8.decode(builder.takeBytes(), allowMalformed: true),
    );
    requests.add(captured);

    final MockResponse response;
    try {
      response = await handler(captured);
    } on MockDioFailure catch (failure) {
      throw DioException(
        requestOptions: options,
        type: failure.type,
        message: failure.message,
      );
    }

    return ResponseBody.fromBytes(
      utf8.encode(response.body),
      response.statusCode,
      headers:
          response.headers ??
          <String, List<String>>{
            Headers.contentTypeHeader: <String>[
              'application/json; charset=utf-8',
            ],
          },
    );
  }

  @override
  void close({bool force = false}) {}
}

/// 用假适配器组装 [ApiClient]，其余配置走生产默认值。
ApiClient mockApiClient(
  MockDioAdapter adapter, {
  String baseUrl = 'http://api.example:8000',
  Duration timeout = const Duration(seconds: 8),
  SessionRefresher? refresher,
  RequestCancellation? cancellation,
  bool enableLogging = false,
}) {
  final dio = Dio()..httpClientAdapter = adapter;
  return ApiClient(
    baseUrl: baseUrl,
    dio: dio,
    timeout: timeout,
    refresher: refresher,
    cancellation: cancellation,
    enableLogging: enableLogging,
  );
}

MockResponse jsonResponse(Object body, [int statusCode = 200]) =>
    MockResponse(jsonEncode(body), statusCode: statusCode);

Map<String, dynamic> okEnvelope(Object? data) => <String, dynamic>{
  'code': 0,
  'message': 'ok',
  'data': data,
};
