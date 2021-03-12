FROM python:3.9.2-buster

WORKDIR /usr/src/app

COPY . .

ENTRYPOINT ["/usr/src/app/bors.py"]
