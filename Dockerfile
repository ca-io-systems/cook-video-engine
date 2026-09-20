# The Cook video engine: `concat-cli serve` and FFmpeg, nothing else.
# Build:  docker build --build-arg COOK_ENGINE_COMMIT=$(git rev-parse HEAD) -t cook-video-engine .
FROM rust:1-bookworm AS build
ARG FFMPEG_LINUX=https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n8.1-latest-linux64-gpl-shared-8.1.tar.xz
ARG COOK_ENGINE_COMMIT=""
RUN apt-get update && apt-get install -y --no-install-recommends pkg-config libclang-dev clang libasound2-dev xz-utils curl \
    && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /opt/ffmpeg && curl -fsSL --retry 5 --retry-all-errors -o /tmp/ffmpeg.tar.xz "$FFMPEG_LINUX" \
    && tar -xf /tmp/ffmpeg.tar.xz -C /opt/ffmpeg --strip-components=1 && rm /tmp/ffmpeg.tar.xz
ENV FFMPEG_DIR=/opt/ffmpeg LD_LIBRARY_PATH=/opt/ffmpeg/lib COOK_ENGINE_COMMIT=$COOK_ENGINE_COMMIT
WORKDIR /engine
COPY src ./src
RUN cargo build --release -p concat-cli --manifest-path src/Cargo.toml

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends libasound2 ca-certificates fontconfig fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/* && useradd --create-home --uid 10001 engine
COPY --from=build /opt/ffmpeg/lib /opt/ffmpeg/lib
COPY --from=build /opt/ffmpeg/bin/ffmpeg /opt/ffmpeg/bin/ffprobe /usr/local/bin/
COPY --from=build /engine/src/target/release/concat-cli /usr/local/bin/concat-cli
COPY LICENSE LICENSE-EXCEPTIONS.md THIRD_PARTY_NOTICES.md README.md /usr/share/doc/cook-video-engine/
ENV LD_LIBRARY_PATH=/opt/ffmpeg/lib
USER engine
WORKDIR /home/engine
EXPOSE 7420
# CONCAT_API_TOKEN must be set by whoever runs the container; the server mints
# one and prints it when it is not.
CMD ["concat-cli", "serve", "--json", "0.0.0.0:7420"]
