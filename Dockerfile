FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV TZ=Asia/Bangkok

RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-noto-core ca-certificates ffmpeg curl \
    && mkdir -p /usr/share/fonts/truetype/prompt \
    && curl -L https://github.com/google/fonts/raw/main/ofl/prompt/Prompt-Regular.ttf -o /usr/share/fonts/truetype/prompt/Prompt-Regular.ttf \
    && curl -L https://github.com/google/fonts/raw/main/ofl/prompt/Prompt-Bold.ttf -o /usr/share/fonts/truetype/prompt/Prompt-Bold.ttf \
    && fc-cache -f -v \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .

EXPOSE 10000

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-10000}"]
