FROM mcr.microsoft.com/playwright/python:v1.61.0-noble

USER root
WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PROFILE_DIR=/profile

# headless 模式：仅需 gosu 降权；不装虚拟显示与远程桌面组件（登录在本地做、cookie 上传）。
RUN apt-get update \
    && apt-get install -y --no-install-recommends gosu \
    && rm -rf /var/lib/apt/lists/*

COPY leonardo_refresher/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY leonardo_refresher /app/leonardo_refresher
RUN chmod 0755 /app/leonardo_refresher/entrypoint.sh \
    && mkdir -p /profile \
    && chown pwuser:pwuser /profile

EXPOSE 8080

ENTRYPOINT ["/app/leonardo_refresher/entrypoint.sh"]
