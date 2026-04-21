FROM python:3.13

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app
COPY ./requirements.txt /app

RUN uv pip install --system -r requirements.txt

COPY . /app
RUN uv pip install --system --no-deps .

ENV PORT=4000
EXPOSE $PORT

CMD ["sh", "-c", "python -m uvicorn eco_mcp_app.http_app:app --host 0.0.0.0 --port $PORT"]
