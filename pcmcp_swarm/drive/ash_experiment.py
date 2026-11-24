"""Evaluate Instinct-authored language cases using an isolated Ash database."""
import json
import math
from pathlib import Path
import re
import signal
import sqlite3
import sys
from tempfile import TemporaryDirectory


def validate(raw):
    if not isinstance(raw, str) or len(raw) > 4500:
        raise ValueError("Send a JSON array of up to 5 cases, at most 4500 characters.")
    try:
        cases = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        raise ValueError("Malformed experiment JSON.") from exc
    if not isinstance(cases, list) or not 1 <= len(cases) <= 5:
        raise ValueError("Send 1 to 5 cases.")
    for case in cases:
        if not isinstance(case, dict) or not {"question", "expected"} <= case.keys() <= {"context", "question", "expected", "scene"}:
            raise ValueError("Each case needs question and expected, with optional context and scene.")
        if any(not isinstance(value, str) or len(value) > 900 for key, value in case.items() if key != "scene"):
            raise ValueError("Case fields must be text of at most 900 characters.")
        if not case["question"].strip() or not case["expected"].strip():
            raise ValueError("Question and expected cannot be empty.")
        if "scene" in case:
            validate_scene(case["scene"])
    return cases


def validate_scene(scene):
    ranges = {"x": (-8, 8), "speed": (-12, 12), "seconds": (0.1, 3)}
    if not isinstance(scene, dict) or not {"x", "speed", "seconds", "screen"} <= scene.keys() <= {"shape", *ranges, "screen"}:
        raise ValueError("Scene needs x, speed, seconds, screen, and optional shape.")
    if type(scene["screen"]) is not bool or scene.get("shape", "sphere") not in ("sphere", "box"):
        raise ValueError("Use true or false for screen, and sphere or box for shape.")
    for key, (low, high) in ranges.items():
        v = scene[key]
        if type(v) not in (int, float) or not low <= v <= high:
            raise ValueError(f"Scene {key} must be between {low} and {high}.")


def observe_scene(scene):
    from dataclasses import replace
    from ash.world import PerceptionBridge, World, situation_from_percepts

    world = World()
    shape = scene.get("shape", "sphere")
    subject = world.create("subject", shape, (scene["x"], .25, 0), half=(.25,) * 3)
    if scene["screen"]:
        world.create("screen", "screen", (0, 2, 5), half=(2.5, 2.5, .1), mass=None, occluder=True)
    sensor = PerceptionBridge(viewpoint=(0, 2, 14))
    if scene["speed"]:
        world.push(subject.id, (scene["speed"], 0, 0))
    # Sample throughout the run so a temporary occlusion remains observable.
    frames = []
    observations = []
    steps = round(scene["seconds"] * 100)
    sample_steps = sorted({0, steps // 3, 2 * steps // 3, steps})
    previous = 0
    for step in sample_steps:
        if step > previous:
            world.step(.01, step - previous)
        previous = step
        percept = sensor.observe(world)
        observations.append(replace(percept, items=tuple(i for i in percept.items if i.visible)))
        item = next((item for item in percept.items if item.visible and item.handle.split(":", 1)[1] == str(subject.id)), None)
        entry = {"time": percept.time, "visible": item is not None}
        if item:
            entry.update(x=item.position[0], speed=round(math.hypot(*item.velocity), 3))
        frames.append(entry)
    names = {str(subject.id): shape}
    situation = situation_from_percepts(observations[-2], observations[-1], [], names)
    return situation, frames


def extract(tail):
    try:
        _, end = json.JSONDecoder().raw_decode(tail)
    except (ValueError, RecursionError):
        return ""
    return tail[:end]


def normalize(text):
    return re.sub(r"[^\w]+", " ", text.casefold()).strip()


def main(cases):
    root = Path.home() / "Documents" / "SYNA_5"
    sys.path.insert(0, str(root / "ASH-AI"))
    from ash.memory.mind import AshMind

    source = root / "ASH-AI-state" / "mind.db"
    with TemporaryDirectory(prefix="pcmcp-ash-") as temp:
        snapshot = Path(temp) / "snapshot.db"
        with sqlite3.connect(source.as_uri() + "?mode=ro", uri=True) as live:
            with sqlite3.connect(snapshot) as copy:
                live.backup(copy)
        for idx, case in enumerate(cases, 1):
            # Independent in-memory state for each case. Keep the source and snapshot untouched.
            ash = AshMind(snapshot)
            try:
                context = case.get("context", "")
                frames = None
                if "scene" in case:
                    from ash.perception.text import parse
                    situation, frames = observe_scene(case["scene"])
                    question = parse(case["question"])
                    situation.question = question.question
                    situation.qword = question.qword
                    answer = ash.pipeline().ask(
                        case["question"], situation=situation,
                        observed_motion=(case["scene"].get("shape", "sphere"), frames[-1].get("speed")),
                    )
                else:
                    answer = ash.ask(case["question"], context)
                actual = " ".join(answer.text.split())
                verdict = "exact match" if normalize(actual) == normalize(case["expected"]) else "review"
                visibility = "" if frames is None else " | visibility " + "".join("V" if f["visible"] else "-" for f in frames)
                if actual.startswith("Verified (calculation):"):
                    calculation, marker, limitation = actual.partition(" Missing information:")
                    display = (f"Missing information:{limitation} | {calculation}" if marker else
                               f"Ash returned a calculation without a direct answer. | {actual}")
                else:
                    display = actual
                print(f"CASE {idx}: {verdict}{visibility} | {display}", flush=True)
                print(json.dumps({"case": idx, "answer": actual, "expected": case["expected"],
                                  "abstained": answer.abstained, "frames": frames}, ensure_ascii=False), flush=True)
            finally:
                ash.close()


if __name__ == "__main__":
    signal.alarm(90)
    main(validate(Path(sys.argv[1]).read_text()))
