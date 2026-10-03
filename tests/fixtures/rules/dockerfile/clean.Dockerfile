FROM node:22.4.0-slim AS build
FROM scratch
FROM build AS final
FROM ${BASE_IMAGE}
FROM python@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
# USER root   <- commented out
USER app
USER 1000:1000
USER 01
