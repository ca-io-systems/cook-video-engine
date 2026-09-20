# Cook video engine

A headless fork of [Concat](https://github.com/jub0t/Concat), the open-source
video editor by Jareer and the Concat contributors. This repository is **not**
the Concat application and is not maintained or endorsed by the Concat project.
If you want the desktop editor, get it from
[jub0t/Concat](https://github.com/jub0t/Concat).

Cook ([trycook.ai](https://trycook.ai)) runs this engine on its servers as the
timeline, editing engine and exporter behind its browser video editor. Cook's
own software talks to it only through the JSON-RPC API in
`src/crates/concat-api`, which is the boundary the
[Concat Plugin Exception](LICENSE-EXCEPTIONS.md) describes.

## Licence and source

Everything here is licensed under **AGPL-3.0-or-later**, as upstream is; see
[`LICENSE`](LICENSE), [`LICENSE-EXCEPTIONS.md`](LICENSE-EXCEPTIONS.md) and
[`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md). Upstream's copyright and
SPDX notices are kept in every file. "Concat" and its logo are marks of the
Concat project ([`TRADEMARK.md`](TRADEMARK.md)); this fork uses the name only to
say what it is forked from, and ships none of the logos.

This repository is the complete corresponding source of the engine Cook
serves. The branch Cook builds from is `engine`; the engine's
`version` call reports the commit a running build was made from.

## What was changed, and when

Forked from upstream commit `28d94eae86d6177f0c090f50ddf383748a4b07a1`
(2026-09-19). Changes made from 2026-09-20:

- **Removed:** the desktop window (`crates/concat`), the Android wrapper
  (`crates/concat-android`), the speech crate (`crates/concat-speech`:
  whisper.cpp transcription and Kokoro text to speech), ONNX Runtime and every
  inference path in `concat-vision` and `concat-host` (person and object
  cutout models, the smart cutout brush), the compiled-in person model, the
  model download table and downloader, and the packaging, mobile and model
  mirror workflows and scripts that served them. The engine downloads nothing
  and runs no model.
- **Cutout masks are imported, never inferred.** `cutout.status` lists the
  source instants a timeline's cutouts still have no mask for; `cutout.import`
  takes a finished set of `<millis>.png` masks in, all of them or none, and
  records what made them. `export.run` is refused while any mask is missing,
  so footage with a cutout is never rendered untreated.
- **Inventories through the API.** The text preset inventory moved from the
  window crate into `concat-host` and is listed by `catalogue.textPresets`;
  clip animation names are listed by `catalogue.animations`.
- **Linux x86-64 compile fix** in `concat-media/src/ffi.rs` (the `va_list`
  parameter of the FFmpeg log callback). Upstream's own CI fails on it at the
  forked commit.

## Build and test

Rust 1.93 or newer, FFmpeg 8.1 shared development libraries (`FFMPEG_DIR`),
`libclang`, `pkg-config` and, on Linux, `libasound2-dev`.

```sh
cd src
cargo test --workspace
cargo build --release -p concat-cli
```

`cook/e2e/engine_e2e.py` drives a built `concat-cli` through a real import,
edit, preview and export and measures the exported file:

```sh
python3 cook/e2e/engine_e2e.py --cli src/target/release/concat-cli \
  --footage some-video-with-sound.mp4 --out /tmp/engine-e2e
```

## Serving it

```sh
concat-cli serve --json 127.0.0.1:7420 --token "$CONCAT_API_TOKEN"
```

One JSON-RPC 2.0 request per line; `version` first. See
[`ARCHITECTURE.md`](ARCHITECTURE.md) section 7 and
`src/crates/concat-api/src/message.rs` for every method.
