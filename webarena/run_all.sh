#!/bin/bash
# Runs the full webarena stack: initial setup (if needed) + reset server + homepage.
# Usage: sudo bash run_all.sh [--setup]
#   --setup: Run the full initial setup (load images, create containers, patch, checkpoint)
#            Only needed on first run or to rebuild the :ready images.
#   Without --setup: Just starts the reset server and homepage (assumes :ready images exist).
set -e

cd "$(dirname "$0")"
source 00_vars.sh

host_nginx_master_pids() {
    ps -eo pid=,cmd= 2>/dev/null | while read -r pid cmd; do
        case "${cmd}" in
            nginx:\ master\ process*)
                if [ "$(readlink -f "/proc/${pid}/root" 2>/dev/null || true)" = "/" ]; then
                    printf '%s\n' "${pid}"
                fi
                ;;
        esac
    done
}

stop_host_nginx() {
    local pids remaining pid

    mapfile -t pids < <(host_nginx_master_pids)
    if [ "${#pids[@]}" -eq 0 ]; then
        rm -f /run/nginx.pid 2>/dev/null || true
        return
    fi

    kill -TERM "${pids[@]}" 2>/dev/null || true
    sleep 2

    remaining=()
    for pid in "${pids[@]}"; do
        if kill -0 "${pid}" 2>/dev/null; then
            remaining+=("${pid}")
        fi
    done

    if [ "${#remaining[@]}" -gt 0 ]; then
        kill -KILL "${remaining[@]}" 2>/dev/null || true
        sleep 1
    fi

    rm -f /run/nginx.pid 2>/dev/null || true
}

prepare_host_nginx() {
    # The reset server owns the host nginx reverse proxy. Keep Debian's nginx
    # service and default port-80 site from racing the homepage/reset server.
    if command -v systemctl >/dev/null 2>&1; then
        systemctl stop nginx.service >/dev/null 2>&1 || true
        systemctl disable nginx.service >/dev/null 2>&1 || true
        systemctl mask nginx.service >/dev/null 2>&1 || true
    fi
    stop_host_nginx
    rm -f /etc/nginx/sites-enabled/default /etc/nginx/conf.d/default.conf 2>/dev/null || true
}

if [ "$1" = "--setup" ]; then
    echo "=== Running full initial setup ==="
    bash 01_docker_load_images.sh
    bash 02_docker_remove_containers.sh
    bash 03_docker_create_containers.sh
    bash 04_docker_start_containers.sh
    bash 05_docker_patch_containers.sh
    bash 08_checkpoint.sh
    # Stop all containers — the reset server will manage them from here
    podman stop -a 2>/dev/null || true
    echo "=== Initial setup complete ==="
fi

prepare_host_nginx

# Start homepage in background
echo "Starting homepage server on port ${HOMEPAGE_PORT}..."
bash 06_serve_homepage.sh &
HOMEPAGE_PID=$!

# Start reset server (blocking — manages all containers)
echo "Starting reset server on port ${RESET_PORT}..."
bash 07_serve_reset.sh &
RESET_PID=$!

# Wait for either to exit
trap "kill $HOMEPAGE_PID $RESET_PID 2>/dev/null; wait" EXIT INT TERM
wait
