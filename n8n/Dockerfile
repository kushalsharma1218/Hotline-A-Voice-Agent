# n8n with a JSON Schema validator available to Code nodes
# (PRD §6.5 "Validate"; contracts.md Amendment 3).
# 2.40.5 = the n8nio/n8n "stable" tag on Docker Hub as of 2026-09-23.
FROM n8nio/n8n:2.40.5

# @cfworker/json-schema: Draft 2020-12, interprets schemas without code
# generation, so it works inside n8n 2.x's hardened Code-node runner
# (Ajv does not: the runner forbids new Function()).
# The image runs as "node"; global npm install needs root. Global modules land
# in /usr/local/lib/node_modules, already on the image's NODE_PATH, which the
# task runner inherits.
USER root
RUN npm install -g --omit=dev @cfworker/json-schema@4.1.1 \
 && npm cache clean --force
USER node

# n8n's require allowlist is an exact match on the request string.
# Usage: const { Validator } = require('@cfworker/json-schema');
# Compose sets this too (from .env); this is the fallback default.
ENV NODE_FUNCTION_ALLOW_EXTERNAL=@cfworker/json-schema
