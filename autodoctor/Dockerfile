FROM ghcr.io/home-assistant/base:latest

ARG BUILD_VERSION
ARG BUILD_ARCH

LABEL \
  io.hass.name="AutoDoctor" \
  io.hass.description="AI-assisted Home Assistant incident detection and safe repair orchestration" \
  io.hass.type="app" \
  io.hass.version="${BUILD_VERSION}" \
  io.hass.arch="${BUILD_ARCH}"

RUN apk add --no-cache python3 py3-pip ca-certificates && \
    python3 -m pip install --no-cache-dir --break-system-packages \
      'aiohttp>=3.10,<4' \
      'httpx2>=2.5,<3' \
      'mcp>=2,<3'

COPY app /app
COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
