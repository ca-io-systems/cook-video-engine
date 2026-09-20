#!/usr/bin/env python3
# SPDX-License-Identifier: AGPL-3.0-or-later
"""One measured check per family of edits the engine offers, through the JSON-RPC API, on real footage.

Usage:
    capability_families.py --cli <path to concat-cli> --footage <video with audio> --out <dir>

Each family makes its own small project, exports it, and measures the exported file: a duration, a frame's
luma in one region, how alike two frames are, a stream's codec. A family the engine cannot do shows up as a
failed check with the engine's own refusal in it, never as a skipped one. Results go to <out>/families.json.
"""
import argparse
import json
import os
import shutil
import struct
import subprocess
import sys
import zlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from engine_e2e import Engine, ffprobe, luma  # noqa: E402

WHITE, BLACK = 235, 16  # limited-range luma of white and of black


def rgb_png(path, width, height, rgb):
    """Writes a solid RGB PNG."""
    row = b"\x00" + bytes(rgb) * width
    raw = zlib.compress(row * height)

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    with open(path, "wb") as handle:
        handle.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", raw) + chunk(b"IEND", b""))


def frame(video, at, out):
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", str(at), "-i", video, "-frames:v", "1", out],
                   check=True)
    return out


def ssim(a, b):
    """How alike two pictures are, 0..1."""
    run = subprocess.run(["ffmpeg", "-nostdin", "-v", "info", "-i", a, "-i", b, "-lavfi",
                          "[0:v]scale=640:360[x];[1:v]scale=640:360[y];[x][y]ssim", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    for line in run.splitlines():
        if "SSIM" in line and "All:" in line:
            return float(line.split("All:")[1].split()[0])
    return None


def volume_db(path, start, end):
    run = subprocess.run(["ffmpeg", "-nostdin", "-v", "info", "-i", path, "-map", "0:a:0", "-af",
                          f"atrim={start}:{end},volumedetect", "-f", "null", "-"], capture_output=True, text=True)
    for line in run.stderr.splitlines():
        if "mean_volume:" in line:
            return float(line.split("mean_volume:")[1].split("dB")[0])
    return None


class Families:
    def __init__(self, cli, footage, out):
        self.footage = footage
        self.out = out
        self.engine = Engine(cli, os.path.join(out, "home"))
        self.checks = []
        self.failures = []
        self.white = os.path.join(out, "white.png")
        self.black = os.path.join(out, "black.png")
        rgb_png(self.white, 320, 180, (255, 255, 255))
        rgb_png(self.black, 320, 180, (0, 0, 0))

    def check(self, family, name, passed, detail):
        self.checks.append({"family": family, "name": name, "passed": bool(passed), "detail": detail})
        print(("PASS " if passed else "FAIL ") + f"[{family}] {name} :: " + json.dumps(detail))
        if not passed:
            self.failures.append(f"{family}: {name}")

    # -- helpers -------------------------------------------------------------------------------------------
    def project(self, name, width=1280, height=720):
        self.engine.ok("project.create", location=self.out, name=name,
                       video={"width": width, "height": height, "rateNum": 30, "rateDen": 1})
        return os.path.join(self.out, name)

    def media(self, project, file):
        view = self.engine.ok("media.import", path=project, file=file)
        return next(m for m in view["project"]["media"] if m["path"] == file)

    def apply(self, project, **command):
        return self.engine.ok("edit.apply", path=project, command=command)

    def place(self, project, media_id, start):
        return self.apply(project, op="addClipAtFirstFree", mediaId=media_id, start=start)["createdId"]

    def clips(self, project):
        view = self.engine.ok("project.get", path=project)
        doc = view["project"]
        timeline = next(t for t in doc["timelines"] if t["id"] == doc["activeTimelineId"])
        return timeline["clips"], timeline

    def clip(self, project, clip_id):
        return next(c for c in self.clips(project)[0] if c["id"] == clip_id)

    def piece(self, project, media_id, source_from, source_to):
        """One clip at 0 showing source_from..source_to of the footage."""
        clip_id = self.place(project, media_id, 0)
        if source_from > 0:
            self.apply(project, op="trimClip", clipId=clip_id, edge="start", delta=source_from, ripple=False)
            self.apply(project, op="moveClips", moves=[{"clipId": clip_id, "start": 0,
                                                        "trackId": self.clip(project, clip_id)["trackId"]}])
        clip = self.clip(project, clip_id)
        extra = clip["duration"] - (source_to - source_from)
        if extra > 0:
            self.apply(project, op="trimClip", clipId=clip_id, edge="end", delta=-extra, ripple=False)
        return clip_id

    def export(self, project, name, **spec):
        output = os.path.join(self.out, name)
        reply = self.engine.call("export.run", path=project, output=output, crf=23, preset="veryfast", **spec)
        if "error" in reply:
            return None, reply["error"]
        done = self.engine.wait_for({"export.done", "export.failed"}, reply["result"]["job"])
        if done["method"] != "export.done":
            return None, done.get("params")
        return output, None

    def family(self, name, run):
        try:
            run()
        except Exception as error:  # a refusal or a crash is a failed check, not a lost run
            self.check(name, "the family ran to its end", False, {"error": str(error)})

    # -- the families --------------------------------------------------------------------------------------
    def speed(self):
        p = self.project("speed")
        m = self.media(p, self.footage)
        clip_id = self.piece(p, m["id"], 10, 14)
        self.apply(p, op="setClipSpeed", clipId=clip_id, speed=2.0)
        doubled = self.clip(p, clip_id)["duration"]
        out, why = self.export(p, "speed.mp4")
        length = float(ffprobe(out)["format"]["duration"]) if out else None
        self.check("speed", "4 s of source at double speed exports as 2 s",
                   out and abs(doubled - 2.0) < 0.05 and abs(length - 2.0) < 0.15,
                   {"clipDuration": doubled, "exportDuration": length, "refusal": why})
        self.apply(p, op="setClipSpeedCurve", clipId=clip_id, curve=[{"at": 0.0, "speed": 1.0}, {"at": 1.0, "speed": 3.0}])
        curved = self.clip(p, clip_id)
        out, why = self.export(p, "speed-curve.mp4")
        length = float(ffprobe(out)["format"]["duration"]) if out else None
        self.check("speed", "a speed curve is kept in the document and the export is as long as the clip says",
                   out and curved.get("speedCurve") and abs(length - curved["duration"]) < 0.15,
                   {"clipDuration": curved["duration"], "exportDuration": length, "refusal": why})

    def reverse_and_freeze(self):
        p = self.project("reverse")
        m = self.media(p, self.footage)
        clip_id = self.piece(p, m["id"], 20, 24)
        self.apply(p, op="updateClip", clipId=clip_id, patch={"reverse": True})
        out, why = self.export(p, "reverse.mp4")
        if out:
            first = frame(out, 0.2, os.path.join(self.out, "reverse-first.png"))
            near_end = frame(self.footage, 23.8, os.path.join(self.out, "source-23_8.png"))
            near_start = frame(self.footage, 20.2, os.path.join(self.out, "source-20_2.png"))
            to_end, to_start = ssim(first, near_end), ssim(first, near_start)
        else:
            to_end = to_start = None
        self.check("reverse", "a reversed clip opens on the end of its source",
                   out and to_end is not None and to_end > to_start,
                   {"likeSourceEnd": to_end, "likeSourceStart": to_start, "refusal": why})

        p = self.project("freeze")
        m = self.media(p, self.footage)
        clip_id = self.piece(p, m["id"], 20, 24)
        still = os.path.join(self.out, "freeze-still.png")
        self.engine.ok("preview.frame", path=p, time=2.0, output=still, width=1280, height=720)
        freeze = self.apply(p, op="freezeFrame", clipId=clip_id, time=2.0, duration=2.0, still={
            "path": still, "name": "freeze-still.png", "duration": None, "kind": "image", "width": 1280,
            "height": 720, "frameRate": None, "frameRateFraction": None, "videoCodec": None,
            "audioCodec": None, "hasAudio": False})
        out, why = self.export(p, "freeze.mp4")
        if out:
            held = ssim(frame(out, 2.4, os.path.join(self.out, "freeze-a.png")),
                        frame(out, 3.6, os.path.join(self.out, "freeze-b.png")))
            moving = ssim(frame(out, 0.4, os.path.join(self.out, "freeze-c.png")),
                          frame(out, 1.6, os.path.join(self.out, "freeze-d.png")))
            length = float(ffprobe(out)["format"]["duration"])
        else:
            held = moving = length = None
        self.check("freeze", "a 2 s freeze holds one picture and makes the cut 2 s longer",
                   out and freeze.get("createdId") and held > 0.98 and held > moving and abs(length - 6.0) < 0.2,
                   {"heldFramesAlike": held, "movingFramesAlike": moving, "exportDuration": length, "refusal": why})

    def transform_and_tracks(self):
        p = self.project("transform")
        white = self.media(p, self.white)
        m = self.media(p, self.footage)
        ground = self.place(p, white["id"], 0)
        self.apply(p, op="trimClip", clipId=ground, edge="end", delta=-(self.clip(p, ground)["duration"] - 3.0))
        tracks = self.clips(p)[1]["tracks"]
        ground_track = self.clip(p, ground)["trackId"]
        above = next(t["id"] for t in tracks if t["id"] != ground_track)
        top = self.apply(p, op="addClip", mediaId=m["id"], trackId=above, start=0)["createdId"]
        self.apply(p, op="trimClip", clipId=top, edge="start", delta=20.0)
        self.apply(p, op="moveClips", moves=[{"clipId": top, "start": 0, "trackId": above}])
        self.apply(p, op="trimClip", clipId=top, edge="end", delta=-(self.clip(p, top)["duration"] - 3.0))
        self.apply(p, op="setClipTransform", clipId=top, scale=0.4, offsetX=0.25, offsetY=-0.25, rotation=None)
        out, why = self.export(p, "transform.mp4")
        corner = luma(out, 1.0, "iw/5:ih/5:iw/20:ih*3/4") if out else None     # bottom left: the white ground
        inset = luma(out, 1.0, "iw/6:ih/8:iw*2/3:ih/5") if out else None      # top right: the scaled footage
        order = [self.clip(p, ground)["trackId"], self.clip(p, top)["trackId"]]
        self.check("transform", "a clip scaled to 0.4 and moved up and right sits over a white clip on another track",
                   out and corner > 200 and inset < 190, {"groundLuma": corner, "insetLuma": inset,
                                                           "tracks": order, "refusal": why})

    def keys_animation_layer(self):
        p = self.project("keys")
        white = self.media(p, self.white)
        clip_id = self.place(p, white["id"], 0)
        self.apply(p, op="trimClip", clipId=clip_id, edge="end", delta=-(self.clip(p, clip_id)["duration"] - 3.0))
        self.apply(p, op="setClipKey", clipId=clip_id, property="opacity", at=0.0, value=0.0)
        self.apply(p, op="setClipKey", clipId=clip_id, property="opacity", at=1.0, value=1.0)
        out, why = self.export(p, "keys.mp4")
        early = luma(out, 0.3, "iw/2:ih/2:iw/4:ih/4") if out else None
        late = luma(out, 2.7, "iw/2:ih/2:iw/4:ih/4") if out else None
        self.check("keyframes", "opacity keyed from 0 to 1 makes a white clip brighten over its length",
                   out and early < 80 and late > 190, {"lumaAt0_3s": early, "lumaAt2_7s": late, "refusal": why})

        p = self.project("animation")
        white = self.media(p, self.white)
        clip_id = self.place(p, white["id"], 0)
        self.apply(p, op="trimClip", clipId=clip_id, edge="end", delta=-(self.clip(p, clip_id)["duration"] - 3.0))
        names = {a["slot"]: a["names"] for a in self.engine.ok("catalogue.animations")}
        preset = names["in"][0]
        self.apply(p, op="setClipAnimation", clipId=clip_id, slot="in", animation={"preset": preset, "duration": 1.0})
        stored = self.clip(p, clip_id).get("animationIn")
        out, why = self.export(p, "animation.mp4")
        alike = ssim(frame(out, 0.1, os.path.join(self.out, "anim-a.png")),
                     frame(out, 2.5, os.path.join(self.out, "anim-b.png"))) if out else None
        self.check("animation", f"the in animation {preset!r} changes the clip's first frames",
                   out and stored and stored["preset"] == preset and alike is not None and alike < 0.99,
                   {"preset": preset, "firstAndSettledFramesAlike": alike, "refusal": why})

        p = self.project("layer")
        white = self.media(p, self.white)
        clip_id = self.place(p, white["id"], 0)
        self.apply(p, op="trimClip", clipId=clip_id, edge="end", delta=-(self.clip(p, clip_id)["duration"] - 4.0))
        layer = self.apply(p, op="addLayerClip", trackId=None, start=2.0, duration=2.0, effectId="concat.invert",
                           name="Invert")
        out, why = self.export(p, "layer.mp4")
        outside = luma(out, 1.0, "iw/2:ih/2:iw/4:ih/4") if out else None
        under = luma(out, 3.0, "iw/2:ih/2:iw/4:ih/4") if out else None
        self.check("effect layer", "an invert layer over the second half turns the white clip under it black",
                   out and layer.get("createdId") and outside > 200 and under < 60,
                   {"lumaOutsideLayer": outside, "lumaUnderLayer": under, "refusal": why})

    def transition(self):
        p = self.project("transition")
        white, black = self.media(p, self.white), self.media(p, self.black)
        first = self.place(p, white["id"], 0)
        self.apply(p, op="trimClip", clipId=first, edge="end", delta=-(self.clip(p, first)["duration"] - 3.0))
        track = self.clip(p, first)["trackId"]
        second = self.apply(p, op="addClip", mediaId=black["id"], trackId=track, start=3.0)["createdId"]
        self.apply(p, op="trimClip", clipId=second, edge="end", delta=-(self.clip(p, second)["duration"] - 3.0))
        self.apply(p, op="updateClip", clipId=second, patch={"transitionIn": {"id": "cross-fade", "duration": 1.0}})
        out, why = self.export(p, "transition.mp4")
        before = luma(out, 1.0, "iw/2:ih/2:iw/4:ih/4") if out else None
        middle = luma(out, 3.0, "iw/2:ih/2:iw/4:ih/4") if out else None
        after = luma(out, 5.0, "iw/2:ih/2:iw/4:ih/4") if out else None
        self.check("transition", "a 1 s cross-fade from white to black passes through grey",
                   out and before > 200 and after < 40 and 40 < middle < 200,
                   {"lumaBefore": before, "lumaMidTransition": middle, "lumaAfter": after, "refusal": why})

    def audio(self):
        p = self.project("audio")
        m = self.media(p, self.footage)
        clip_id = self.piece(p, m["id"], 30, 36)
        self.apply(p, op="splitClips", clipIds=[clip_id], time=3.0)
        pieces = sorted((c for c in self.clips(p)[0] if c["mediaId"] == m["id"] and c["kind"] != "audio"),
                        key=lambda c: c["start"])
        self.apply(p, op="updateClip", clipId=pieces[1]["id"], patch={"volume": 0.0})
        self.apply(p, op="updateClip", clipId=pieces[0]["id"],
                   patch={"filters": [{"id": "concat.echo", "params": {}, "enabled": True}]})
        out, why = self.export(p, "audio.mp4")
        loud = volume_db(out, 0.5, 2.5) if out else None
        quiet = volume_db(out, 3.5, 5.5) if out else None
        self.check("audio", "a piece at volume 0 is silent while the piece before it, with an echo filter, is not",
                   out and loud is not None and loud > -50 and (quiet is None or quiet < -70),
                   {"firstPieceDb": loud, "secondPieceDb": quiet, "refusal": why})

        detached = self.apply(p, op="detachAudio", clipId=pieces[0]["id"])
        sound = [c for c in self.clips(p)[0] if c["kind"] == "audio"]
        self.check("audio", "detaching a clip's sound makes an audio clip of its own",
                   len(sound) == 1 and bool(detached.get("createdId")), {"audioClips": len(sound)})
        track = sound[0]["trackId"] if sound else None
        if track:
            self.apply(p, op="setTrackFlag", trackId=track, flag="muted", value=True)
            out, why = self.export(p, "audio-muted-track.mp4")
            muted = volume_db(out, 0.5, 2.5) if out else None
            self.check("audio", "muting the track the detached sound is on silences it",
                       out and (muted is None or muted < -70), {"firstPieceDb": muted, "refusal": why})

    def arrange(self):
        p = self.project("arrange")
        m = self.media(p, self.footage)
        clip_id = self.piece(p, m["id"], 10, 19)
        self.apply(p, op="splitClips", clipIds=[clip_id], time=3.0)
        self.apply(p, op="splitClips", clipIds=[c["id"] for c in self.clips(p)[0] if c["kind"] != "audio"], time=6.0)
        a, b, c = sorted((x for x in self.clips(p)[0] if x["kind"] != "audio"), key=lambda x: x["start"])
        self.apply(p, op="removeClips", clipIds=[b["id"]], ripple=True)
        moved = self.clip(p, c["id"])["start"]
        self.check("arrange", "a ripple delete of the middle piece pulls the last piece back to 3 s",
                   abs(moved - 3.0) < 0.01, {"lastPieceStart": moved})
        self.apply(p, op="moveClips", moves=[{"clipId": c["id"], "start": 5.0, "trackId": c["trackId"]}])
        self.check("arrange", "a moved clip starts where it was put", abs(self.clip(p, c["id"])["start"] - 5.0) < 0.01,
                   {"start": self.clip(p, c["id"])["start"]})

        p = self.project("merge")
        m = self.media(p, self.footage)
        clip_id = self.piece(p, m["id"], 10, 16)
        self.apply(p, op="splitClips", clipIds=[clip_id], time=3.0)
        halves = [x["id"] for x in self.clips(p)[0] if x["kind"] != "audio"]
        self.apply(p, op="mergeClips", clipIds=halves)
        whole = [x for x in self.clips(p)[0] if x["kind"] != "audio"]
        self.check("arrange", "two pieces of a split merge back into one 6 s clip",
                   len(whole) == 1 and abs(whole[0]["duration"] - 6.0) < 0.01,
                   {"clips": len(whole), "duration": whole[0]["duration"] if whole else None})

    def timelines_and_frame(self):
        p = self.project("timelines")
        m = self.media(p, self.footage)
        self.piece(p, m["id"], 10, 15)
        first = self.clips(p)[1]["id"]
        self.apply(p, op="addTimeline")
        second = self.clips(p)[1]["id"]
        white = self.media(p, self.white)
        short = self.place(p, white["id"], 0)
        self.apply(p, op="trimClip", clipId=short, edge="end", delta=-(self.clip(p, short)["duration"] - 2.0))
        out, why = self.export(p, "timeline-two.mp4")
        two = float(ffprobe(out)["format"]["duration"]) if out else None
        self.apply(p, op="selectTimeline", timelineId=first)
        out1, why1 = self.export(p, "timeline-one.mp4")
        one = float(ffprobe(out1)["format"]["duration"]) if out1 else None
        self.check("timelines", "each of two timelines exports its own length", out and out1 and first != second
                   and abs(two - 2.0) < 0.15 and abs(one - 5.0) < 0.15,
                   {"secondTimeline": two, "firstTimeline": one, "refusal": why or why1})

        p = self.project("vertical", 1080, 1920)
        m = self.media(p, self.footage)
        self.piece(p, m["id"], 10, 12)
        out, why = self.export(p, "vertical.mp4")
        video = next(s for s in ffprobe(out)["streams"] if s["codec_type"] == "video") if out else {}
        self.check("frame", "a 1080x1920 project exports a 1080x1920 file",
                   out and video.get("width") == 1080 and video.get("height") == 1920,
                   {"width": video.get("width"), "height": video.get("height"), "refusal": why})

    def export_settings(self):
        p = self.project("codecs")
        m = self.media(p, self.footage)
        self.piece(p, m["id"], 10, 12)
        for name, spec, want in (
            ("h264 at 640x360 and 24 fps", {"codec": "h264", "width": 640, "height": 360, "rateNum": 24, "rateDen": 1},
             {"codec_name": "h264", "width": 640, "height": 360, "r_frame_rate": "24/1"}),
            ("hevc", {"codec": "hevc"}, {"codec_name": "hevc"}),
            ("av1", {"codec": "av1"}, {"codec_name": "av1"}),
            ("h264 ten bit", {"codec": "h264", "tenBit": True}, {"codec_name": "h264", "pix_fmt": "yuv420p10le"}),
        ):
            out, why = self.export(p, "codec-" + name.replace(" ", "-") + ".mp4", **spec)
            video = next(s for s in ffprobe(out)["streams"] if s["codec_type"] == "video") if out else {}
            got = {key: video.get(key) for key in want}
            self.check("export settings", f"an export asked for {name} is {name}", out and got == want,
                       {"asked": spec, "got": got, "refusal": why})
        started = self.engine.ok("export.run", path=p, output=os.path.join(self.out, "cancelled.mp4"), preset="veryslow")
        self.engine.ok("export.cancel", job=started["job"])
        ended = self.engine.wait_for({"export.done", "export.failed"}, started["job"])
        self.check("export settings", "a cancelled export ends as cancelled",
                   ended["method"] == "export.failed" and ended["params"]["error"]["code"] == "cancelled",
                   ended.get("params"))

    def fonts_and_templates(self):
        p = self.project("fonts")
        serif = "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"
        if os.path.exists(serif):
            self.apply(p, op="addFont", family="DejaVu Serif", path=serif)
            self.apply(p, op="addTextClip", trackId=None, above=False, start=0.0, duration=2.0,
                       style={"content": "Serif title", "fontFamily": "DejaVu Serif", "fontSize": 0.12})
            fonts = self.engine.ok("project.get", path=p)["project"]["fonts"]
            out, why = self.export(p, "fonts.mp4")
            lit = luma(out, 1.0, "iw*3/5:ih/5:iw/5:ih*2/5") if out else None
            self.check("fonts", "a title in a font added to the project exports with the words painted",
                       out and len(fonts) == 1 and lit is not None and lit > 25,
                       {"projectFonts": len(fonts), "titleBandLuma": lit, "refusal": why})
        else:
            self.check("fonts", "a font file to add is on this machine", False, {"looked": serif})

        p = self.project("template-source")
        m = self.media(p, self.footage)
        self.piece(p, m["id"], 10, 13)
        self.apply(p, op="setMediaPlaceholder", mediaId=m["id"], placeholder=True)
        saved = self.engine.ok("template.save", path=p, name="three second cut")
        listed = [t["path"] for t in self.engine.ok("template.list")]
        slots = saved.get("slots", [])
        made = self.engine.call("template.instantiate", template=saved["path"], location=self.out,
                                name="from-template",
                                fills=[{"mediaId": slot["mediaId"], "file": self.footage} for slot in slots])
        clips = None
        if "result" in made:
            doc = made["result"]["project"]
            clips = len(next(t for t in doc["timelines"] if t["id"] == doc["activeTimelineId"])["clips"])
        self.check("templates", "a project saved as a template is listed and makes a new project when its slot is filled",
                   saved["path"] in listed and len(slots) == 1 and clips and clips >= 1,
                   {"listed": saved["path"] in listed, "slots": len(slots), "clipsInNewProject": clips,
                    "refusal": made.get("error")})

    def run(self):
        for name, run in (("speed", self.speed), ("reverse and freeze", self.reverse_and_freeze),
                          ("transform and tracks", self.transform_and_tracks),
                          ("keys, animation, layer", self.keys_animation_layer), ("transition", self.transition),
                          ("audio", self.audio), ("arrange", self.arrange),
                          ("timelines and frame", self.timelines_and_frame), ("export settings", self.export_settings),
                          ("fonts and templates", self.fonts_and_templates)):
            self.family(name, run)
        self.engine.close()
        with open(os.path.join(self.out, "families.json"), "w") as handle:
            json.dump({"checks": self.checks, "failures": self.failures}, handle, indent=1)
        print(f"{len(self.checks) - len(self.failures)} of {len(self.checks)} checks passed")
        return 1 if self.failures else 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cli", required=True)
    parser.add_argument("--footage", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()
    out = os.path.abspath(args.out)
    shutil.rmtree(out, ignore_errors=True)
    os.makedirs(out)
    sys.exit(Families(args.cli, os.path.abspath(args.footage), out).run())


if __name__ == "__main__":
    main()
