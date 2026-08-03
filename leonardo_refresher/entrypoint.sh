#!/usr/bin/env bash
set -Eeuo pipefail

if [[ -z "${NOVNC_PASSWORD:-}" ]]; then
    echo "NOVNC_PASSWORD is required" >&2
    exit 2
fi
if (( ${#NOVNC_PASSWORD} < 8 )); then
    echo "NOVNC_PASSWORD must contain at least 8 characters" >&2
    exit 2
fi

export DISPLAY="${DISPLAY:-:99}"
PROFILE_DIR="${PROFILE_DIR:-/profile}"
RUNTIME_DIR=/tmp/leonardo-refresher
VNC_PASSWORD_FILE="${RUNTIME_DIR}/vnc.pass"

if [[ "${PROFILE_DIR}" != "/profile" ]]; then
    echo "PROFILE_DIR must be /profile" >&2
    exit 2
fi

if [[ "$(id -u)" -eq 0 ]]; then
    mkdir -p "${PROFILE_DIR}" "${RUNTIME_DIR}"
    chown -R pwuser:pwuser "${PROFILE_DIR}"
    chown -R pwuser:pwuser "${RUNTIME_DIR}"
    exec gosu pwuser "$0" "$@"
fi

mkdir -p "${PROFILE_DIR}" "${RUNTIME_DIR}"
chmod 0700 "${PROFILE_DIR}" "${RUNTIME_DIR}"
x11vnc -storepasswd "${NOVNC_PASSWORD}" "${VNC_PASSWORD_FILE}" >/dev/null 2>&1
chmod 0600 "${VNC_PASSWORD_FILE}"

pids=()
cleanup() {
    trap - EXIT INT TERM
    for pid in "${pids[@]:-}"; do
        kill "${pid}" 2>/dev/null || true
    done
    for pid in "${pids[@]:-}"; do
        wait "${pid}" 2>/dev/null || true
    done
}
trap cleanup EXIT INT TERM

Xvfb "${DISPLAY}" -screen 0 1280x800x24 -nolisten tcp &
xvfb_pid="$!"
pids+=("${xvfb_pid}")

display_number="${DISPLAY#:}"
display_number="${display_number%%.*}"
x_socket="/tmp/.X11-unix/X${display_number}"
for ((attempt = 0; attempt < 100; attempt++)); do
    [[ -S "${x_socket}" ]] && break
    if ! kill -0 "${xvfb_pid}" 2>/dev/null; then
        echo "Xvfb exited before its display became ready" >&2
        wait "${xvfb_pid}" 2>/dev/null || true
        exit 1
    fi
    sleep 0.1
done
if [[ ! -S "${x_socket}" ]]; then
    echo "Xvfb display did not become ready within 10 seconds" >&2
    exit 1
fi

x11vnc \
    -display "${DISPLAY}" \
    -forever \
    -shared \
    -localhost \
    -noxdamage \
    -rfbport 5900 \
    -rfbauth "${VNC_PASSWORD_FILE}" &
pids+=("$!")

/usr/share/novnc/utils/novnc_proxy \
    --listen 6080 \
    --vnc localhost:5900 &
pids+=("$!")

python -m leonardo_refresher &
pids+=("$!")

set +e
wait -n "${pids[@]}"
status=$?
set -e
exit "${status}"
