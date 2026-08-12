FROM ghcr.io/agentslastexam/sandbox-base-cli:latest

RUN npm install -g \
        @openai/codex@0.146.0 \
        @anthropic-ai/claude-code@2.1.227 \
    && codex --version \
    && claude --version
