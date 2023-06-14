FROM frolvlad/alpine-python3

RUN pip3 install docker redis
RUN mkdir /hoster
WORKDIR /hoster
ADD hoster.py /hoster/

CMD ["python3", "-u", "hoster.py"]



