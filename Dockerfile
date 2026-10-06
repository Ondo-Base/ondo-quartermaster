# syntax=docker/dockerfile:1
# The control plane with the web UI, as one image.
#
#   docker build -t ondo-control-plane .
#   docker run -p 8787:8787 -v ondo-data:/data \
#     -e ONDO_COOKIE_SECRET=... -e ONDO_PUBLIC_URL=https://ondo.example.com ondo-control-plane
#
# The desktop agent is not in this image: it runs on each user's machine and is
# released as a Python package.
#
# Behind a TLS-inspecting proxy, pass its CA as a build secret. It is used for
# npm only and never stored in a layer:
#   docker build --secret id=ca_bundle,src=/path/to/ca.pem .

FROM node:26-bookworm-slim AS build
WORKDIR /app
COPY package.json package-lock.json ./
COPY server/package.json server/
COPY web/package.json web/
RUN --mount=type=secret,id=ca_bundle,required=false \
    if [ -f /run/secrets/ca_bundle ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/ca_bundle; fi; \
    npm ci --workspace server --workspace web --no-audit --no-fund
COPY server server
COPY web web
RUN npm run build --workspace web && npm run build --workspace server

FROM node:26-bookworm-slim AS runtime
ENV NODE_ENV=production \
    HOST=0.0.0.0 \
    PORT=8787 \
    ONDO_DB=/data/ondo.sqlite \
    ONDO_WEB_DIST=/app/web/dist
WORKDIR /app
COPY package.json package-lock.json ./
COPY server/package.json server/
COPY web/package.json web/
RUN --mount=type=secret,id=ca_bundle,required=false \
    if [ -f /run/secrets/ca_bundle ]; then export NODE_EXTRA_CA_CERTS=/run/secrets/ca_bundle; fi; \
    npm ci --workspace server --omit=dev --no-audit --no-fund && npm cache clean --force
COPY --from=build /app/server/dist server/dist
COPY --from=build /app/web/dist web/dist
RUN mkdir -p /data && chown node:node /data
USER node
VOLUME ["/data"]
EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD node -e "fetch('http://127.0.0.1:'+process.env.PORT+'/healthz').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"
CMD ["node", "server/dist/main.js"]
