FROM python:3.13-alpine
WORKDIR /app
# openssl is only used to create the optional self-signed certificate (TLS_SELF_SIGNED=1)
RUN apk add --no-cache openssl
COPY app/ /app/
# Run as the TrueNAS "apps" user (568) so files in /data match dataset permissions
USER 568:568
ENV DATA_DIR=/data PORT=8080
EXPOSE 8080
CMD ["python", "-u", "/app/server.py"]
