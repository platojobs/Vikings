"""网关配置：全部通过环境变量注入，禁止在代码里硬编码连接信息。"""

from functools import lru_cache
from pathlib import Path
from typing import List, Optional
from urllib.parse import urlsplit

from pydantic_settings import BaseSettings, SettingsConfigDict

# 仓库内公开过的 JWT 默认值：生产环境一律视为不安全，即使长度达标
KNOWN_WEAK_JWT_SECRETS = frozenset(
    {
        "change-me-in-production",
        "iot-platform-dev-secret-key-please-change-in-prod",
        "secret",
        "dev-secret",
        "jwt-secret",
        "your-secret-key",
    }
)

# 常见弱口令（含本仓库演示账号）与默认数据库口令
KNOWN_WEAK_PASSWORDS = frozenset(
    {
        "admin123",
        "operator123",
        "viewer123",
        "password",
        "password123",
        "123456",
        "12345678",
        "123456789",
        "qwerty",
        "abc123",
        "letmein",
        "welcome",
        "changeme",
        "test1234",
    }
)
KNOWN_WEAK_DB_PASSWORDS = frozenset({"iot_pass", "postgres", "password", "admin123"})


class Settings(BaseSettings):
    """集中式配置对象。

    读取顺序：进程环境变量 > gateway/.env > 默认值。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- 运行环境：development / test / production ----
    # production 下启动会校验密钥、口令与数据库，仍使用默认值直接拒绝启动
    app_env: str = "development"

    # ---- 数据库：默认 SQLite 保证零依赖可跑，生产通过 .env 指向 PostgreSQL ----
    database_url: str = "sqlite+aiosqlite:///./data/iot.db"

    # ---- MQTT ----
    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = 1883
    mqtt_username: str = ""
    mqtt_password: str = ""
    mqtt_client_id: str = "iot-gateway"
    mqtt_will_topic: str = "gateway/status"
    # Broker 已关闭匿名：网关必须使用 iot-gateway 身份（ACL 见 infra/mosquitto/acl.device）
    # 密码文件由 scripts/mqtt_credentials.py 生成，路径与 Broker 使用的一致
    mqtt_password_file: str = "infra/mosquitto/passwd"
    # 凭证变更后如何让 Broker 重新读取 passwd/acl；留空则自动使用 .run/mosquitto.pid
    # Docker 部署可设为：docker compose -f infra/docker-compose.yml kill -s HUP mosquitto
    mqtt_reload_command: str = ""
    # 客户端只读身份：登录后通过 /api/auth/mqtt-credentials 下发给 Web / 移动端
    # 口令只存在于服务端环境变量，不进入前端构建产物
    mqtt_web_username: str = "web-console"
    mqtt_web_password: str = ""
    mqtt_mobile_username: str = "mobile-console"
    mqtt_mobile_password: str = ""

    # ---- 鉴权 ----
    jwt_secret: str = "change-me-in-production"
    jwt_expire_minutes: int = 720
    admin_username: str = "admin"
    admin_password: str = "admin123"
    # 口令最小长度（新建/重置账号时校验，既有账号不受影响）
    password_min_length: int = 8
    # 演示账号 operator/viewer：None 表示按环境决定（开发/测试种，生产不种）
    seed_demo_users: Optional[bool] = None
    # 登录失败限流：同一 IP + 账号在窗口内失败达到阈值后锁定
    login_max_failures: int = 5
    login_failure_window_seconds: int = 300
    login_lockout_seconds: int = 900

    # ---- AI 诊断 ----
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 8.0

    # ---- 业务参数 ----
    alert_suppress_seconds: int = 300
    device_offline_timeout_seconds: int = 300
    data_retention_days: int = 90
    # 已确认告警（acked）保留 180 天：覆盖两次季度复盘，足够定位历史事件；
    # 未确认（open）告警不自动删除，避免清掉未处理的运维欠账。
    alert_retention_days: int = 180
    # 审计日志保留 365 天：操作追溯依据，比告警口径更长，且不可重建。
    audit_retention_days: int = 365
    # 离线巡检与数据清理的执行周期（秒）
    offline_sweep_interval_seconds: int = 60
    data_purge_interval_seconds: int = 3600

    # ---- 告警 Webhook 外推（空则关闭）----
    # 产生 critical 告警时向该 URL POST JSON，可用于对接钉钉/飞书/企业微信机器人
    alert_webhook_url: str = ""
    alert_webhook_timeout_seconds: float = 5.0

    # ---- 告警阈值（环境变量为初始值；在线修改走 /api/rules/thresholds，存入 rule_config 表） ----
    threshold_voltage_warning: float = 3.4
    threshold_voltage_critical: float = 3.2
    threshold_temperature_warning_high: float = 60.0
    threshold_temperature_critical_high: float = 75.0
    threshold_temperature_warning_low: float = -10.0
    threshold_temperature_critical_low: float = -20.0
    threshold_humidity_warning: float = 90.0
    threshold_humidity_critical: float = 97.0
    threshold_rssi_warning: float = -100
    threshold_rssi_critical: float = -110

    # ---- 跨域 ----
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173"

    @property
    def cors_origin_list(self) -> List[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def is_production(self) -> bool:
        return self.app_env.strip().lower() == "production"

    @property
    def demo_users_enabled(self) -> bool:
        """演示账号开关：显式配置优先，否则开发/测试默认开启、生产默认关闭。"""
        if self.seed_demo_users is not None:
            return self.seed_demo_users
        return not self.is_production

    @property
    def safe_database_url(self) -> str:
        """日志用数据库地址：保留主机/库名，隐藏口令。"""
        parsed = urlsplit(self.database_url)
        if not parsed.password:
            return self.database_url
        netloc = parsed.netloc.replace(f":{parsed.password}@", ":***@", 1)
        return parsed._replace(netloc=netloc).geturl()

    @property
    def mqtt_password_path(self) -> Path:
        """Broker 密码文件的绝对路径；相对路径按仓库根目录解析。

        网关进程通常从 `gateway/` 目录启动，而 Broker 配置里的相对路径
        （`infra/mosquitto/passwd`）是按仓库根目录解析的，二者必须一致。
        """
        path = Path(self.mqtt_password_file)
        if path.is_absolute():
            return path
        return Path(__file__).resolve().parents[2] / path

    def security_warnings(self) -> List[str]:
        """开发/测试环境的醒目告警；生产环境由 validate_for_production 直接拦截。"""
        warnings: List[str] = []
        secret = self.jwt_secret.strip()
        if len(secret) < 32 or secret.lower() in KNOWN_WEAK_JWT_SECRETS:
            warnings.append(
                f"JWT_SECRET 仍为默认/弱密钥（长度 {len(secret)}），仅适合本地开发"
            )
        if (
            self.admin_password.strip().lower() in KNOWN_WEAK_PASSWORDS
            or len(self.admin_password) < self.password_min_length
        ):
            warnings.append("ADMIN_PASSWORD 仍为默认/弱口令，仅适合本地开发")
        if self.is_sqlite:
            warnings.append("DATABASE_URL 使用 SQLite 默认值，仅适合本地开发")
        if not self.mqtt_username or not self.mqtt_password:
            warnings.append(
                "MQTT_USERNAME/MQTT_PASSWORD 未配置；Broker 已关闭匿名，网关将无法连接"
            )
        if not self.mqtt_web_password or not self.mqtt_mobile_password:
            warnings.append(
                "MQTT_WEB_PASSWORD/MQTT_MOBILE_PASSWORD 未配置；登录后无法下发客户端只读凭证"
            )
        return warnings

    def validate_for_production(self) -> None:
        """生产环境启动校验：任何默认/弱密钥、弱口令、SQLite 都直接拒绝启动。"""
        if not self.is_production:
            return

        errors: List[str] = []
        secret = self.jwt_secret.strip()
        if len(secret) < 32:
            errors.append(
                "JWT_SECRET 长度不足 32 位；生成命令："
                "python -c \"import secrets; print(secrets.token_urlsafe(48))\""
            )
        elif secret.lower() in KNOWN_WEAK_JWT_SECRETS:
            errors.append("JWT_SECRET 仍是仓库公开的默认值，必须替换为随机密钥")

        password = self.admin_password.strip()
        if password.lower() in KNOWN_WEAK_PASSWORDS or len(password) < self.password_min_length:
            errors.append(
                f"ADMIN_PASSWORD 仍是默认/弱口令（至少 {self.password_min_length} 位且不能是常见口令）"
            )

        if self.is_sqlite:
            errors.append("DATABASE_URL 仍是默认 SQLite；生产必须使用 PostgreSQL")
        else:
            db_password = urlsplit(self.database_url).password or ""
            if not db_password:
                errors.append("DATABASE_URL 未显式设置数据库口令")
            elif (
                db_password.lower() in KNOWN_WEAK_DB_PASSWORDS
                or len(db_password) < 8
            ):
                errors.append("DATABASE_URL 的数据库口令仍是默认/弱口令")

        if not self.mqtt_username.strip() or not self.mqtt_password.strip():
            errors.append(
                "MQTT_USERNAME/MQTT_PASSWORD 未配置；生产 Broker 禁止匿名，网关无法接入"
            )
        elif self.mqtt_username.strip() != "iot-gateway":
            errors.append(
                "MQTT_USERNAME 必须为 iot-gateway（与 infra/mosquitto/acl.device 的服务身份一致）"
            )
        if not self.mqtt_web_password.strip() or not self.mqtt_mobile_password.strip():
            errors.append(
                "MQTT_WEB_PASSWORD/MQTT_MOBILE_PASSWORD 未配置；无法向 Web/移动端下发只读凭证"
            )

        if errors:
            raise RuntimeError(
                "生产环境配置不安全，拒绝启动：\n- " + "\n- ".join(errors)
            )


@lru_cache
def get_settings() -> Settings:
    """带缓存的配置单例，避免每个请求重复解析 .env。"""
    return Settings()
