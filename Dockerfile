FROM python:slim

LABEL maintainer="Manuel Klemenz <manuel.klemenz@gmail.com>"

COPY --from=ghcr.io/astral-sh/uv:0.12.5 /uv /uvx /bin/

WORKDIR /tmp/fishnet/
RUN uv pip install --system dumb-init fairyfishnet

ENTRYPOINT ["dumb-init", "--", "python", "-m", "fairyfishnet", "--no-conf"]
