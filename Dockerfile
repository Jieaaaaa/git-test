FROM agenthack/base:1.1.1

ARG AGENT_ROLE
ARG SOURCE_REVISION=unknown
LABEL org.opencontainers.image.revision=${SOURCE_REVISION}
ENV AGENT_ROLE=${AGENT_ROLE}
WORKDIR /app
COPY --chown=agent:agent wire.py model_policy.py semantic_model.py agent_app.py ./
USER agent
EXPOSE 8080/tcp 9000/udp
CMD ["python", "/app/agent_app.py"]
