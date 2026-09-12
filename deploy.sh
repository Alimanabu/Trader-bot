#!/usr/bin/env bash
# Установка и запуск отдела BTC-агентов на сервере с Ubuntu/Debian одной командой.
# Повторный запуск обновляет код и перезапускает приложение, настройки сохраняются.
set -euo pipefail

REPO="https://github.com/Alimanabu/Trader-bot.git"
BRANCH="${BRANCH:-claude/bitcoin-trading-agents-app-ihqylo}"
DIR="${DIR:-$HOME/trader-bot}"

echo "==> Проверяю Docker"
if ! command -v docker >/dev/null 2>&1; then
  echo "==> Устанавливаю Docker (1-2 минуты)"
  curl -fsSL https://get.docker.com | sh
fi
if ! command -v git >/dev/null 2>&1; then
  apt-get update -qq && apt-get install -y -qq git
fi

if [ -d "$DIR/.git" ]; then
  echo "==> Обновляю код в $DIR"
  git -C "$DIR" pull --ff-only
else
  echo "==> Скачиваю код в $DIR"
  git clone -q -b "$BRANCH" "$REPO" "$DIR"
fi
cd "$DIR"

if [ ! -f .env ]; then
  cp .env.example .env
  echo
  read -rp "Ключ Claude API (Enter, если пока нет): " KEY < /dev/tty
  while true; do
    read -rsp "Придумайте пароль для панели (минимум 8 символов): " PW < /dev/tty; echo
    [ "${#PW}" -ge 8 ] && break
    echo "Слишком короткий, попробуйте ещё раз."
  done
  sed -i "s|^ANTHROPIC_API_KEY=.*|ANTHROPIC_API_KEY=${KEY}|" .env
  sed -i "s|^PANEL_PASSWORD=.*|PANEL_PASSWORD=${PW}|" .env
  echo "==> Настройки сохранены в $DIR/.env"
fi

mkdir -p data
echo "==> Собираю и запускаю (первый раз 2-3 минуты)"
docker compose up -d --build

IP=$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')
echo
echo "Готово. Панель: http://${IP}:8080  (логин любой, пароль тот, что вы придумали)"
echo "Логи:          cd $DIR && docker compose logs -f"
echo "Обновить код:  bash $DIR/deploy.sh"
