FROM netboxcommunity/netbox:latest

RUN uv pip install --python /opt/netbox/venv/bin/python3 --no-cache \
    black "granian[reload]" pytest pytest-django playwright

# The browser tests (tests/browser.py) run in Chromium.
RUN apt-get update -qq && apt-get install -y -qq libatomic1 \
    && /opt/netbox/venv/bin/playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

COPY .devcontainer/docker-entrypoint-dev.sh /docker-entrypoint-dev.sh
RUN chmod +x /docker-entrypoint-dev.sh

ENTRYPOINT ["/usr/bin/tini", "--", "/docker-entrypoint-dev.sh"]
CMD ["/opt/netbox/docker-entrypoint.sh", "/opt/netbox/launch-netbox.sh"]
