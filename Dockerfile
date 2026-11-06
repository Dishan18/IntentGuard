# The hosted demo: one container, one port. Node builds the two web apps and installs PayPal's MCP
# server; the runtime is Python (gateway + store) with Node alongside to run PayPal's MCP server.
#
#   docker build -t intentguard .
#   docker run --env-file .env -e INTENTGUARD_OWNER_KEY=<20+ chars> -p 10000:10000 intentguard

FROM node:22-bookworm-slim AS node

WORKDIR /src
COPY apps/store/package.json apps/store/package-lock.json apps/store/
COPY apps/console/package.json apps/console/package-lock.json apps/console/
COPY paypal-mcp/package.json paypal-mcp/package-lock.json paypal-mcp/
RUN cd apps/store && npm ci \
 && cd ../console && npm ci \
 && cd ../../paypal-mcp && npm ci --omit=dev
COPY apps/store apps/store
COPY apps/console apps/console
RUN cd apps/store && npm run build && cd ../console && npm run build


FROM python:3.11-slim-bookworm

# Node, to run PayPal's MCP server as the gateway's downstream (it needs libstdc++).
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 && rm -rf /var/lib/apt/lists/*
COPY --from=node /usr/local/bin/node /usr/local/bin/node
RUN node --version

WORKDIR /app
COPY backend/requirements.lock backend/
RUN pip install --no-cache-dir -r backend/requirements.lock

COPY backend/intentguard backend/intentguard
COPY docs/paypal-mcp-tools.json docs/
COPY --from=node /src/paypal-mcp paypal-mcp
COPY --from=node /src/apps/store/dist apps/store/dist
COPY --from=node /src/apps/console/dist apps/console/dist

# The package finds the web apps and data/ relative to the repo root (/app), so it runs from source.
ENV PYTHONPATH=/app/backend PYTHONUNBUFFERED=1 PORT=10000
RUN useradd --create-home intentguard && mkdir data && chown intentguard data
USER intentguard

EXPOSE 10000
CMD ["python", "-m", "intentguard.hosted"]
