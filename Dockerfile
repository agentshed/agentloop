FROM ghcr.io/nvidia/openshell/sandbox:latest

# ---- Extension point: system packages ----
# RUN apt-get update && apt-get install -y --no-install-recommends \
#     build-essential \
#     && rm -rf /var/lib/apt/lists/*

# ---- Install uv (Python package manager) ----
RUN curl -LsSf https://astral.sh/uv/install.sh | sh \
    && ln -sf /root/.local/bin/uv /usr/local/bin/uv \
    && ln -sf /root/.local/bin/uvx /usr/local/bin/uvx

# ---- Extension point: Python packages ----
# RUN uv pip install --system numpy pandas

# ---- Extension point: Node packages ----
# RUN npm install -g typescript

WORKDIR /sandbox
