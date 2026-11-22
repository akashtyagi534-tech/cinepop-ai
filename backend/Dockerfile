FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    curl \
    ca-certificates \
    fontconfig \
    libraqm0 \
    libfribidi0 \
    libharfbuzz0b \
    fonts-lohit-deva \
    && mkdir -p /app/fonts \
    && (curl -fsSL --retry 2 "https://raw.githubusercontent.com/notofonts/notofonts.github.io/main/fonts/NotoSansDevanagari/full/ttf/NotoSansDevanagari-Bold.ttf" -o /app/fonts/NotoSansDevanagari-Bold.ttf || true) \
    && (curl -fsSL --retry 2 "https://raw.githubusercontent.com/notofonts/notofonts.github.io/main/fonts/NotoSansDevanagari/full/ttf/NotoSansDevanagari-Regular.ttf" -o /app/fonts/NotoSansDevanagari-Regular.ttf || true) \
    && fc-cache -fv \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN python -m py_compile main.py && mkdir -p /app/all_movies /app/all_face_swaps && chmod 777 /app/all_movies /app/all_face_swaps

EXPOSE 7860 8000 8080
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-7860}"]
