FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY snitch ./snitch
RUN pip install --no-cache-dir ".[llm]"
ENV SNITCH_STATE_DIR=/data
VOLUME /data
ENTRYPOINT ["snitch"]
CMD ["run"]
