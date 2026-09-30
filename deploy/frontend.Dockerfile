# deploy/frontend.Dockerfile — build the React app, serve it with Caddy (TLS + same-origin proxy).
FROM node:20-alpine AS build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend/ ./
# Optional overrides; empty = same origin (/api and /livekit through Caddy).
ARG VITE_API_BASE=""
ARG VITE_LIVEKIT_URL=""
ENV VITE_API_BASE=$VITE_API_BASE VITE_LIVEKIT_URL=$VITE_LIVEKIT_URL
RUN npm run build

FROM caddy:2-alpine
COPY deploy/Caddyfile /etc/caddy/Caddyfile
COPY --from=build /app/dist /srv
