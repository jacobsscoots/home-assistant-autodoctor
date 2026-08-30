# Home Assistant's supported app base includes S6-Overlay and uses a root-oriented runtime contract.
# Official HA apps likewise use root where S6/runtime hardware or persistent-volume access requires it.
FROM ghcr.io/home-assistant/base:3.24

ARG BUILD_VERSION
ARG BUILD_ARCH
ARG APP_USER=root

LABEL \
  io.hass.name="AutoDoctor" \
  io.hass.description="AI-assisted Home Assistant incident detection and safe repair orchestration" \
  io.hass.type="app" \
  io.hass.version="${BUILD_VERSION}" \
  io.hass.arch="${BUILD_ARCH}"

RUN apk add --no-cache ca-certificates py3-pip python3 && \
    python3 -m pip install --no-cache-dir --break-system-packages \
      'aiohttp==3.14.3' \
      'httpx2==2.12.0' \
      'mcp==2.1.1'

COPY app /app
COPY run.sh /run.sh
RUN chmod a+x /run.sh

# /data is a Supervisor-managed persistent volume and the HA base runtime is S6-oriented.
# Keep the supported root runtime explicit; do not substitute an unverified non-root user.
USER ${APP_USER}

CMD ["/run.sh"]
