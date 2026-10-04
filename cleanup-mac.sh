#!/bin/zsh
# Очистка места на диске. Перед каждым шагом спрашивает подтверждение (y/n).

ask() { read -q "?$1 [y/N] " && echo && return 0; echo; return 1; }
free() { df -h /System/Volumes/Data | awk 'NR==2 {print "Свободно: " $4}'; }

free

ask "Очистить ~/Library/Caches и ~/Library/Logs (~2,2 ГБ)?" &&
  rm -rf ~/Library/Caches/* ~/Library/Logs/*

ask "Удалить ~/.cache/codex-runtimes (3,3 ГБ)?" &&
  rm -rf ~/.cache/codex-runtimes

ask "Удалить модели Ollama (5 ГБ)?" &&
  ollama rm qwen2.5:3b qwen2.5-codex:latest qwen3.5:4b qwen3.5-codex:latest

if docker info >/dev/null 2>&1; then
  ask "Docker: удалить неиспользуемые образы и контейнеры?" && docker system prune -af
else
  echo "Docker не запущен — пропускаю (запустите Docker Desktop и повторите, если нужно)."
fi

pgrep -x UTM >/dev/null && echo "UTM открыт — закройте его перед удалением VM."
ask "Удалить VM Windows из UTM (4 ГБ, НАВСЕГДА со всем содержимым)?" &&
  rm -rf ~/Library/Containers/com.utmapp.UTM/Data/Documents/Windows.utm

echo "Claude vm_bundles (9,7 ГБ) удаляйте только при закрытом Claude Desktop."
ask "Удалить ~/Library/Application Support/Claude/vm_bundles?" &&
  rm -rf ~/Library/Application\ Support/Claude/vm_bundles

free
echo "Готово. Не забудьте очистить Корзину (4,15 ГБ)."
