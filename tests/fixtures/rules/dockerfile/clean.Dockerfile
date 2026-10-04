FROM node:22.4.0-slim AS build
FROM scratch
FROM build AS final
FROM ${BASE_IMAGE}
FROM python@sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
# USER root   <- commented out
USER app
USER 1000:1000
USER 01
RUN curl -fsSL -o /tmp/install.sh https://example.com/install.sh && sha256sum -c checksums.txt
RUN curl -fsSL https://example.com/tool.tgz | tar -xz -C /opt
RUN curl -fsSL https://example.com/file | sha256sum -c -
