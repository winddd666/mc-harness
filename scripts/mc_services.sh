#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="/home/xzq/projects/mc"
UNIT_SRC_DIR="${PROJECT_DIR}/deploy/systemd"
UNIT_DST_DIR="${HOME}/.config/systemd/user"
SERVICES=("mc-live-proxy.service" "mc-console.service")

export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"
if [ -z "${DBUS_SESSION_BUS_ADDRESS:-}" ]; then
  if [ -n "${AGYP_DESKTOP_BUS_ADDRESS:-}" ]; then
    export DBUS_SESSION_BUS_ADDRESS="$AGYP_DESKTOP_BUS_ADDRESS"
  elif [ -S "${XDG_RUNTIME_DIR}/bus" ]; then
    export DBUS_SESSION_BUS_ADDRESS="unix:path=${XDG_RUNTIME_DIR}/bus"
  fi
fi

usage() {
  cat <<'EOF'
Usage: mc_services.sh <command> [args]

Commands:
  install         Install systemd --user units, reload daemon, enable & start services
  start           Start mc-live-proxy and mc-console services
  stop            Stop mc-live-proxy and mc-console services
  restart         Restart mc-live-proxy and mc-console services
  restart-live    Restart only mc-live-proxy service (keep mc-console running)
  status          Show systemd status and HTTP health checks (:8765, :8770)
  health          Check HTTP endpoints (:8765/api/info, :8770/api/state)
  logs [N]        Show last N journal lines (default: 60)
  beast-status    Check persistent services on remote nju-beast (via beast_ctl.py)
  beast-restart   Restart persistent viewer/director services on remote nju-beast
EOF
}

check_health() {
  echo "--- HTTP Health Checks ---"
  echo -n "[mc-live :8765/api/info] "
  if curl -fsS --max-time 5 "http://127.0.0.1:8765/api/info"; then
    echo ""
  else
    echo "UNREACHABLE"
    return 1
  fi
  echo -n "[mc-console :8770/api/state] "
  if curl -fsS --max-time 5 "http://127.0.0.1:8770/api/state"; then
    echo ""
  else
    echo "UNREACHABLE"
    return 1
  fi
}

cmd="${1:-status}"
case "$cmd" in
  install)
    mkdir -p "$UNIT_DST_DIR"
    for unit in "${SERVICES[@]}"; do
      cp -f "${UNIT_SRC_DIR}/${unit}" "${UNIT_DST_DIR}/${unit}"
    done
    systemctl --user daemon-reload
    systemctl --user enable --now "${SERVICES[@]}"
    ;;
  start)
    systemctl --user start "${SERVICES[@]}"
    ;;
  stop)
    systemctl --user stop "${SERVICES[@]}"
    ;;
  restart)
    systemctl --user restart "${SERVICES[@]}"
    ;;
  restart-live)
    systemctl --user restart mc-live-proxy.service
    ;;
  status)
    systemctl --user --no-pager -l status "${SERVICES[@]}" || true
    check_health
    ;;
  health)
    check_health
    ;;
  logs)
    lines="${2:-60}"
    journalctl --user -u mc-live-proxy.service -u mc-console.service -n "$lines" --no-pager
    ;;
  beast-status)
    ssh -n -F "${HOME}/.ssh/config" nju-beast wsl -d Ubuntu-22.04 -u xia -- python3 /home/xia/mc-beast/mc/live_viewer/beast_ctl.py status
    ;;
  beast-restart)
    ssh -n -F "${HOME}/.ssh/config" nju-beast wsl -d Ubuntu-22.04 -u xia -- python3 /home/xia/mc-beast/mc/live_viewer/beast_ctl.py restart
    ;;
  -h|--help|help)
    usage
    ;;
  *)
    echo "Unknown command: $cmd" >&2
    usage >&2
    exit 2
    ;;
esac
