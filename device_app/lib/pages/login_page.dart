import 'package:flutter/material.dart';

import '../config/api_config.dart';
import '../models/mobile_api_models.dart';
import '../services/api_client.dart';
import '../services/app_storage.dart';

/// 移动看板登录页：账号密码登录网关，token 与会话持久化到本地。
///
/// 登录只复用 `/api/auth/login`，不新增后端接口；账号为管理后台同一套。
class LoginPage extends StatefulWidget {
  const LoginPage({
    super.key,
    required this.initialBaseUrl,
    required this.onLoggedIn,
    this.notice,
  });

  final String initialBaseUrl;
  final void Function(AuthSession session, String baseUrl) onLoggedIn;

  /// 由看板页传入的提示，例如「登录已失效，请重新登录」。
  final String? notice;

  @override
  State<LoginPage> createState() => _LoginPageState();
}

class _LoginPageState extends State<LoginPage> {
  final AppStorage _storage = AppStorage();
  late final TextEditingController _baseUrlController;
  late final TextEditingController _usernameController;
  late final TextEditingController _passwordController;

  bool _submitting = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    _baseUrlController = TextEditingController(text: widget.initialBaseUrl);
    _usernameController = TextEditingController(text: 'admin');
    _passwordController = TextEditingController();
  }

  @override
  void dispose() {
    _baseUrlController.dispose();
    _usernameController.dispose();
    _passwordController.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    FocusScope.of(context).unfocus();
    final rawBaseUrl = _baseUrlController.text;
    final username = _usernameController.text.trim();
    final password = _passwordController.text;

    if (!ApiConfig.isValid(rawBaseUrl)) {
      setState(() => _error = '网关地址无效，请填写形如 http://192.168.1.8:8000 的地址');
      return;
    }
    if (username.isEmpty || password.isEmpty) {
      setState(() => _error = '请输入用户名与密码');
      return;
    }

    final baseUrl = ApiConfig.normalize(rawBaseUrl);
    setState(() {
      _submitting = true;
      _error = null;
    });

    final client = ApiClient(baseUrl: baseUrl);
    try {
      final session = await client.login(username: username, password: password);
      try {
        await _storage.saveBaseUrl(baseUrl);
        await _storage.saveSession(session);
      } catch (_) {
        // 安全存储不可用（如桌面缺 libsecret）时仍放行本次登录，
        // 仅表现为重启后需重新登录，不影响看板使用
      }
      if (!mounted) {
        return;
      }
      widget.onLoggedIn(session, baseUrl);
    } on ApiException catch (error) {
      if (mounted) {
        setState(() {
          _error = error.kind == ApiErrorKind.network ||
                  error.kind == ApiErrorKind.timeout
              ? '${error.userMessage}\n当前地址：$baseUrl'
              : error.userMessage;
        });
      }
    } finally {
      client.close();
      if (mounted) {
        setState(() => _submitting = false);
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    final theme = Theme.of(context);
    return Scaffold(
      appBar: AppBar(title: const Text('移动看板 · 登录'), centerTitle: false),
      body: SafeArea(
        child: ListView(
          padding: const EdgeInsets.all(16),
          children: [
            if (widget.notice != null)
              Container(
                margin: const EdgeInsets.only(bottom: 12),
                padding: const EdgeInsets.all(12),
                decoration: BoxDecoration(
                  color: Colors.orange.withValues(alpha: 0.1),
                  borderRadius: BorderRadius.circular(10),
                ),
                child: Row(
                  children: [
                    const Icon(Icons.info_outline, color: Colors.orange, size: 18),
                    const SizedBox(width: 8),
                    Expanded(
                      child: Text(
                        widget.notice!,
                        style: const TextStyle(color: Colors.orange, fontSize: 13),
                      ),
                    ),
                  ],
                ),
              ),
            Text('只读移动看板', style: theme.textTheme.titleLarge),
            const SizedBox(height: 6),
            Text(
              '登录后查看设备实时值、未处理告警与指令回执；数据来自网关 REST，'
              '设备模拟器（本地模式）不受影响。',
              style: theme.textTheme.bodySmall?.copyWith(color: Colors.black54),
            ),
            const SizedBox(height: 20),
            TextField(
              controller: _baseUrlController,
              decoration: const InputDecoration(
                labelText: '网关地址',
                hintText: 'http://192.168.1.8:8000',
                prefixIcon: Icon(Icons.dns_outlined),
                border: OutlineInputBorder(),
              ),
              keyboardType: TextInputType.url,
              textInputAction: TextInputAction.next,
            ),
            const SizedBox(height: 8),
            Align(
              alignment: Alignment.centerLeft,
              child: TextButton.icon(
                onPressed: () {
                  _baseUrlController.text = ApiConfig.defaultBaseUrl;
                },
                icon: const Icon(Icons.restore, size: 16),
                label: const Text('使用当前平台默认地址'),
              ),
            ),
            const SizedBox(height: 8),
            TextField(
              controller: _usernameController,
              decoration: const InputDecoration(
                labelText: '用户名',
                prefixIcon: Icon(Icons.person_outline),
                border: OutlineInputBorder(),
              ),
              textInputAction: TextInputAction.next,
            ),
            const SizedBox(height: 12),
            TextField(
              controller: _passwordController,
              obscureText: true,
              onSubmitted: (_) => _submit(),
              decoration: const InputDecoration(
                labelText: '密码',
                prefixIcon: Icon(Icons.lock_outline),
                border: OutlineInputBorder(),
              ),
            ),
            const SizedBox(height: 8),
            Text(
              '演示账号：admin / admin123（以 gateway/.env 为准）',
              style: theme.textTheme.bodySmall?.copyWith(color: Colors.black45),
            ),
            if (_error != null) ...[
              const SizedBox(height: 12),
              Container(
                padding: const EdgeInsets.all(10),
                decoration: BoxDecoration(
                  color: Colors.red.withValues(alpha: 0.08),
                  borderRadius: BorderRadius.circular(8),
                  border: Border.all(color: Colors.red.withValues(alpha: 0.3)),
                ),
                child: Text(
                  _error!,
                  style: const TextStyle(color: Colors.red, fontSize: 13),
                ),
              ),
            ],
            const SizedBox(height: 20),
            FilledButton.icon(
              onPressed: _submitting ? null : _submit,
              icon: _submitting
                  ? const SizedBox(
                      width: 16,
                      height: 16,
                      child: CircularProgressIndicator(strokeWidth: 2),
                    )
                  : const Icon(Icons.login),
              label: Text(_submitting ? '登录中…' : '登录并进入看板'),
            ),
            const SizedBox(height: 24),
            _buildAddressHint(theme),
          ],
        ),
      ),
    );
  }

  Widget _buildAddressHint(ThemeData theme) {
    return Container(
      padding: const EdgeInsets.all(12),
      decoration: BoxDecoration(
        color: Colors.black.withValues(alpha: 0.03),
        borderRadius: BorderRadius.circular(10),
      ),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        children: [
          Text('地址怎么填', style: theme.textTheme.titleSmall),
          const SizedBox(height: 6),
          const Text(
            '· iOS 模拟器：http://127.0.0.1:8000\n'
            '· Android 模拟器：http://10.0.2.2:8000\n'
            '· 真机：http://<电脑局域网 IP>:8000，需与电脑同一 Wi-Fi\n'
            '注意：真机上 127.0.0.1 指向手机自身，连不到电脑。',
            style: TextStyle(fontSize: 12, height: 1.6, color: Colors.black54),
          ),
        ],
      ),
    );
  }
}
