# Surrogate-1 on Hugging Face Spaces (CPU 16 GB)
# Single-container that runs Ollama + Redis + all Surrogate daemons.
FROM python:3.12-slim

# ── System deps ──────────────────────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash curl wget git ca-certificates jq sqlite3 redis-server \
    ripgrep fswatch procps net-tools zstd \
    && rm -rf /var/lib/apt/lists/*

# ── Ollama (CPU build for ARM/x86) ──────────────────────────────────────────
RUN curl -fsSL https://ollama.com/install.sh | sh

# ── App user (HF Spaces requires uid 1000) ──────────────────────────────────
RUN useradd -m -u 1000 hermes
ENV HOME=/home/hermes \
    PATH=/home/hermes/.surrogate/bin:/home/hermes/.local/bin:/usr/local/bin:/usr/bin:/bin \
    SURROGATE_HOME=/home/hermes/.surrogate \
    HERMES_HOME=/home/hermes/.hermes \
    PYTHONUNBUFFERED=1

WORKDIR /home/hermes

# ── Python deps for Discord bot + scrape + RAG ──────────────────────────────
COPY --chown=hermes:hermes requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# ── Copy Surrogate scripts + agents + config skeleton ──────────────────────
# Surrogate's home: ~/.surrogate/bin/  (separate from Claude Code's ~/.claude/)
COPY --chown=hermes:hermes bin/ /home/hermes/.surrogate/bin/
COPY --chown=hermes:hermes agents/ /home/hermes/.surrogate/agents/
COPY --chown=hermes:hermes config/ /home/hermes/.hermes/config/
COPY --chown=hermes:hermes start.sh /home/hermes/start.sh
RUN chmod +x /home/hermes/.surrogate/bin/*.sh /home/hermes/start.sh

USER hermes

# ── Persistent dirs (HF mounts /data into ~/.surrogate symlink) ─────────────
RUN mkdir -p /home/hermes/.surrogate/state /home/hermes/.surrogate/logs \
    /home/hermes/.surrogate/workspace /home/hermes/.surrogate/memory \
    /home/hermes/.surrogate/skills /home/hermes/.surrogate/sessions \
    /home/hermes/.hermes/workspace /home/hermes/.ollama

# ── Backward-compat: legacy refs to ~/.claude/bin/ + ~/.claude/logs/ ────────
# Some scripts still reference old paths; symlink prevents breakage during
# progressive migration. Eventually all callers should use ~/.surrogate/.
RUN mkdir -p /home/hermes/.claude && \
    ln -sfn /home/hermes/.surrogate/bin /home/hermes/.claude/bin && \
    ln -sfn /home/hermes/.surrogate/logs /home/hermes/.claude/logs && \
    ln -sfn /home/hermes/.surrogate/state /home/hermes/.claude/state

# ── Expose port 7860 (HF default) ────────────────────────────────────────────
EXPOSE 7860

CMD ["/home/hermes/start.sh"]
