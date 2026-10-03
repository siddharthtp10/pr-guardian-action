FROM node:latest # EXPECT:DOCKER-001
FROM --platform=linux/amd64 python:latest AS build # EXPECT:DOCKER-001
FROM registry.example.com:5000/team/base:latest # EXPECT:DOCKER-001
RUN npm ci
USER root # EXPECT:DOCKER-002
USER 0 # EXPECT:DOCKER-002
