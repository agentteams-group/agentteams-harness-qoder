# syntax=docker/dockerfile:1
ARG CONTROLLER_IMAGE
ARG MC_IMAGE=higress-registry.cn-hangzhou.cr.aliyuncs.com/higress/mc:20260216
FROM ${CONTROLLER_IMAGE} AS controller
FROM ${MC_IMAGE} AS mc
FROM python:3.13-slim-bookworm
RUN apt-get update && apt-get install -y --no-install-recommends bash ca-certificates curl git jq tini \
    && rm -rf /var/lib/apt/lists/*
COPY --from=controller /usr/local/bin/agt /usr/local/bin/agt
COPY --from=controller /opt/agentteams/agent/ /opt/agentteams/agent/
COPY --from=mc /usr/bin/mc /usr/local/bin/mc.bin
COPY --from=at /shared/lib/ /opt/agentteams/scripts/lib/
COPY --from=at /plugins/teamharness/ /opt/agentteams/plugins/teamharness/
COPY --from=at /LICENSE /usr/share/licenses/agentteams/LICENSE
COPY pyproject.toml /opt/agentteams/qoder/pyproject.toml
COPY src/ /opt/agentteams/qoder/src/
RUN pip install --no-cache-dir /opt/agentteams/qoder \
    && chmod +x /opt/agentteams/scripts/lib/mc-wrapper.sh \
    && ln -s /opt/agentteams/scripts/lib/mc-wrapper.sh /usr/local/bin/mc
COPY scripts/entrypoint.sh /opt/agentteams/entrypoint.sh
ENV PYTHONPATH=/opt/agentteams/scripts/lib
EXPOSE 8088
ENTRYPOINT ["/usr/bin/tini", "--", "bash", "/opt/agentteams/entrypoint.sh"]
