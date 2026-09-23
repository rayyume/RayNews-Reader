FROM python:3.12-slim

# Install nginx, process supervision, and timezone data for log timestamps
ENV TZ=Asia/Shanghai
RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends nginx supervisor util-linux tzdata && \
    rm -rf /var/lib/apt/lists/* && \
    rm -f /etc/nginx/sites-enabled/default /etc/nginx/sites-available/default

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir --require-hashes -r requirements.txt

COPY *.py .
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY nginx-security-headers.conf /etc/nginx/snippets/raynews-security-headers.conf
COPY supervisord.conf /app/supervisord.conf
COPY frontend/ /usr/share/nginx/html/

RUN groupadd --system raynews && \
    useradd --system --gid raynews --create-home \
      --home-dir /home/raynews --shell /usr/sbin/nologin raynews && \
    mkdir -p /app/data /var/log/nginx /run/nginx && \
    chown -R raynews:raynews /app/data

ARG COMMIT_SHA=unknown
ARG FULL_VERSION_OVERRIDE=
COPY VERSION BETA_REVISION /app/
RUN VERSION=$(cat /app/VERSION) && \
    if [ -n "$FULL_VERSION_OVERRIDE" ]; then \
      FULL_VERSION="$FULL_VERSION_OVERRIDE"; \
      FULL_BUILD_VERSION="${FULL_VERSION_OVERRIDE}-${COMMIT_SHA}"; \
    else \
      BETA_REV=$(cat /app/BETA_REVISION) && \
      if [ "$BETA_REV" = "0" ]; then \
        FULL_VERSION="v${VERSION}"; \
      else \
        FULL_VERSION="v${VERSION}-beta.${BETA_REV}"; \
      fi; \
      FULL_BUILD_VERSION="${FULL_VERSION}-${COMMIT_SHA}"; \
    fi && \
    sed -i "s/{{VERSION}}/$VERSION/g" /usr/share/nginx/html/sw.js && \
    sed -i "s/{{COMMIT_SHA}}/$COMMIT_SHA/g" /usr/share/nginx/html/sw.js && \
    sed -i "s/{{FULL_VERSION}}/$FULL_VERSION/g" /usr/share/nginx/html/index.html && \
    sed -i "s/{{FULL_BUILD_VERSION}}/$FULL_BUILD_VERSION/g" /usr/share/nginx/html/index.html

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 80

CMD ["/entrypoint.sh"]
