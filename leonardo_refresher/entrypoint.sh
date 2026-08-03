#!/usr/bin/env bash
set -Eeuo pipefail

# headless 模式：无虚拟显示与远程桌面组件（登录在本地完成、cookie 上传）。
PROFILE_DIR="${PROFILE_DIR:-/profile}"
if [[ "${PROFILE_DIR}" != "/profile" ]]; then
    echo "PROFILE_DIR must be /profile" >&2
    exit 2
fi

if [[ "$(id -u)" -eq 0 ]]; then
    mkdir -p "${PROFILE_DIR}"
    chown -R pwuser:pwuser "${PROFILE_DIR}"
    exec gosu pwuser "$0" "$@"
fi

mkdir -p "${PROFILE_DIR}"
chmod 0700 "${PROFILE_DIR}"

exec python -m leonardo_refresher
