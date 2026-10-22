FROM python:3.12-slim

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY scripts/fetch-probe-core.sh scripts/fetch-installer.sh /app/scripts/
RUN sh /app/scripts/fetch-probe-core.sh

COPY app /app/app
COPY scripts/fetch-geoip.py /app/scripts/
RUN python /app/scripts/fetch-geoip.py
RUN sh /app/scripts/fetch-installer.sh

ENV PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai

EXPOSE 18899

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "18899"]
