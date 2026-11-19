FROM python:3.12-slim

# One image for every AI provider; LLM_PROVIDER in the ConfigMap picks one at runtime.
# The Cursor SDK has a proprietary licence and adds a bundled helper program. To leave it
# out (for example in an image you publish): docker build --build-arg WITH_CURSOR=false agent
ARG WITH_CURSOR=true

# No .pyc files and unbuffered logs: the pod runs with a read-only root filesystem.
# HOME=/tmp: the only writable place in the pod (an emptyDir); the Cursor SDK keeps its state there.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp

# Security updates for the base image's system packages (zlib1g, openssl, ...) that Debian
# published after the base image was built. Every rebuild picks up the newest ones.
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -u 10001 agent

WORKDIR /app
COPY requirements.txt requirements-cursor.txt ./
COPY docker/patch_cursor_bridge.py /opt/build/patch_cursor_bridge.py

# 1. Install the app's packages with a current pip.
# 2. Cursor SDK: replace the vulnerable undici in its bundled Node packages (see the script).
# 3. Remove pip, and the copy Python keeps for re-installing it. Nothing needs pip at runtime,
#    and its CVEs would otherwise show up in every image scan.
RUN python -m pip install --no-cache-dir --upgrade pip \
 && python -m pip install --no-cache-dir -r requirements.txt \
 && if [ "$WITH_CURSOR" = "true" ]; then \
      python -m pip install --no-cache-dir -r requirements-cursor.txt \
      && python /opt/build/patch_cursor_bridge.py; \
    fi \
 && python -m pip uninstall -y pip \
 && python -c "import os, shutil, sysconfig; shutil.rmtree(os.path.join(sysconfig.get_path('stdlib'), 'ensurepip', '_bundled'), ignore_errors=True)" \
 && rm -rf /opt/build /tmp/* /root/.cache

COPY --chown=agent:agent . .
# docker/ holds build-time helpers only
RUN rm -rf /app/docker
USER 10001

# 8000: internal API.  8080: Teams webhook only (Cloudflare Tunnel target).
# 8443: activity webhook (HTTPS, only with a mounted certificate)
EXPOSE 8000 8080 8443
CMD ["python", "main.py"]
