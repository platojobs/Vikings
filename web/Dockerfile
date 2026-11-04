# syntax=docker/dockerfile:1
# Vue3 管理后台生产镜像：Node 构建 + nginx 静态托管（非 root，监听 8080）。
# 构建（仓库根目录）：docker build -f web/Dockerfile -t iot-web .

FROM node:20-alpine AS build

WORKDIR /app
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./

# 生产默认同源：API 走 /api，MQTT WebSocket 走 /mqtt，均由反代转发。
ARG VITE_API_BASE=""
ARG VITE_MQTT_WS="/mqtt"
ENV VITE_API_BASE=$VITE_API_BASE \
    VITE_MQTT_WS=$VITE_MQTT_WS
RUN npm run build

FROM nginxinc/nginx-unprivileged:1.27-alpine AS runtime

COPY infra/nginx/web.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 8080

HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
  CMD ["wget", "-q", "-O", "/dev/null", "http://127.0.0.1:8080/"]
