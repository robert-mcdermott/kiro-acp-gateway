# Kiro ACP Gateway container: kiro-cli + the gateway, bound to 0.0.0.0:8000.
# Works with Docker and Podman (substitute `podman` for `docker`).
#
#   docker build -t kiro-acp-gateway .
#   docker run -it --rm -v kiro-home:/home/kiro/.kiro kiro-acp-gateway kiro-cli login --use-device-flow   # once
#   docker run -d --name kiro-gateway -p 127.0.0.1:8000:8000 \
#     -v kiro-home:/home/kiro/.kiro -v "$PWD":/workspace:z \
#     -e KIRO_GATEWAY_API_KEY=change-me kiro-acp-gateway
#
# The named volume keeps Kiro's login and agents; /workspace is the directory Kiro's own
# tools may touch (agent mode). Coding harnesses run their tools outside the container.
FROM python:3.12-slim-trixie

ARG TARGETARCH=amd64
ARG KIRO_CLI_VERSION=latest
ENV PATH=/opt/kiro-acp-gateway/.venv/bin:/home/kiro/.local/bin:/usr/local/bin:$PATH \
    UV_LINK_MODE=copy \
    KIRO_GATEWAY_HOST=0.0.0.0 \
    KIRO_GATEWAY_PORT=8000 \
    KIRO_GATEWAY_WORKSPACE=/workspace

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git ripgrep unzip \
    && rm -rf /var/lib/apt/lists/*

# Kiro CLI from AWS's release channel (same source as `curl -fsSL https://cli.kiro.dev/install | bash`).
RUN set -eux; \
    case "${TARGETARCH}" in amd64) KIRO_ARCH=x86_64 ;; arm64) KIRO_ARCH=aarch64 ;; *) echo "unsupported arch ${TARGETARCH}" >&2; exit 1 ;; esac; \
    curl -fsSL "https://desktop-release.q.us-east-1.amazonaws.com/${KIRO_CLI_VERSION}/kirocli-${KIRO_ARCH}-linux.zip" -o /tmp/kirocli.zip; \
    unzip -q /tmp/kirocli.zip -d /tmp; \
    Q_INSTALL_GLOBAL=1 Q_SKIP_SETUP=1 /tmp/kirocli/install.sh; \
    rm -rf /tmp/kirocli /tmp/kirocli.zip; \
    kiro-cli --version

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

RUN useradd --create-home --uid 1000 kiro && mkdir -p /workspace && chown kiro:kiro /workspace
WORKDIR /opt/kiro-acp-gateway
COPY --chown=kiro:kiro pyproject.toml uv.lock README.md ./
COPY --chown=kiro:kiro src ./src
RUN uv sync --frozen --no-dev && chown -R kiro:kiro /opt/kiro-acp-gateway
USER kiro
VOLUME ["/home/kiro/.kiro", "/workspace"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD curl -fs http://127.0.0.1:8000/health || exit 1
CMD ["kiro-gateway"]
