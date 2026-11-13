# Sandbox for third-party code: package installs, linters, tests (CHB-31). Run it with
# `.devcontainer/run <command>`. Python 3.15 and uv match compose.yaml; Node 24 matches .nvmrc;
# Terraform matches infra/github/.terraform-version. The image build has full network.
# The egress limit starts with each container (entry.sh, init-firewall.sh).
FROM ghcr.io/astral-sh/uv:0.13-python3.15-trixie-slim

ARG TERRAFORM_VERSION=1.17.1
# From https://releases.hashicorp.com/terraform/1.17.1/terraform_1.17.1_SHA256SUMS
ARG TERRAFORM_SHA256_AMD64=4638628058e71a55212b1c563712eac03b902128f40740f3dc8174df12135061
ARG TERRAFORM_SHA256_ARM64=988e950a1c23058093b0e66ab2d5fb53fc18345ecfbd5871acf51dbac9325edd

COPY --from=node:24-trixie-slim /usr/local/ /usr/local/

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl dnsutils git iproute2 ipset iptables jq make unzip \
    && rm -rf /var/lib/apt/lists/*

RUN arch="$(dpkg --print-architecture)" \
    && case "$arch" in \
        amd64) tf_sha="$TERRAFORM_SHA256_AMD64" ;; \
        arm64) tf_sha="$TERRAFORM_SHA256_ARM64" ;; \
        *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
       esac \
    && curl -fsSL -o /tmp/terraform.zip \
        "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_${arch}.zip" \
    && echo "$tf_sha  /tmp/terraform.zip" | sha256sum -c - \
    && unzip -q /tmp/terraform.zip -d /usr/local/bin terraform \
    && rm /tmp/terraform.zip

# uid 1000 matches the usual host user, so files written to the mounted checkout keep their owner.
RUN useradd --create-home --uid 1000 --shell /bin/bash agent \
    && mkdir -p /home/agent/.cache && chown agent:agent /home/agent/.cache

COPY allowed-hosts.txt egress.py init-firewall.sh entry.sh /usr/local/share/devcontainer/
RUN chmod 0755 /usr/local/share/devcontainer/init-firewall.sh /usr/local/share/devcontainer/entry.sh

# The container starts as root only to set the firewall; entry.sh then runs the command as
# `agent` with no capabilities and no way to gain any (no sudo in the image).
ENV UV_PROJECT_ENVIRONMENT=/home/agent/.venv-backend \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_CACHE_DIR=/home/agent/.cache/uv \
    npm_config_cache=/home/agent/.cache/npm \
    npm_config_update_notifier=false
ENTRYPOINT ["/usr/local/share/devcontainer/entry.sh"]
