# INTENTIONALLY INSECURE - a demo input for PR Guardian. Never copy this.
FROM node:latest

WORKDIR /app
COPY . .
RUN npm ci

USER root
CMD ["node", "server.js"]
