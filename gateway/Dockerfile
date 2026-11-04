# syntax=docker/dockerfile:1
# Python 网关生产镜像：多阶段构建，运行阶段非 root，内置健康检查。
# 构建（仓库根目录）：docker build -f gateway/Dockerfile -t iot-gateway .

FROM python:3.12-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build
RUN python -m venv /opt/venv
COPY gateway/requirements.txt ./requirements.txt
RUN /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install -r requirements.txt

FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH

WORKDIR /app

# 固定 uid/gid 的非 root 运行用户；compose 可用 DEPLOY_UID/DEPLOY_GID 覆盖为宿主机
# 用户，以便写共享的 Mosquitto 密码文件（默认仍是 10001 非 root）。
RUN groupadd --gid 10001 appuser \
 && useradd --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin appuser

COPY --from=builder /opt/venv /opt/venv
COPY gateway/app ./app
# 迁移文件必须随镜像发布：生产 compose 的 migrate 服务直接执行 alembic upgrade head
COPY gateway/alembic.ini ./alembic.ini
COPY gateway/alembic ./alembic

USER 10001:10001
EXPOSE 8000

# 就绪检查（v1.20.0）：/api/health/ready 会真实查询数据库并检查 Broker 连接。
# `/api/health` 仍是轻量存活探针（进程存活即 200）；这里用 ready，避免
# 「数据库不可用/ Broker 掉线」时容器继续被标记为健康。
HEALTHCHECK --interval=15s --timeout=5s --start-period=40s --retries=5 \
  CMD ["python", "-c", "import sys,urllib.request; r=urllib.request.urlopen('http://127.0.0.1:8000/api/health/ready', timeout=4); sys.exit(0 if r.status == 200 else 1)"]

# 反向代理后面运行：信任反代传入的 X-Forwarded-For（登录限流按真实客户端 IP）。
# 网关端口只在 Docker 内网可见，不对宿主机发布。
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
