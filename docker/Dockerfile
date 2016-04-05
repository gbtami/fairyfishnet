FROM debian:8.3

MAINTAINER Sylvain Ageneau <ageneau@gmail.com>

RUN apt-get update && apt-get install -y --no-install-recommends python python-pip
RUN pip install --upgrade pip

RUN useradd --create-home --user-group fishnet
USER fishnet:fishnet
WORKDIR /home/fishnet/

RUN pip install --user fishnet

ENTRYPOINT ["python", "-m", "fishnet", "--no-conf"]
