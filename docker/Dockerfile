FROM debian:8.3

MAINTAINER Sylvain Ageneau <ageneau@gmail.com>

RUN apt-get update && apt-get install -y --no-install-recommends python python-pip
RUN useradd --create-home --user-group fishnet

USER fishnet:fishnet
WORKDIR /home/fishnet/
RUN pip install --user --upgrade fishnet==1.3.5

COPY update-and-start.sh /home/fishnet/update-and-start.sh


ENTRYPOINT ["/home/fishnet/update-and-start.sh"]
