#!/bin/sh
# Minimal calls against the gateway. Requires: curl. Set URL/KEY for your gateway.
URL="${KIRO_GATEWAY_URL:-http://127.0.0.1:8000}"
KEY="${KIRO_GATEWAY_KEY:-your-gateway-key}"

echo "# models"
curl -s "$URL/v1/models" -H "Authorization: Bearer $KEY" | head -c 400; echo

echo "# OpenAI chat completion"
curl -s "$URL/v1/chat/completions" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"claude-sonnet-4.6","messages":[{"role":"user","content":"Say hello in five words."}]}'
echo

echo "# Anthropic messages"
curl -s "$URL/v1/messages" -H "x-api-key: $KEY" -H "anthropic-version: 2023-06-01" -H "Content-Type: application/json" \
  -d '{"model":"claude-sonnet-4.6","max_tokens":256,"messages":[{"role":"user","content":"Say hello in five words."}]}'
echo
