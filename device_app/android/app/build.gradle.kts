import java.io.FileInputStream
import java.util.Properties

plugins {
    id("com.android.application")
    // The Flutter Gradle Plugin must be applied after the Android and Kotlin Gradle plugins.
    id("dev.flutter.flutter-gradle-plugin")
}

// ---- Release 签名材料解析（P1-5 / v1.22.0）----
// 优先级：环境变量（CI）> android/key.properties（本地，已 gitignore）。
// 两者都不存在时不会生成 release 签名配置：release 构建回落到 debug 签名，
// 并在 Gradle 日志中明确告警，禁止把该产物当作正式发布包。
val keystorePropertiesFile = rootProject.file("key.properties")
val keystoreProperties = Properties().apply {
    if (keystorePropertiesFile.exists()) {
        FileInputStream(keystorePropertiesFile).use { load(it) }
    }
}

val envStorePath = System.getenv("ANDROID_KEYSTORE_PATH")
val envStorePassword = System.getenv("ANDROID_KEYSTORE_PASSWORD")
val envKeyAlias = System.getenv("ANDROID_KEY_ALIAS")
val envKeyPassword = System.getenv("ANDROID_KEY_PASSWORD")
val hasEnvKeystore = !envStorePath.isNullOrBlank() &&
    !envStorePassword.isNullOrBlank() &&
    !envKeyAlias.isNullOrBlank() &&
    !envKeyPassword.isNullOrBlank()

// 环境变量只配了一部分时直接报错，避免静默回落到 debug 签名。
val envVarsPresent = listOf(envStorePath, envStorePassword, envKeyAlias, envKeyPassword)
    .any { !it.isNullOrBlank() }
if (envVarsPresent && !hasEnvKeystore) {
    val missing = listOf(
        "ANDROID_KEYSTORE_PATH" to envStorePath,
        "ANDROID_KEYSTORE_PASSWORD" to envStorePassword,
        "ANDROID_KEY_ALIAS" to envKeyAlias,
        "ANDROID_KEY_PASSWORD" to envKeyPassword,
    ).filter { it.second.isNullOrBlank() }.map { it.first }
    throw GradleException("签名环境变量不完整，缺少：${missing.joinToString(" / ")}。")
}

val hasPropsKeystore = keystorePropertiesFile.exists() &&
    !keystoreProperties.getProperty("storeFile").isNullOrBlank() &&
    !keystoreProperties.getProperty("storePassword").isNullOrBlank() &&
    !keystoreProperties.getProperty("keyAlias").isNullOrBlank() &&
    !keystoreProperties.getProperty("keyPassword").isNullOrBlank()

// key.properties 存在但字段不全（例如只复制了模板未填写）时给出可读错误。
if (!hasEnvKeystore && keystorePropertiesFile.exists() && !hasPropsKeystore) {
    throw GradleException(
        "android/key.properties 存在，但 storeFile/storePassword/keyAlias/keyPassword 不完整；" +
            "请参考 key.properties.example 补齐。",
    )
}

val signingSource: String? = when {
    hasEnvKeystore -> "env"
    hasPropsKeystore -> "properties"
    else -> null
}
val releaseSigningAvailable = signingSource != null

// 严格模式：设置 REQUIRE_SIGNED_RELEASE=true（或 -PrequireSignedRelease=true）时，
// 缺少签名材料直接失败，而不是产出 debug 签名的替代包。
val requireSignedRelease =
    System.getenv("REQUIRE_SIGNED_RELEASE")?.toBoolean() == true ||
        (project.findProperty("requireSignedRelease") as String?)?.toBoolean() == true

val releaseStoreFile: File? = when (signingSource) {
    "env" -> rootProject.file(envStorePath!!)
    "properties" -> rootProject.file(keystoreProperties.getProperty("storeFile"))
    else -> null
}

if (releaseSigningAvailable && releaseStoreFile?.exists() != true) {
    throw GradleException(
        "release 签名配置指向的 keystore 不存在：${releaseStoreFile?.path}。\n" +
            "请检查 ANDROID_KEYSTORE_PATH 或 android/key.properties 的 storeFile" +
            "（相对路径以 device_app/android 为基准）。",
    )
}

if (!releaseSigningAvailable && requireSignedRelease) {
    throw GradleException(
        "release 构建要求签名，但未找到签名材料。\n" +
            "请二选一：\n" +
            "  1) 设置环境变量 ANDROID_KEYSTORE_PATH / ANDROID_KEYSTORE_PASSWORD / " +
            "ANDROID_KEY_ALIAS / ANDROID_KEY_PASSWORD；\n" +
            "  2) 在 device_app/android/ 下创建 key.properties（参考 key.properties.example）。\n" +
            "详见 docs/mobile-release.md。",
    )
}

android {
    namespace = "com.platojobs.iot_device_app"
    compileSdk = flutter.compileSdkVersion
    ndkVersion = flutter.ndkVersion

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    defaultConfig {
        applicationId = "com.platojobs.iot_device_app"
        // versionCode / versionName 来自 pubspec.yaml 的 `version: 1.x.y+build`：
        // versionName = 1.x.y，versionCode = build。发布时手动递增 build。
        minSdk = flutter.minSdkVersion
        targetSdk = flutter.targetSdkVersion
        // Uses the version code from pubspec.yaml. When using split APKs, 1000 * ABI_VERSION
        // is added automatically by Flutter. (https://developer.android.com/studio/build/configure-apk-splits#configure-APK-versions)
        // You can force using the value of versionCode by specifying the `-P force-version-code-ignoring-abi=true`
        // flag during build.
        versionCode = flutter.versionCode
        versionName = flutter.versionName
    }

    signingConfigs {
        if (releaseSigningAvailable) {
            create("release") {
                if (signingSource == "env") {
                    storeFile = releaseStoreFile
                    storePassword = envStorePassword
                    keyAlias = envKeyAlias
                    keyPassword = envKeyPassword
                } else {
                    storeFile = releaseStoreFile
                    storePassword = keystoreProperties.getProperty("storePassword")
                    keyAlias = keystoreProperties.getProperty("keyAlias")
                    keyPassword = keystoreProperties.getProperty("keyPassword")
                }
            }
        }
    }

    buildTypes {
        release {
            if (releaseSigningAvailable) {
                signingConfig = signingConfigs.getByName("release")
                logger.lifecycle(
                    "release 签名：使用${if (signingSource == "env") "环境变量" else "key.properties"}提供的 keystore",
                )
            } else {
                // 兜底：明确使用 debug 签名，并在日志与 CI 产物名中标出，绝不静默冒充发布包。
                signingConfig = signingConfigs.getByName("debug")
                logger.warn(
                    "WARNING: 未配置 release 签名材料，release 构建回落到 debug 签名。" +
                        "该产物仅用于内部测试，不可分发/上架；正式发布请按 docs/mobile-release.md 配置 keystore。",
                )
            }
        }
    }
}

kotlin {
    compilerOptions {
        jvmTarget = org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17
    }
}

flutter {
    source = "../.."
}
