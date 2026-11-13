import 'package:easy_refresh/easy_refresh.dart';

/// 统一的下拉刷新 / 上拉加载外观。
///
/// 用 `easy_refresh`（3.5.1，近 12 个月有发布，且官方明确 4.x 面向
/// material_ui 新包、flutter/material 应用应使用 3.x）。页面只引用这里的
/// header/footer，避免各页复制文案与样式。
ClassicHeader appRefreshHeader() => const ClassicHeader(
  dragText: '下拉刷新',
  armedText: '释放刷新',
  readyText: '刷新中…',
  processingText: '刷新中…',
  processedText: '刷新完成',
  failedText: '刷新失败',
  noMoreText: '没有更多了',
  showMessage: false,
);

ClassicFooter appRefreshFooter({String noMoreText = '没有更多了'}) => ClassicFooter(
  dragText: '上拉加载',
  armedText: '释放加载',
  readyText: '加载中…',
  processingText: '加载中…',
  processedText: '加载完成',
  failedText: '加载失败',
  noMoreText: noMoreText,
  showMessage: false,
);
