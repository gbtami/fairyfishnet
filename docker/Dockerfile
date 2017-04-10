FROM python:slim

LABEL maintainer "Sylvain Ageneau <ageneau@gmail.com>"

WORKDIR /tmp/fishnet/
RUN pip install fishnet

ENTRYPOINT ["python", "-m", "fishnet", "--no-conf"]
