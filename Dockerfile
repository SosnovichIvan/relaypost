FROM python:3.12-slim
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock && useradd --uid 10001 --create-home relaypost
COPY relaypost ./relaypost
RUN mkdir /data && chown relaypost /data
USER relaypost
ENV RELAYPOST_DATABASE=/data/relaypost.db
CMD ["python", "-m", "relaypost"]
