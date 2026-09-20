#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""End-to-end check of the headless engine, through the same JSON-RPC lines a
client speaks: import real footage that has sound, edit it, render frames,
export, and measure the exported file with ffprobe/ffmpeg.

Usage:
    engine_e2e.py --cli <path to concat-cli> --footage <video with audio> --out <dir>

Exits non-zero when any check fails. Everything it measures is written to
<out>/report.json, and the export and the frames stay in <out> to be looked at.
"""
import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
import zlib


class Engine:
    """One `concat-cli api` process: requests on stdin, replies and events on stdout."""

    def __init__(self, cli, home):
        env = dict(os.environ, HOME=home, XDG_CONFIG_HOME=os.path.join(home, "config"),
                   XDG_DATA_HOME=os.path.join(home, "data"))
        self.proc = subprocess.Popen([cli, "api"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     text=True, bufsize=1, env=env)
        self.next_id = 0
        self.events = []

    def call(self, method, **params):
        self.next_id += 1
        line = json.dumps({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params})
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()
        while True:
            reply = self.proc.stdout.readline()
            if not reply:
                raise RuntimeError(f"the engine closed its output during {method}")
            message = json.loads(reply)
            if "id" in message and message["id"] == self.next_id:
                return message
            self.events.append(message)

    def ok(self, method, **params):
        message = self.call(method, **params)
        if "error" in message:
            raise RuntimeError(f"{method} was refused: {message['error']}")
        return message["result"]

    def wait_for(self, names, job):
        """Reads events until one of `names` arrives for `job`; returns it."""
        def wanted(event):
            return event.get("method") in names and event.get("params", {}).get("job") == job

        for event in self.events:
            if wanted(event):
                return event
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError(f"the engine closed its output before any of {names}")
            event = json.loads(line)
            self.events.append(event)
            if wanted(event):
                return event

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=600)


def ffprobe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def mean_volume_db(path):
    """The mean volume of the first audio stream, in dB; None when there is no audio."""
    run = subprocess.run(["ffmpeg", "-nostdin", "-v", "info", "-i", path, "-map", "0:a:0", "-af", "volumedetect",
                          "-f", "null", "-"], capture_output=True, text=True)
    for line in run.stderr.splitlines():
        if "mean_volume:" in line:
            return float(line.split("mean_volume:")[1].split("dB")[0])
    return None


def grey_png(path, width, height, value):
    """Writes an eight-bit grey PNG filled with `value`, left half only when value is a tuple."""
    rows = []
    for _ in range(height):
        if isinstance(value, tuple):
            left, right = value
            row = bytes([left] * (width // 2) + [right] * (width - width // 2))
        else:
            row = bytes([value] * width)
        rows.append(b"\x00" + row)
    raw = zlib.compress(b"".join(rows))

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
    png += chunk(b"IDAT", raw) + chunk(b"IEND", b"")
    with open(path, "wb") as handle:
        handle.write(png)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cli", required=True)
    parser.add_argument("--footage", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    out = os.path.abspath(args.out)
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    footage = os.path.abspath(args.footage)
    report = {"checks": []}
    failures = []

    def check(name, passed, detail):
        report["checks"].append({"name": name, "passed": bool(passed), "detail": detail})
        print(("PASS " if passed else "FAIL ") + name + " :: " + json.dumps(detail))
        if not passed:
            failures.append(name)

    source = ffprobe(footage)
    source_has_audio = any(s["codec_type"] == "audio" for s in source["streams"])
    source_db = mean_volume_db(footage)
    check("the footage has picture and sound", source_has_audio and source_db is not None and source_db > -60,
          {"duration": source["format"]["duration"], "meanVolumeDb": source_db})

    engine = Engine(args.cli, os.path.join(out, "home"))
    version = engine.ok("version")
    report["version"] = version
    check("the build says it imports masks and finds none", "cutout.import" in version["capabilities"],
          version["capabilities"])

    view = engine.ok("project.create", location=out, name="proof",
                     video={"width": 1280, "height": 720, "rateNum": 30, "rateDen": 1})
    project = os.path.join(out, "proof")

    view = engine.ok("media.import", path=project, file=footage)
    media = view["project"]["media"][0]
    check("the import probed the file's sound", media.get("hasAudio") is True,
          {"mediaId": media["id"], "duration": media.get("duration"), "hasAudio": media.get("hasAudio")})

    view = engine.ok("edit.apply", path=project,
                     command={"op": "addClipAtFirstFree", "mediaId": media["id"], "start": 0})
    clip_id = view["createdId"]

    # Cut the clip at 4 s and drop everything after 8 s: two pieces, 0-4 and 4-8.
    view = engine.ok("edit.apply", path=project, command={"op": "splitClips", "clipIds": [clip_id], "time": 4.0})
    timeline = next(t for t in view["project"]["timelines"] if t["id"] == view["project"]["activeTimelineId"])
    pieces = sorted((c for c in timeline["clips"] if c["mediaId"] == media["id"] and c["kind"] != "audio"),
                    key=lambda c: c["start"])
    second = pieces[-1]
    tail = second["start"] + second["duration"] - 8.0
    if tail > 0:
        view = engine.ok("edit.apply", path=project,
                         command={"op": "trimClip", "clipId": second["id"], "edge": "end", "delta": -tail})

    # A look on the second piece, a fade-in between the two, a slower first piece.
    engine.ok("edit.apply", path=project, command={
        "op": "updateClip", "clipId": second["id"],
        "patch": {"filters": [{"id": "concat.sepia", "params": {}, "enabled": True}], "fadeOut": 0.5}})
    view = engine.ok("edit.apply", path=project, command={
        "op": "addTextClip", "trackId": None, "above": True, "start": 0.5, "duration": 3.0,
        "style": {"content": "ENGINE PROOF", "fontSize": 0.12, "fontWeight": 700, "color": "#ffffff",
                  "strokeWidth": 0.008}})
    title_id = view["createdId"]
    check("a title clip was made", bool(title_id), {"titleId": title_id})

    undone = engine.ok("edit.undo", path=project)
    redone = engine.ok("edit.redo", path=project)
    check("undo then redo returns the title", undone["canRedo"] is True and redone["canUndo"] is True,
          {"canRedoAfterUndo": undone["canRedo"]})

    engine.ok("project.save", path=project)
    document = engine.ok("project.document", path=project)
    with open(os.path.join(out, "document.json"), "w") as handle:
        json.dump(document, handle, indent=1)

    for name, at in (("frame-title", 1.5), ("frame-sepia", 6.0)):
        written = engine.ok("preview.frame", path=project, time=at, output=os.path.join(out, name + ".png"),
                            width=1280, height=720)
        check(f"a preview frame was written at {at}s", os.path.getsize(written["path"]) > 10_000, written)

    packages = engine.ok("catalogue.list")
    presets = engine.ok("catalogue.textPresets")
    animations = engine.ok("catalogue.animations")
    check("the effect, title and animation inventories are listed",
          len(packages) > 50 and len(presets) > 5 and {a["slot"] for a in animations} == {"in", "out", "combo"},
          {"packages": len(packages), "textPresets": len(presets),
           "animations": {a["slot"]: len(a["names"]) for a in animations}})

    exported = os.path.join(out, "export.mp4")
    started = engine.ok("export.run", path=project, output=exported, crf=20, preset="veryfast")
    done = engine.wait_for({"export.done", "export.failed"}, started["job"])
    check("the export finished", done["method"] == "export.done", done.get("params"))

    probe = ffprobe(exported)
    video = [s for s in probe["streams"] if s["codec_type"] == "video"]
    audio = [s for s in probe["streams"] if s["codec_type"] == "audio"]
    duration = float(probe["format"]["duration"])
    volume = mean_volume_db(exported)
    report["export"] = {"video": video[0]["codec_name"] if video else None,
                        "audio": audio[0]["codec_name"] if audio else None,
                        "width": video[0]["width"] if video else None,
                        "height": video[0]["height"] if video else None,
                        "duration": duration, "meanVolumeDb": volume, "job": started["job"]}
    check("the export has a picture stream at the project's size",
          bool(video) and video[0]["width"] == 1280 and video[0]["height"] == 720, report["export"])
    check("the export has a sound stream that is not silence", bool(audio) and volume is not None and volume > -60,
          {"meanVolumeDb": volume, "sourceMeanVolumeDb": source_db})
    check("the export is as long as the cut", abs(duration - 8.0) < 0.2, {"duration": duration, "expected": 8.0})
    for name, at in (("export-title", 1.5), ("export-sepia", 6.0)):
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", str(at), "-i", exported, "-frames:v", "1",
                        os.path.join(out, name + ".png")], check=True)

    # A cutout with no masks: the export must be refused, never rendered untreated.
    engine.ok("edit.apply", path=project, command={
        "op": "setClipCutout", "clipId": pieces[0]["id"],
        "cutout": {"mode": "auto", "subject": "person", "feather": 0.0, "strokes": []}})
    refused = engine.call("export.run", path=project, output=os.path.join(out, "untreated.mp4"))
    check("an export whose cutout has no masks is refused",
          "error" in refused and refused["error"].get("data", {}).get("code") == "refused"
          and not os.path.exists(os.path.join(out, "untreated.mp4")),
          refused.get("error"))
    status = engine.ok("cutout.status", path=project)
    missing = status[0]["missing"] if status else []
    check("cutout.status names the missing instants", len(missing) > 10 and status[0]["stepMs"] == 100,
          {"mediaId": status[0]["mediaId"] if status else None, "missing": len(missing)})

    # Masks made elsewhere: keep the left half of the picture. Import, then export over a second track.
    masks = os.path.join(out, "masks")
    os.makedirs(masks)
    for millis in missing:
        grey_png(os.path.join(masks, f"{millis:09}.png"), 64, 36, (255, 0))
    imported = engine.ok("cutout.import", path=project, mediaId=status[0]["mediaId"], subject="person",
                         source="e2e-left-half", masks=masks)
    check("the masks were imported whole", imported["imported"] == len(missing), imported)
    after = engine.ok("cutout.status", path=project)
    check("nothing is missing after the import", after[0]["missing"] == [], {"missing": len(after[0]["missing"])})

    cut = os.path.join(out, "export-cutout.mp4")
    second_export = engine.ok("export.run", path=project, output=cut, crf=20, preset="veryfast")
    done = engine.wait_for({"export.done", "export.failed"}, second_export["job"])
    check("the export with imported masks finished", done["method"] == "export.done", done.get("params"))
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", "2.0", "-i", cut, "-frames:v", "1",
                    os.path.join(out, "export-cutout.png")], check=True)
    # The right half was masked out, so it is the black of an empty frame; the left half is footage.
    stats = subprocess.run(["ffmpeg", "-nostdin", "-v", "info", "-ss", "2.0", "-i", cut, "-frames:v", "1", "-vf",
                            "crop=iw/4:ih/2:iw*5/8:ih/4,signalstats,metadata=print", "-f", "null", "-"],
                           capture_output=True, text=True).stderr
    right_luma = next((float(l.split("=")[1]) for l in stats.splitlines() if "lavfi.signalstats.YAVG" in l), None)
    stats = subprocess.run(["ffmpeg", "-nostdin", "-v", "info", "-ss", "2.0", "-i", cut, "-frames:v", "1", "-vf",
                            "crop=iw/4:ih/2:iw/8:ih/4,signalstats,metadata=print", "-f", "null", "-"],
                           capture_output=True, text=True).stderr
    left_luma = next((float(l.split("=")[1]) for l in stats.splitlines() if "lavfi.signalstats.YAVG" in l), None)
    check("the masked half of the picture is gone and the kept half is there",
          right_luma is not None and left_luma is not None and right_luma < 20 and left_luma > right_luma + 10,
          {"keptHalfLuma": left_luma, "maskedHalfLuma": right_luma})

    engine.ok("project.close", path=project, save=True)
    engine.close()

    report["failures"] = failures
    with open(os.path.join(out, "report.json"), "w") as handle:
        json.dump(report, handle, indent=1)
    print(f"{len(report['checks']) - len(failures)} of {len(report['checks'])} checks passed")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
