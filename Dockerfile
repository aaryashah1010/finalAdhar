FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DISPLAY=:99 \
    QT_X11_NO_MITSHM=1 \
    AADHAAR_INPUT_DIR=/data/input \
    AADHAAR_OUTPUT_DIR=/data/output

RUN apt-get update && apt-get install -y --no-install-recommends \
    fluxbox \
    fonts-dejavu \
    libegl1 \
    libgl1 \
    libglib2.0-0 \
    libxkbcommon-x11-0 \
    libxcb-cursor0 \
    libxcb-icccm4 \
    libxcb-image0 \
    libxcb-keysyms1 \
    libxcb-randr0 \
    libxcb-render-util0 \
    libxcb-shape0 \
    libxcb-xinerama0 \
    novnc \
    tesseract-ocr \
    tesseract-ocr-eng \
    tesseract-ocr-guj \
    tesseract-ocr-hin \
    tesseract-ocr-mar \
    websockify \
    x11vnc \
    xvfb \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
COPY requirements-docker.txt .
RUN pip install --no-cache-dir -r requirements-docker.txt

COPY app.py .
COPY src ./src
COPY docker-entrypoint.sh /usr/local/bin/aadhaar-entrypoint

RUN chmod +x /usr/local/bin/aadhaar-entrypoint \
    && mkdir -p /data/input /data/output

EXPOSE 6080

ENTRYPOINT ["aadhaar-entrypoint"]
