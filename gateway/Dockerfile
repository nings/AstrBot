FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir uv
COPY pyproject.toml README.md ./
COPY astrbot_gateway ./astrbot_gateway
RUN uv pip install --system --no-cache .

ENV GATEWAY_DATA_DIR=/data
VOLUME ["/data"]
EXPOSE 8080
CMD ["python", "-m", "astrbot_gateway"]
