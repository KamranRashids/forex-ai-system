# syntax=docker/dockerfile:1
# ============================================================================
# Forex AI System — frontend image (multi-stage Next.js standalone, non-root)
# Targets: deps -> dev | builder -> prod
# ============================================================================

FROM node:24-alpine AS deps
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci

FROM deps AS dev
ENV NEXT_TELEMETRY_DISABLED=1
COPY . .
EXPOSE 3000
CMD ["npm", "run", "dev"]

FROM deps AS builder
ENV NEXT_TELEMETRY_DISABLED=1
ARG NEXT_PUBLIC_API_URL=
ARG NEXT_PUBLIC_WS_URL=
# Bake same-origin (empty) endpoints by default so API/WS flow through the
# NGINX edge under CSP connect-src 'self'. Explicit values only when a
# deployment intentionally points the browser at another origin.
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL \
    NEXT_PUBLIC_WS_URL=$NEXT_PUBLIC_WS_URL
COPY . .
RUN npm run build

FROM node:24-alpine AS prod
WORKDIR /app
# F-04 remediation (CVE-2026-14456, HIGH): the base image's baked OpenSSL
# (libcrypto3/libssl3 3.5.7-r0) predates the Alpine v3.24 fix. Upgrade only the
# OpenSSL package family to the patched Alpine build (3.5.8-r0) from the
# pinned v3.24 repos at image-build time. Targeted packages only - no blanket
# `apk upgrade`, no Node or app-dependency changes.
RUN apk add --no-cache --upgrade openssl libcrypto3 libssl3
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    PORT=3000 \
    HOSTNAME=0.0.0.0
COPY --from=builder --chown=node:node /app/.next/standalone ./
COPY --from=builder --chown=node:node /app/.next/static ./.next/static
COPY --from=builder --chown=node:node /app/public ./public
USER node
EXPOSE 3000
CMD ["node", "server.js"]
