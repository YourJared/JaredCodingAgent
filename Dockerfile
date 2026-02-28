FROM node:20-slim

# Install system deps
RUN apt-get update && apt-get install -y \
    python3 \
    python3-pip \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code
RUN npm install -g @anthropic-ai/claude-code

# Install Python deps
WORKDIR /app
COPY requirements.txt .
RUN pip3 install -r requirements.txt --break-system-packages

# Copy daemon
COPY agent.py .

# Git config (identity needed to push PRs)
RUN git config --global user.email "jared-agent@yourjared.com" && \
    git config --global user.name "Jared Coding Agent"

ENV PYTHONUNBUFFERED=1

CMD ["python3", "agent.py"]
