FROM python:3.11-slim

WORKDIR /app

# System deps for PDF generation (if generate_v5 uses reportlab/weasyprint)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libcairo2 libpango-1.0-0 libpangocairo-1.0-0 libgdk-pixbuf2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Bot code
COPY bot_v3.py ./bot.py

# Report generation template + assets
COPY generate_v5_newtempl.py .
COPY tpl_v2/ ./tpl_v2/
COPY fonts/ ./fonts/

# Data dirs
RUN mkdir -p /app/data/photos /app/data/backups

# Symlinks so paths work from both /app/ and /app/data/
RUN ln -sf /app/fonts /app/data/fonts && \
    ln -sf /app/tpl_v2 /app/data/tpl_v2

ENV REPORT_DIR=/app/data
ENV ASSETS_DIR=/app

CMD ["python3", "bot.py"]
