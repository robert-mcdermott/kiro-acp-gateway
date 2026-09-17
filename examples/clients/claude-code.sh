#!/bin/sh
# Run Claude Code against the gateway. Both ANTHROPIC_API_KEY and ANTHROPIC_AUTH_TOKEN are
# needed when Claude Code is logged in to a Claude account (it otherwise sends its OAuth token).
export ANTHROPIC_BASE_URL="${KIRO_GATEWAY_URL:-http://127.0.0.1:8000}"
export ANTHROPIC_API_KEY="${KIRO_GATEWAY_KEY:-your-gateway-key}"
export ANTHROPIC_AUTH_TOKEN="$ANTHROPIC_API_KEY"
export ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-claude-sonnet-4.6}"
export ANTHROPIC_SMALL_FAST_MODEL="${ANTHROPIC_SMALL_FAST_MODEL:-gpt-5.6-luna}"
exec claude "$@"
