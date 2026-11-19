FROM python:3.12-slim

# One image for all AI providers, LLM_PROVIDER in the ConfigMap picks one at runtime.
# The Cursor SDK is proprietary and bundles a helper program. To build without it
# (e.g. for an image you publish): docker build --build-arg WITH_CURSOR=false agent
ARG WITH_CURSOR=true

# no .pyc files, unbuffered logs (the pod has a read-only root filesystem)
# HOME=/tmp because that emptyDir is the only writable path, the Cursor SDK keeps state in $HOME
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp

# Debian security updates that came out after the base image was built (zlib1g, openssl, ...).
# Every rebuild picks up the latest.
RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get upgrade -y \
 && rm -rf /var/lib/apt/lists/* \
 && useradd -u 10001 agent

WORKDIR /app
COPY requirements.txt requirements-cursor.txt ./
COPY docker/patch_cursor_bridge.py /opt/build/patch_cursor_bridge.py

# Install with an up to date pip, patch the vulnerable undici inside the Cursor SDK (see the
# script), then remove pip again together with the copy python keeps for reinstalling it.
# Nothing needs pip at runtime and its CVEs would show up in every image scan.
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
# docker/ is build-time only
RUN rm -rf /app/docker
USER 10001

# 8000 internal API, 8080 Teams webhook (the Cloudflare tunnel target),
# 8443 activity webhook (HTTPS, only when a certificate is mounted)
EXPOSE 8000 8080 8443
CMD ["python", "main.py"]
