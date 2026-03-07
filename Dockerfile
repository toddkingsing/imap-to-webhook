FROM python:3.14-slim AS build

WORKDIR /usr/src/app
COPY requirements.txt ./
RUN apt-get update \
 && apt-get install -y --no-install-recommends g++ \
 && pip install --no-cache-dir --upgrade pip \
 && pip install --no-cache-dir -r requirements.txt \
 && apt-get remove -y g++ && apt-get autoremove -y \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*
ENV PYTHONUNBUFFERED=1
COPY . .

# Testing: build fails if tests fail
FROM build AS testing
RUN python test.py

# Production image
FROM build
RUN adduser --disabled-password --gecos "" appuser
USER appuser
HEALTHCHECK --interval=120s --timeout=5s --retries=3 --start-period=30s \
    CMD python healthcheck.py
CMD ["python", "daemon.py"]
