FROM python:3.12-slim

# One image for every AI provider; LLM_PROVIDER in the ConfigMap picks one at runtime.
# The Cursor SDK has a proprietary licence and adds a bundled helper program. To leave it
# out (for example in an image you publish): docker build --build-arg WITH_CURSOR=false agent
ARG WITH_CURSOR=true

# No .pyc files and unbuffered logs: the pod runs with a read-only root filesystem.
# HOME=/tmp: the only writable place in the pod (an emptyDir); the Cursor SDK keeps its state there.
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp

RUN useradd -u 10001 agent
WORKDIR /app
COPY requirements.txt requirements-cursor.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
 && if [ "$WITH_CURSOR" = "true" ]; then pip install --no-cache-dir -r requirements-cursor.txt; fi
COPY --chown=agent:agent . .
USER 10001

# 8000: internal API.  8080: Teams webhook only (Cloudflare Tunnel target).
# 8443: activity webhook (HTTPS, only with a mounted certificate)
EXPOSE 8000 8080 8443
CMD ["python", "main.py"]
