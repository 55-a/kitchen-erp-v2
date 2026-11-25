# Образ API + бота Kuxnya NVGR
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Шрифты для PDF: DejaVuSans (кириллица/латиница) и WenQuanYi Micro Hei (китайский, TrueType —
# Noto CJK с CFF-контурами fpdf2 встраивает неверно, вместо иероглифов выходят цифры)
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-dejavu-core fonts-wqy-microhei \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Сначала зависимости — слой кэшируется, пока requirements.txt не изменился
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY . .

# Запуск от непривилегированного пользователя
RUN useradd --create-home --uid 1000 app && chown -R app:app /app
USER app

# Railway передаёт порт в переменной PORT
ENV PORT=8000
EXPOSE 8000

# Один воркер: бот (polling) запускается внутри процесса API и не должен дублироваться
CMD ["sh", "-c", "uvicorn api.main:app --host 0.0.0.0 --port ${PORT} --proxy-headers --forwarded-allow-ips='*'"]
