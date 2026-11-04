-- ============================================================
-- IoT 全端平台 数据库初始化脚本（PostgreSQL 16）
-- 执行方式：
--   psql -U iot -d iot -f infra/postgres/init.sql
-- 幂等：全部使用 IF NOT EXISTS，可重复执行
-- ============================================================

-- ---------- 1. 设备表 ----------
CREATE TABLE IF NOT EXISTS device (
    device_id        varchar(64)  PRIMARY KEY,
    name             varchar(128) NOT NULL,
    model            varchar(64)  NOT NULL DEFAULT 'VG-S100',
    firmware_version varchar(32)  NOT NULL DEFAULT '1.0.0',
    status           varchar(16)  NOT NULL DEFAULT 'offline',
    last_report_at   timestamptz,
    last_online_at   timestamptz,
    created_at       timestamptz  NOT NULL DEFAULT now(),
    updated_at       timestamptz  NOT NULL DEFAULT now()
);

COMMENT ON TABLE  device IS '设备元数据表：一个设备一行，状态由 Broker 的 retained/LWT 报文驱动';
COMMENT ON COLUMN device.status IS 'online / offline，由 device/{id}/status 消息更新';

-- ---------- 2. 传感器时序表 ----------
CREATE TABLE IF NOT EXISTS sensor_record (
    id          bigserial    PRIMARY KEY,
    device_id   varchar(64)  NOT NULL,
    temperature numeric(10,2) NOT NULL,
    humidity    numeric(10,2) NOT NULL,
    voltage     numeric(10,2) NOT NULL,
    rssi        integer      NOT NULL,
    battery     integer      NOT NULL,
    seq         bigint       NOT NULL,
    ts          timestamptz  NOT NULL
);

COMMENT ON TABLE sensor_record IS '传感器时序数据表：设备每次唤醒上报一条';

-- 查询主路径：按设备 + 时间倒序取最新 N 条
CREATE INDEX IF NOT EXISTS idx_sensor_device_ts
    ON sensor_record (device_id, ts DESC);

-- 幂等去重：同设备同序号同时间戳视为同一条消息（MQTT QoS1 可能重复投递）
CREATE UNIQUE INDEX IF NOT EXISTS uq_sensor_device_seq_ts
    ON sensor_record (device_id, seq, ts);

-- ---------- 3. AI 告警表 ----------
CREATE TABLE IF NOT EXISTS device_alert (
    id           bigserial    PRIMARY KEY,
    alert_id     varchar(64)  NOT NULL UNIQUE,
    device_id    varchar(64)  NOT NULL,
    level        varchar(16)  NOT NULL,
    metric       varchar(32)  NOT NULL,
    value        numeric(10,2) NOT NULL,
    threshold    numeric(10,2) NOT NULL,
    message      text         NOT NULL,
    ai_generated boolean      NOT NULL DEFAULT false,
    status       varchar(16)  NOT NULL DEFAULT 'open',
    created_at   timestamptz  NOT NULL DEFAULT now()
);

COMMENT ON TABLE  device_alert IS 'AI 异常诊断告警表：规则判定触发，LLM 生成处置文案';
COMMENT ON COLUMN device_alert.status IS 'open 未处理 / acked 已确认';

CREATE INDEX IF NOT EXISTS idx_alert_created
    ON device_alert (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_alert_device_metric
    ON device_alert (device_id, metric, created_at DESC);

-- ---------- 4. 指令表（OTA 与远程控制链路支撑） ----------
CREATE TABLE IF NOT EXISTS device_command (
    cmd_id      varchar(64)  PRIMARY KEY,
    device_id   varchar(64)  NOT NULL,
    type        varchar(16)  NOT NULL,
    payload     jsonb        NOT NULL DEFAULT '{}'::jsonb,
    state       varchar(16)  NOT NULL DEFAULT 'issued',
    progress    integer      NOT NULL DEFAULT 0,
    message     varchar(255),
    ota_task_id varchar(64),
    operator    varchar(64)  NOT NULL DEFAULT 'system',
    created_at  timestamptz  NOT NULL DEFAULT now(),
    updated_at  timestamptz  NOT NULL DEFAULT now()
);

COMMENT ON TABLE  device_command IS '云端下发指令及设备回执状态：issued→received→progress→success/failed';
COMMENT ON COLUMN device_command.progress IS 'OTA 升级进度百分比 0~100';

CREATE INDEX IF NOT EXISTS idx_command_device_created
    ON device_command (device_id, created_at DESC);

-- ---------- 5. OTA 任务表 ----------
CREATE TABLE IF NOT EXISTS ota_task (
    task_id        varchar(64)  PRIMARY KEY,
    target_version varchar(32)  NOT NULL,
    url            varchar(512) NOT NULL,
    description    varchar(255),
    total          integer      NOT NULL DEFAULT 0,
    success        integer      NOT NULL DEFAULT 0,
    failed         integer      NOT NULL DEFAULT 0,
    created_at     timestamptz  NOT NULL DEFAULT now()
);

COMMENT ON TABLE ota_task IS 'OTA 任务表：一个任务覆盖多台设备，汇总字段用于列表页快速展示';

-- ---------- 7. 阈值在线配置表 ----------
CREATE TABLE IF NOT EXISTS rule_config (
    key         varchar(64) PRIMARY KEY,
    value       numeric(10,2) NOT NULL,
    updated_by  varchar(64) NOT NULL DEFAULT 'system',
    updated_at  timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE rule_config IS '告警阈值运行时覆盖：优先于环境变量，在线修改立即生效';

-- ---------- 8. 审计日志表 ----------
CREATE TABLE IF NOT EXISTS audit_log (
    id          bigserial PRIMARY KEY,
    operator    varchar(64) NOT NULL,
    action      varchar(32) NOT NULL,
    target_type varchar(32) NOT NULL DEFAULT '',
    target_id   varchar(128) NOT NULL DEFAULT '',
    detail      jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at  timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE audit_log IS '后台管理动作审计：登录/指令/OTA/告警确认/阈值修改/账号变更';

CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_log (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_action_created ON audit_log (action, created_at DESC);

-- ---------- 9. 后台账号表 ----------
CREATE TABLE IF NOT EXISTS app_user (
    username      varchar(64) PRIMARY KEY,
    password_hash varchar(255) NOT NULL,
    role          varchar(16) NOT NULL DEFAULT 'viewer',
    active        boolean NOT NULL DEFAULT true,
    last_login_at timestamptz,
    created_at    timestamptz NOT NULL DEFAULT now(),
    updated_at    timestamptz NOT NULL DEFAULT now()
);

COMMENT ON TABLE app_user IS '后台账号与角色：admin/operator/viewer，口令以 PBKDF2 哈希存储';
COMMENT ON COLUMN app_user.role IS 'admin 全部权限 / operator 运维执行 / viewer 只读';

-- ---------- 10. 种子数据 ----------
INSERT INTO device (device_id, name, model, firmware_version, status)
VALUES
    ('dev-0001', '冷库温度节点 01', 'VG-S100', '1.0.0', 'offline'),
    ('dev-0002', '冷库温度节点 02', 'VG-S100', '1.0.0', 'offline'),
    ('dev-0003', '车间环境节点 03', 'VG-S200', '1.0.0', 'offline'),
    ('dev-0004', '户外监测节点 04', 'VG-S200', '1.0.0', 'offline')
ON CONFLICT (device_id) DO NOTHING;
