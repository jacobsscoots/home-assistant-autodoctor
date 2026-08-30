FROM ghcr.io/home-assistant/base:3.24 # NOSONAR -- HA Supervisor/S6 app runtime requires its managed root-oriented /data contract.

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

# Home Assistant's base runtime and Supervisor-managed /data are root-oriented.
# Keep that compatibility explicit while allowing a future verified non-root override.
USER ${APP_USER}

CMD ["/run.sh"]
