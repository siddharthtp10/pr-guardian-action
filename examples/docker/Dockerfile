# INTENTIONALLY INSECURE - a demo input for PR Guardian. Never copy this.
FROM node:latest

ENV DB_PASSWORD=hunter2hunter2

RUN curl -fsSL https://example.com/install.sh | sh

WORKDIR /app
COPY . .
RUN npm ci

USER root
CMD ["node", "server.js"]
