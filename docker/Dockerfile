FROM debian:8.3

MAINTAINER Sylvain Ageneau <ageneau@gmail.com>

RUN apt-get update
RUN apt-get install -y --no-install-recommends python python-pip
RUN pip install --upgrade fishnet

COPY fishnet.ini /root/
COPY run_fishnet /usr/local/bin/

WORKDIR /root

CMD ["run_fishnet"]
