FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS tunnel
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update --error-on=any \
    && apt-get install -y --no-install-recommends openssh-client netcat-openbsd \
    && rm -rf /var/lib/apt/lists/*
ENTRYPOINT ["ssh"]

FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS bot
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock && useradd --uid 10001 --create-home relaypost
COPY relaypost ./relaypost
RUN mkdir /data && chown relaypost /data
USER relaypost
ENV RELAYPOST_DATABASE=/data/relaypost.db
CMD ["python", "-m", "relaypost"]
