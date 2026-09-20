#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Makes a fresh Ubuntu 24.04 machine able to build and test this fork: build tools, Rust, the FFmpeg 8.1
# shared development libraries the bindings need, and a clone of the engine branch. It mirrors what
# .github/workflows/ci.yml installs. Run as the user that will build; it uses sudo for packages.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

sudo apt-get update -qq
sudo apt-get install -y -qq build-essential pkg-config libclang-dev clang libasound2-dev git curl xz-utils \
  python3 fonts-dejavu-core >/dev/null

if ! command -v cargo >/dev/null 2>&1; then
  curl -fsSL https://sh.rustup.rs | sh -s -- -y --profile minimal --component clippy,rustfmt >/dev/null
fi

FF=/opt/ffmpeg-dev
if [ ! -d "$FF/lib" ]; then
  sudo mkdir -p "$FF" && sudo chown "$USER" "$FF"
  curl -fsSL --retry 5 --retry-all-errors -o /tmp/ffmpeg.tar.xz \
    https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-n8.1-latest-linux64-gpl-shared-8.1.tar.xz
  tar -xf /tmp/ffmpeg.tar.xz -C "$FF" --strip-components=1
fi

mkdir -p "$HOME/engine" && cd "$HOME/engine"
[ -d cook-video-engine/.git ] || git clone --quiet --branch engine https://github.com/ca-io-systems/cook-video-engine.git

cat <<'NOTE'
Ready. In every shell that builds or tests:
  export FFMPEG_DIR=/opt/ffmpeg-dev LD_LIBRARY_PATH=/opt/ffmpeg-dev/lib PATH=/opt/ffmpeg-dev/bin:$HOME/.cargo/bin:$PATH
Then:
  cd ~/engine/cook-video-engine/src && cargo test --workspace
  COOK_ENGINE_COMMIT=$(git rev-parse HEAD) cargo build --release -p concat-cli
NOTE
