# Devcontainer for agent sessions that run `make check` (CHB-31).
# Python 3.15 and uv match compose.yaml; Node 24 matches .nvmrc; Terraform matches
# infra/github/.terraform-version. The image build has full network. The egress
# limit starts with the container (init-firewall.sh).
FROM ghcr.io/astral-sh/uv:0.13-python3.15-trixie-slim

ARG TERRAFORM_VERSION=1.17.1
ARG CLAUDE_CODE_VERSION=2.1.322

COPY --from=node:24-trixie-slim /usr/local/ /usr/local/

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl dnsutils git gh iproute2 ipset iptables jq make sudo unzip \
    && rm -rf /var/lib/apt/lists/*

RUN arch="$(dpkg --print-architecture)" \
    && curl -fsSL -o /tmp/terraform.zip \
        "https://releases.hashicorp.com/terraform/${TERRAFORM_VERSION}/terraform_${TERRAFORM_VERSION}_linux_${arch}.zip" \
    && unzip -q /tmp/terraform.zip -d /usr/local/bin terraform \
    && rm /tmp/terraform.zip \
    && npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"

RUN useradd --create-home --uid 1000 --shell /bin/bash agent \
    && mkdir -p /workspace && chown agent:agent /workspace

COPY allowed-hosts.txt egress.py init-firewall.sh /usr/local/share/devcontainer/
RUN chmod 0755 /usr/local/share/devcontainer/init-firewall.sh \
    && printf 'agent ALL=(root) NOPASSWD: /usr/local/share/devcontainer/init-firewall.sh\n' > /etc/sudoers.d/agent-firewall \
    && chmod 0440 /etc/sudoers.d/agent-firewall

USER agent
WORKDIR /workspace
ENV UV_PROJECT_ENVIRONMENT=/home/agent/.venv-backend \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
