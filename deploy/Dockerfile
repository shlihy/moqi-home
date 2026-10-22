FROM python:3.13-slim-bookworm@sha256:a881a81282cca8c57f97014867fa651def1b3799030cc6b1cff45ed55ba8ef36
WORKDIR /app
ARG MOQI_AUDIO=0
RUN apt-get update && apt-get install -y --no-install-recommends git libportaudio2 libasound2 && rm -rf /var/lib/apt/lists/*
COPY requirements/itx-runtime.lock.txt requirements/itx-audio.lock.txt ./requirements/
RUN case "$MOQI_AUDIO" in \
      0) pip install --no-cache-dir --require-hashes -r requirements/itx-runtime.lock.txt ;; \
      1) pip install --no-cache-dir --require-hashes -r requirements/itx-audio.lock.txt ;; \
      *) exit 1 ;; \
    esac
RUN pip install --no-cache-dir --no-deps 'edgejev @ git+https://github.com/yzfly/edgejev.git@dce469472e0e6eea396256e9bf866082dcb9490b'
COPY pyproject.toml README.md ./
COPY moqi ./moqi
RUN pip install --no-cache-dir --no-deps .
CMD ["python", "-m", "moqi.acceptance", "--help"]
