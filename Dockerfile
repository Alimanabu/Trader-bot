FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY trader ./trader
COPY web ./web
RUN mkdir -p /app/data
ENV DB_PATH=/app/data/trader.db PORT=8080
EXPOSE 8080
CMD ["python", "-m", "trader", "serve"]
