"""网关配置：全部通过环境变量注入，禁止在代码里硬编码连接信息。"""

from functools import lru_cache
from typing import List

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """集中式配置对象。

    读取顺序：进程环境变量 > gateway/.env > 默认值。
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # ---- 数据库：默认 SQLite 保证零依赖可跑，生产通过 .env 指向 PostgreSQL ----
    database_url: str = "sqlite+aiosqlite:///./data/iot.db"

    # ---- MQTT ----
    mqtt_host: str = "127.0.0.1"
    mqtt_port: int = 1883
    mqtt_username: str = ""
    mqtt_password: str = ""
    mqtt_client_id: str = "iot-gateway"
    mqtt_will_topic: str = "gateway/status"

    # ---- 鉴权 ----
    jwt_secret: str = "change-me-in-production"
    jwt_expire_minutes: int = 720
    admin_username: str = "admin"
    admin_password: str = "admin123"

    # ---- AI 诊断 ----
    llm_base_url: str = "https://api.openai.com/v1"
    llm_api_key: str = ""
    llm_model: str = "gpt-4o-mini"
    llm_timeout_seconds: float = 8.0

    # ---- 业务参数 ----
    alert_suppress_seconds: int = 300
    device_offline_timeout_seconds: int = 300
    data_retention_days: int = 90
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


@lru_cache
def get_settings() -> Settings:
    """带缓存的配置单例，避免每个请求重复解析 .env。"""
    return Settings()
