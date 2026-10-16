# Debian-based so every Python dependency installs as a prebuilt wheel
# (nothing is compiled on the Home Assistant machine).
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

COPY requirements.txt /tmp/requirements.txt
RUN pip install --only-binary=:all: -r /tmp/requirements.txt

# go2rtc serves the RTSP streams and starts/stops the camera sessions on demand.
ARG GO2RTC_VERSION=1.9.14
ARG GO2RTC_SHA256_AMD64=32d616af226bd731678ffde328b94cfb94e30339bfefc469cfb76323144615a6
ARG GO2RTC_SHA256_ARM64=359fabade8a7a51e81a55fe6df6b0ef81764a5e1d63179577534eaaa71904b50
RUN set -eux; \
    case "$(uname -m)" in \
      aarch64|arm64) arch=arm64; sum="${GO2RTC_SHA256_ARM64}" ;; \
      x86_64|amd64) arch=amd64; sum="${GO2RTC_SHA256_AMD64}" ;; \
      *) echo "Unsupported architecture: $(uname -m)"; exit 1 ;; \
    esac; \
    python3 -c "import sys, urllib.request; urllib.request.urlretrieve(sys.argv[1], '/usr/local/bin/go2rtc')" \
      "https://github.com/AlexxIT/go2rtc/releases/download/v${GO2RTC_VERSION}/go2rtc_linux_${arch}"; \
    echo "${sum}  /usr/local/bin/go2rtc" | sha256sum -c -; \
    chmod +x /usr/local/bin/go2rtc; \
    go2rtc -version

COPY app /app

CMD ["python3", "-u", "/app/run.py"]
