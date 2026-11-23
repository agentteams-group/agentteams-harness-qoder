#!/bin/bash
set -euo pipefail
if [ "${AGENTTEAMS_LOG_CAPTURED:-}" != 1 ]; then
    exec python3 /opt/agentteams/scripts/lib/runtime_supervisor.py --runtime qoder -- bash "$0" "$@"
fi
umask 077
source /opt/agentteams/scripts/lib/agentteams-env.sh
QODER_MEMBER_HOME="${AGENTTEAMS_WORKER_HOME:-/root/agentteams-fs/agents/${AGENTTEAMS_WORKER_NAME:?}}"
mkdir -p "${QODER_MEMBER_HOME}/runtime" "${QODER_MEMBER_HOME}/workspace" "${QODER_MEMBER_HOME}/shared"
if ! ensure_mc_credentials || ! agentteams_mc_host_configured; then
    if [ "${AGENTTEAMS_STORAGE_PROVIDER:-minio}" = oss ]; then
        echo 'ERROR: your OSS credentials are unavailable' >&2
        exit 1
    fi
    mc alias set "${AGENTTEAMS_STORAGE_ALIAS}" "${AGENTTEAMS_FS_ENDPOINT:?}" \
        "${AGENTTEAMS_FS_ACCESS_KEY:?}" "${AGENTTEAMS_FS_SECRET_KEY:?}" >/dev/null
fi
export TEAMHARNESS_RUNTIME_CONFIG="${QODER_MEMBER_HOME}/runtime/runtime.yaml"
export TEAMHARNESS_WORKSPACE="${QODER_MEMBER_HOME}/workspace"
export TEAMHARNESS_SHARED_DIR="${TEAMHARNESS_SHARED_DIR:-${AGENTTEAMS_SHARED_DIR:-${QODER_MEMBER_HOME}/shared}}"
export AGENTTEAMS_PLUGIN_DIR=/opt/agentteams/plugins/teamharness
python3 -m agentteams_qoder.bootstrap
exec agentteams-qoder
