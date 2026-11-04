#!/usr/bin/env bash
# 生成自签证书（仅用于本地/内网验证 HTTPS）。
# 生产请改用正式证书或 ACME（见 docs/deployment.md 的证书章节）。
#
# 用法：scripts/gen_self_signed_cert.sh [证书目录] [主域名]
#   默认：infra/prod/nginx/certs 与 localhost
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CERT_DIR="${1:-$ROOT_DIR/infra/prod/nginx/certs}"
DOMAIN="${2:-localhost}"

if [ -f "$CERT_DIR/fullchain.pem" ] && [ -f "$CERT_DIR/privkey.pem" ] && [ "${FORCE_CERT:-0}" != "1" ]; then
  echo "[cert] 已存在证书：${CERT_DIR}（如需重新生成请设置 FORCE_CERT=1）"
  exit 0
fi

mkdir -p "$CERT_DIR"
TMP_CONF="$(mktemp)"
trap 'rm -f "$TMP_CONF"' EXIT

cat >"$TMP_CONF" <<EOF
[req]
distinguished_name = dn
x509_extensions = v3_req
prompt = no

[dn]
CN = ${DOMAIN}

[v3_req]
basicConstraints = CA:FALSE
keyUsage = digitalSignature, keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = @alt

[alt]
DNS.1 = ${DOMAIN}
DNS.2 = localhost
IP.1 = 127.0.0.1
EOF

openssl req -x509 -nodes -newkey rsa:2048 -sha256 -days 365 \
  -keyout "$CERT_DIR/privkey.pem" \
  -out "$CERT_DIR/fullchain.pem" \
  -config "$TMP_CONF" >/dev/null 2>&1

chmod 600 "$CERT_DIR/privkey.pem"
chmod 644 "$CERT_DIR/fullchain.pem"

echo "[cert] 已生成自签证书：${CERT_DIR}（CN=${DOMAIN}，有效期 365 天）"
echo "[cert] 浏览器首次访问需手动信任；curl 验证可用 --cacert 或 -k"
