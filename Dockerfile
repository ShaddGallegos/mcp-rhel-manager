FROM mcr.microsoft.com/vscode/devcontainers/python:0-3.11

# Install lightweight system dependencies commonly needed for this repo
RUN apt-get update \
  && apt-get install -y --no-install-recommends \
    openssh-client \
    sshpass \
    git \
    build-essential \
    curl \
    ca-certificates \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

# Copy and install Python dependencies
COPY requirements.txt /tmp/requirements.txt
RUN pip install --upgrade pip \
  && pip install -r /tmp/requirements.txt

CMD ["sleep", "infinity"]
