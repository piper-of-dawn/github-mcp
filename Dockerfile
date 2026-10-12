# No remote syntax directive: use only Docker's already installed frontend.
ARG RUNTIME_BASE_IMAGE
FROM ${RUNTIME_BASE_IMAGE}
USER 0:0
COPY --chmod=0755 github-mcp-server /usr/local/bin/github-mcp-server
COPY --chmod=0755 entrypoint.sh /opt/github-mcp/entrypoint.sh
COPY --chmod=0755 tests/test_startup.sh /opt/github-mcp/tests/test_startup.sh
COPY --chmod=0644 tests/test_mcp.py /opt/github-mcp/tests/test_mcp.py
RUN ["/bin/bash", "-ec", "command -v python3; command -v mkdir; command -v chmod; python3 -c 'import ssl; assert ssl.create_default_context().get_ca_certs()'; mkdir -p /work/repo /work/tmp /work/logs; chmod 1777 /work/tmp /work/logs"]
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /opt/github-mcp
USER 65532:65532
EXPOSE 8082
STOPSIGNAL SIGTERM
ENTRYPOINT ["/opt/github-mcp/entrypoint.sh"]
