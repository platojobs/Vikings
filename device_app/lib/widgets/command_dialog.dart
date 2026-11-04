import 'package:flutter/material.dart';

import '../models/command_request.dart';
import '../models/mobile_api_models.dart';
import '../services/api_client.dart';

/// 指令下发弹窗：填写参数 → 二次确认 → 展示网关回执。
///
/// 网络请求由外部注入的 [onSubmit] 负责（页面层统一走 ApiClient 与 401/403 处理），
/// 本组件只负责表单、参数校验、防重复提交与结果展示。
class CommandDialog extends StatefulWidget {
  const CommandDialog({
    super.key,
    required this.deviceId,
    required this.deviceName,
    required this.onSubmit,
  });

  final String deviceId;
  final String deviceName;
  final Future<CommandItem> Function(CommandRequest request) onSubmit;

  @override
  State<CommandDialog> createState() => _CommandDialogState();
}

enum _DialogStep { form, confirm, result }

class _CommandDialogState extends State<CommandDialog> {
  final TextEditingController _temperatureController = TextEditingController(
    text: '0',
  );
  final TextEditingController _humidityController = TextEditingController(
    text: '0',
  );
  final TextEditingController _reportIntervalController = TextEditingController(
    text: '30',
  );
  final TextEditingController _sleepController = TextEditingController(
    text: '15',
  );
  final TextEditingController _versionController = TextEditingController();
  final TextEditingController _urlController = TextEditingController();
  final TextEditingController _sizeController = TextEditingController();

  DeviceCommandType _type = DeviceCommandType.config;
  _DialogStep _step = _DialogStep.form;
  CommandRequest? _request;
  CommandItem? _result;
  String? _validationError;
  String? _submitError;
  bool _submitting = false;

  @override
  void dispose() {
    _temperatureController.dispose();
    _humidityController.dispose();
    _reportIntervalController.dispose();
    _sleepController.dispose();
    _versionController.dispose();
    _urlController.dispose();
    _sizeController.dispose();
    super.dispose();
  }

  void _next() {
    try {
      final request = buildCommandRequest(
        type: _type,
        temperatureOffset: _temperatureController.text,
        humidityOffset: _humidityController.text,
        reportIntervalSec: _reportIntervalController.text,
        sleepSec: _sleepController.text,
        targetVersion: _versionController.text,
        url: _urlController.text,
        size: _sizeController.text,
      );
      setState(() {
        _request = request;
        _validationError = null;
        _submitError = null;
        _step = _DialogStep.confirm;
      });
    } on CommandValidationException catch (error) {
      setState(() => _validationError = error.message);
    }
  }

  Future<void> _submit() async {
    final request = _request;
    if (request == null || _submitting) {
      return;
    }
    setState(() {
      _submitting = true;
      _submitError = null;
    });
    try {
      final result = await widget.onSubmit(request);
      if (!mounted) {
        return;
      }
      setState(() {
        _result = result;
        _step = _DialogStep.result;
      });
    } on ApiException catch (error) {
      if (mounted) {
        setState(() => _submitError = '提交失败：${error.userMessage}');
      }
    } catch (_) {
      if (mounted) {
        setState(() => _submitError = '提交失败，请稍后重试');
      }
    } finally {
      if (mounted) {
        setState(() => _submitting = false);
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    return AlertDialog(
      scrollable: true,
      title: Text(switch (_step) {
        _DialogStep.form => '下发指令',
        _DialogStep.confirm => '确认下发',
        _DialogStep.result => '指令已提交',
      }),
      content: switch (_step) {
        _DialogStep.form => _buildForm(),
        _DialogStep.confirm => _buildConfirm(),
        _DialogStep.result => _buildResult(),
      },
      actions: switch (_step) {
        _DialogStep.form => <Widget>[
          TextButton(
            onPressed: () => Navigator.of(context).pop(),
            child: const Text('取消'),
          ),
          FilledButton(onPressed: _next, child: const Text('下一步')),
        ],
        _DialogStep.confirm => <Widget>[
          TextButton(
            onPressed: _submitting
                ? null
                : () => setState(() {
                    _step = _DialogStep.form;
                    _submitError = null;
                  }),
            child: const Text('返回修改'),
          ),
          FilledButton(
            onPressed: _submitting ? null : _submit,
            child: _submitting
                ? const SizedBox(
                    width: 18,
                    height: 18,
                    child: CircularProgressIndicator(strokeWidth: 2),
                  )
                : const Text('确认下发'),
          ),
        ],
        _DialogStep.result => <Widget>[
          FilledButton(
            onPressed: () => Navigator.of(context).pop(_result),
            child: const Text('完成'),
          ),
        ],
      },
    );
  }

  Widget _buildForm() {
    return Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          '目标设备：${widget.deviceName}（${widget.deviceId}）',
          style: const TextStyle(fontSize: 13, color: Colors.black54),
        ),
        const SizedBox(height: 12),
        DropdownButtonFormField<DeviceCommandType>(
          initialValue: _type,
          decoration: const InputDecoration(
            labelText: '指令类型',
            border: OutlineInputBorder(),
          ),
          items: DeviceCommandType.values
              .map(
                (type) => DropdownMenuItem<DeviceCommandType>(
                  value: type,
                  child: Text(type.label),
                ),
              )
              .toList(growable: false),
          onChanged: (value) {
            if (value == null) {
              return;
            }
            setState(() {
              _type = value;
              _validationError = null;
            });
          },
        ),
        const SizedBox(height: 6),
        Text(
          _type.description,
          style: const TextStyle(fontSize: 12, color: Colors.black45),
        ),
        const SizedBox(height: 12),
        ..._buildFields(),
        if (_validationError != null) ...[
          const SizedBox(height: 10),
          Text(
            _validationError!,
            style: const TextStyle(color: Colors.red, fontSize: 12),
          ),
        ],
      ],
    );
  }

  List<Widget> _buildFields() {
    switch (_type) {
      case DeviceCommandType.calibrate:
        return <Widget>[
          _numberField(
            controller: _temperatureController,
            label: '温度偏移（℃）',
            hint: '例如 -1.5，范围 -20 ~ 20',
            signed: true,
          ),
          const SizedBox(height: 10),
          _numberField(
            controller: _humidityController,
            label: '湿度偏移（%RH）',
            hint: '例如 2，范围 -50 ~ 50',
            signed: true,
          ),
        ];
      case DeviceCommandType.config:
        return <Widget>[
          _numberField(
            controller: _reportIntervalController,
            label: '上报间隔（秒）',
            hint: '1 ~ 86400',
          ),
          const SizedBox(height: 10),
          _numberField(
            controller: _sleepController,
            label: '休眠时长（秒）',
            hint: '1 ~ 86400',
          ),
        ];
      case DeviceCommandType.reboot:
        return const <Widget>[
          Text(
            '重启会短暂中断设备上报，请确认目标设备。',
            style: TextStyle(fontSize: 13, color: Colors.black54),
          ),
        ];
      case DeviceCommandType.ota:
        return <Widget>[
          _textField(
            controller: _versionController,
            label: '目标版本',
            hint: '例如 1.1.0，最长 32 字符',
          ),
          const SizedBox(height: 10),
          _textField(
            controller: _urlController,
            label: '升级包地址（可选）',
            hint: 'https://…，最长 512 字符',
          ),
          const SizedBox(height: 10),
          _numberField(
            controller: _sizeController,
            label: '升级包大小（字节，可选）',
            hint: '正整数，可留空',
          ),
        ];
    }
  }

  Widget _buildConfirm() {
    final request = _request!;
    return Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        _summaryRow('目标设备', '${widget.deviceName}（${widget.deviceId}）'),
        const SizedBox(height: 8),
        _summaryRow('指令类型', request.type.label),
        const SizedBox(height: 8),
        _summaryRow('参数摘要', request.summary),
        const SizedBox(height: 12),
        Container(
          padding: const EdgeInsets.all(10),
          decoration: BoxDecoration(
            color: Colors.orange.withValues(alpha: 0.08),
            borderRadius: BorderRadius.circular(8),
          ),
          child: const Text(
            '提交后会立即通过 MQTT 下发到设备，并作为当前账号记入审计日志。'
            '请再次确认目标设备与参数。',
            style: TextStyle(fontSize: 12, color: Colors.black87),
          ),
        ),
        if (_submitError != null) ...[
          const SizedBox(height: 10),
          Text(
            _submitError!,
            style: const TextStyle(color: Colors.red, fontSize: 12),
          ),
        ],
      ],
    );
  }

  Widget _buildResult() {
    final result = _result!;
    return Column(
      mainAxisSize: MainAxisSize.min,
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        _summaryRow('指令 ID', result.cmdId),
        const SizedBox(height: 8),
        _summaryRow('当前状态', result.state),
        const SizedBox(height: 8),
        _summaryRow('网关说明', result.message ?? '无'),
        if (result.progress > 0) ...[
          const SizedBox(height: 8),
          _summaryRow('进度', '${result.progress}%'),
        ],
        const SizedBox(height: 12),
        const Text(
          '设备回执会异步推进状态，可在设备详情的「指令回执」中刷新查看。',
          style: TextStyle(fontSize: 12, color: Colors.black45),
        ),
      ],
    );
  }

  Widget _summaryRow(String label, String value) {
    return Row(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        SizedBox(
          width: 72,
          child: Text(
            label,
            style: const TextStyle(fontSize: 13, color: Colors.black54),
          ),
        ),
        Expanded(child: Text(value, style: const TextStyle(fontSize: 13))),
      ],
    );
  }

  Widget _textField({
    required TextEditingController controller,
    required String label,
    required String hint,
  }) {
    return TextField(
      controller: controller,
      enabled: !_submitting,
      decoration: InputDecoration(
        labelText: label,
        hintText: hint,
        border: const OutlineInputBorder(),
      ),
    );
  }

  Widget _numberField({
    required TextEditingController controller,
    required String label,
    required String hint,
    bool signed = false,
  }) {
    return TextField(
      controller: controller,
      enabled: !_submitting,
      decoration: InputDecoration(
        labelText: label,
        hintText: hint,
        border: const OutlineInputBorder(),
      ),
      keyboardType: TextInputType.numberWithOptions(
        decimal: true,
        signed: signed,
      ),
    );
  }
}
