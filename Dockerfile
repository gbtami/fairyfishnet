FROM python:slim

LABEL maintainer "Manuel Klemenz <manuel.klemenz@gmail.com>"

WORKDIR /tmp/fishnet/
RUN pip install dumb-init && \
    pip install fairyfishnet

ENTRYPOINT ["dumb-init", "--", "python", "-m", "fairyfishnet", "--no-conf"]
