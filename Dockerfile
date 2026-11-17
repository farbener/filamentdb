FROM python:3.13-alpine
WORKDIR /app
COPY app/ /app/
# Run as the TrueNAS "apps" user (568) so files in /data match dataset permissions
USER 568:568
ENV DATA_DIR=/data PORT=8080
EXPOSE 8080
CMD ["python", "-u", "/app/server.py"]
