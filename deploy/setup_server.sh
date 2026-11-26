#!/usr/bin/env bash
# Идемпотентная настройка стенда на Ubuntu 26.04: Docker + compose, UFW 22/80/443, swap, папки данных.
# Запуск на сервере из корня репозитория: sudo [DOCKER_REGISTRY_MIRROR=https://...] ./deploy/setup_server.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Запустите через sudo" >&2
  exit 1
fi

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_UID=1000  # пользователь app в образе backend
export DEBIAN_FRONTEND=noninteractive

echo "==> Пакеты"
apt-get update -q
apt-get install -y -q ca-certificates curl git ufw docker.io docker-compose-v2
systemctl enable --now docker

# Docker Hub недоступен из части российских сетей: DOCKER_REGISTRY_MIRROR=https://dockerhub.timeweb.cloud
if [[ -n "${DOCKER_REGISTRY_MIRROR:-}" ]]; then
  mkdir -p /etc/docker
  printf '{\n  "registry-mirrors": ["%s"]\n}\n' "$DOCKER_REGISTRY_MIRROR" > /etc/docker/daemon.json
  systemctl restart docker
fi

if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
  usermod -aG docker "$SUDO_USER"
fi

echo "==> Firewall"
ufw allow 22/tcp
ufw allow 80/tcp
ufw allow 443/tcp
ufw allow 443/udp
ufw --force enable

echo "==> Swap 2G (4 ГБ RAM: запас на сборку образа и выкачку фида)"
if ! swapon --show | grep -q '/swapfile'; then
  if [[ ! -f /swapfile ]]; then
    fallocate -l 2G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
  fi
  swapon /swapfile
fi
grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab

echo "==> Папки данных"
mkdir -p "$REPO_DIR/data/raw" "$REPO_DIR/data/quotes"
chown -R "$APP_UID:$APP_UID" "$REPO_DIR/data"

if [[ ! -f "$REPO_DIR/.env" ]]; then
  cp "$REPO_DIR/.env.example" "$REPO_DIR/.env"
  chmod 600 "$REPO_DIR/.env"
  echo "!! Создан .env из шаблона — заполните LLM_API_KEY и OPERATOR_PASSWORD"
fi

docker --version
docker compose version
echo "==> Готово. Дальше: docker compose up -d --build && ./scripts/smoke_test.sh"
