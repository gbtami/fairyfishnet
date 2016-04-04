FROM debian:8.3

MAINTAINER Sylvain Ageneau <ageneau@gmail.com>

RUN apt-get update && apt-get install -y --no-install-recommends python python-pip
RUN useradd --create-home --user-group fishnet
RUN pip install --upgrade fishnet

USER fishnet:fishnet
WORKDIR /home/fishnet/

ENTRYPOINT ["python", "-m", "fishnet", "--no-conf"]
