# 移动端发布（Android）

> 适用版本：v1.22.0 起。相关实现：`device_app/android/app/build.gradle.kts`、
> `.github/workflows/ci.yml`（`device_app_release` 作业）、`.github/dependabot.yml`。

## 0. 结论与签名策略

- **有密钥**：CI secrets（或本地 `key.properties` / 环境变量）提供 keystore 时，
  release 构建使用正式的 `release` 签名配置。
- **没密钥**：不静默使用 debug 密钥冒充发布包。构建**继续成功**（保证没有 secrets 的
  main CI 不会长期变红），但：
  - Gradle 日志打印 `WARNING: 未配置 release 签名材料...不可分发`；
  - CI 产物名带 `-debug-signed`，`SIGNING.txt` 写明「仅限内部测试，不可分发」；
  - 同时支持严格模式：仓库变量 `REQUIRE_SIGNED_RELEASE=true` 时直接失败并给出可读提示。
- 选择「明确降级 + 标注」而不是「默认失败」的理由：真实密钥无法由代码仓库自行生成，
  强制失败会让所有未配 secrets 的 main 构建长期红灯；显式标注 + 严格模式开关兼顾
  可用性与安全性。**任何 `-debug-signed` 产物都不允许对外分发或上架。**

## 1. 版本号与构建号规则

`device_app/pubspec.yaml` 的 `version: 1.x.y+build` 是唯一版本源：

| 字段 | 取值 | 示例 |
| --- | --- | --- |
| `versionName` | `+` 之前的部分 | `1.22.0` |
| `versionCode` | `+` 之后的 build | `21` |

**正式发布（标签构建）**

- 发版前手动把 `version` 改成新的 `1.x.y+build`，build 必须比上一发布版本大。
- CI 在标签构建时校验两件事，任一不满足直接失败：
  1. 标签 `vX.Y.Z` 与 pubspec 的版本名一致；
  2. 当前 build 大于最近一个 `v*` 标签里的 build（防重号/倒退）。
- 不依赖 `GITHUB_RUN_NUMBER`，避免正式包的 versionCode 被 CI 运行次数绑架。

**main 快照构建（非发布）**

- `versionName = 1.x.y-snapshot.<前 7 位 SHA>`；
- `versionCode = GITHUB_RUN_NUMBER + 100000`，同一快照通道严格递增、不会重号。
- 快照版 versionCode（100xxx 起）远大于正式版（21 起）；因此**从快照包切回正式包时，
  需要先卸载快照包**（Android 不允许降级安装）。正式渠道用户不受影响。

## 2. 生成 keystore（一次性）

在 `device_app/android/` 下执行（口令用密码管理器生成，不要用文档里的示例）：

```bash
cd device_app/android
keytool -genkeypair -v -keystore release-keystore.jks -storetype JKS \
  -keyalg RSA -keysize 4096 -validity 10000 -alias iot-release
```

安全要求：

- `release-keystore.jks`、`key.properties` 已被 `.gitignore` 忽略，**禁止提交**；
- keystore 丢失后无法在应用商店更新同一个应用，必须把 keystore 文件和口令
  一起备份到受控的密码管理器/密钥库；
- 口令只通过环境变量或 `key.properties` 注入，Gradle 日志、CI 日志都不打印口令；
- 仓库示例中的口令一律是 `<PLACEHOLDER>` 形式的占位符。

## 3. 本地出包

### 3.1 方式 A：key.properties（推荐给日常本地构建）

```bash
cp device_app/android/key.properties.example device_app/android/key.properties
# 编辑 key.properties：storeFile/storePassword/keyAlias/keyPassword 换成真实值
```

`storeFile` 的相对路径以 `device_app/android/` 为基准（例如 keystore 就放在该目录时
填 `release-keystore.jks`）。

```bash
cd device_app
flutter build apk --release
flutter build appbundle --release
```

### 3.2 方式 B：环境变量（CI / 临时验证）

```bash
export ANDROID_KEYSTORE_PATH=/absolute/path/release-keystore.jks
export ANDROID_KEYSTORE_PASSWORD='<STORE_PASSWORD>'
export ANDROID_KEY_ALIAS='<KEY_ALIAS>'
export ANDROID_KEY_PASSWORD='<KEY_PASSWORD>'
cd device_app && flutter build apk --release
```

优先级：环境变量 > `key.properties`。两者都缺失时走第 0 节的降级路径；
设置 `REQUIRE_SIGNED_RELEASE=true`（或 Gradle 参数 `-PrequireSignedRelease=true`）可强制失败。

## 4. 在 CI 配置签名

在 GitHub 仓库 **Settings → Secrets and variables → Actions** 添加：

| 名称 | 内容 |
| --- | --- |
| `ANDROID_KEYSTORE_BASE64` | `base64 -i release-keystore.jks \| tr -d '\n'` 的输出 |
| `ANDROID_KEYSTORE_PASSWORD` | keystore 口令 |
| `ANDROID_KEY_ALIAS` | 例如 `iot-release` |
| `ANDROID_KEY_PASSWORD` | key 口令（JKS 下与 store 口令相同即可） |

可选仓库变量（Variables）：

| 名称 | 值 | 作用 |
| --- | --- | --- |
| `REQUIRE_SIGNED_RELEASE` | `true` | 没有签名 secrets 时让发布作业失败，而不是产出 debug 签名包 |

配置后，CI 把 keystore 解码到 `$RUNNER_TEMP/keystore/release.jks`（权限 600），
通过环境变量传给 Gradle，构建结束随 runner 销毁。

## 5. 触发条件与产物

CI 作业 `device_app_release` 只在以下场景运行：

- `push` 到 `main`（快照包，便于内测分发）；
- `push` 版本标签 `v*`（发布包）。

**PR 不构建**：避免每次评审都上传几十 MB 制品、拉长反馈时间。
标签构建额外执行第 1 节的版本一致性/递增校验。

产物（artifact，保留 14 天）：

```text
iot-device-app-<versionName>-<signed|debug-signed>.apk
iot-device-app-<versionName>-<signed|debug-signed>.aab
SHA256SUMS
SIGNING.txt   # versionName/versionCode/signing/commit/ref/note
```

示例名称：

- 有密钥：`iot-device-app-1.22.0-signed.apk`
- 无密钥：`iot-device-app-1.22.0-snapshot.abc1234-debug-signed.apk`

## 6. 下载与验签

```bash
gh run download <run-id> -n iot-device-app-1.22.0-signed
shasum -a 256 -c SHA256SUMS

# 证书指纹（应与你的 keystore 一致；debug 包会显示 CN=Android Debug）
apksigner verify --print-certs iot-device-app-1.22.0-signed.apk

# 版本号自检
aapt2 dump badging iot-device-app-1.22.0-signed.apk | head -1
```

`apksigner` 位于 `$ANDROID_SDK_ROOT/build-tools/<version>/apksigner`。
CI 的「校验签名与版本」步骤会把证书 DN 与 `package:` 行写进日志。

## 7. 依赖治理

### 7.1 Dependabot

`.github/dependabot.yml` 覆盖四个生态：`pub`（`/device_app`）、`pip`（`/gateway`）、
`npm`（`/web`）、`github-actions`（`/`）。

- 节奏：每周一 09:00（Asia/Shanghai）；
- 分组：每个生态的 minor/patch 合并成一个 PR，major 单独提；
- 每个生态最多 5 个开启中的 PR，避免一次开一堆。

### 7.2 漏洞扫描取舍

- 仓库不把 `npm audit` / `pip-audit` 设为阻断门禁：当前直接依赖较少，但上游
  漏洞披露频率不可控，硬门禁容易出现「与本次改动无关的长期红灯」。
- **基线方案**：由仓库管理员在 Settings → Code security 开启 Dependabot alerts
  与 security updates；高危漏洞会以独立 PR/告警出现，由维护者按周处理。
- 需要主动排查时手动执行（不阻断 CI）：

```bash
cd web && npm audit --audit-level=high
gateway/.venv/bin/pip install pip-audit && gateway/.venv/bin/pip-audit -r gateway/requirements.txt
```

若未来确需门禁，建议只在 `main` 的定时任务里跑，并显式允许已知例外（不要放进 PR 必过路径）。

## 8. 常见问题

| 现象 | 原因 / 处理 |
| --- | --- |
| Gradle 报「keystore 不存在」 | `storeFile` 相对路径以 `device_app/android/` 为基准，检查文件位置 |
| CI 产物名带 `debug-signed` | 未配置四个签名 secrets；按第 4 节配置后重跑 |
| `apksigner` 显示 `CN=Android Debug` | 同上，说明是内部测试包，不可分发 |
| 标签构建失败「版本不一致」 | 先改 `pubspec.yaml` 的 `version` 再打标签 |
| 标签构建失败「版本号倒退」 | 把 pubspec 的 build 增到大于上一发布版本 |
| 安装提示版本降级 | 从快照包（100xxx）切回正式包（2x）需先卸载快照包 |
