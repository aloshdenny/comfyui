#!/usr/bin/env python3
"""
nsfw.py  —  Wan2.2 Remix iterative video runner

Usage (single clip):
    python nsfw.py --face her.jpg

Usage (chained clips → long video):
    python nsfw.py --face her.jpg --iterations 8

Each clip uses the last frame of the previous clip as its start frame,
producing a seamless long video when concatenated.
"""
import argparse, copy, json, os, subprocess, sys, time, uuid
import urllib.request, urllib.parse
from pathlib import Path
from websocket import WebSocket

# ─────────────────────────────────────────────────────────────
#  Config
# ─────────────────────────────────────────────────────────────
COMFY_HOST = "127.0.0.1"
COMFY_PORT  = 8188
COMFY_URL   = f"http://{COMFY_HOST}:{COMFY_PORT}"
SCRIPT_DIR  = Path(__file__).parent.resolve()

# Wan2.2 VAE temporal stride: output frames must satisfy (F - 1) % 4 == 0
# Valid values: 5, 9, 13, 17, 21, 25, 29, 33, 37, 41, 45, 49 …
# After RIFE 21 fps → 30 fps interpolation each clip is multiplied by 30/21.

DEFAULT_PROMPT = (
    "A young woman with long dark hair stands on a rooftop during daytime, "
    "looking slightly shy but smiling softly. She gently unbuttons the top of "
    "her light-colored shirt, revealing her Breasts. Her hands move slowly and "
    "hesitantly, as if she's a little nervous while adjusting her outfit. Her "
    "expression is sweet, reserved, and warm, with natural daylight creating a "
    "soft, ultra-realistic mood. Industrial railings and rooftops sit in the "
    "background, with the camera focused gently on her upper body and subtle, "
    "emotional details."
)
DEFAULT_NEGATIVE = (
    "色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，整体发灰，最差质量，"
    "低质量，JPEG压缩残留，丑陋的，残缺的，多余的手指，画得不好的手部，画得不好的脸部，畸形的，"
    "毁容的，形态畸形的肢体，手指融合，静止不动的画面，杂乱的背景，三条腿，背景人很多，倒着走"
)

# ─────────────────────────────────────────────────────────────
#  /object_info cache  (implicit widget slot discovery)
# ─────────────────────────────────────────────────────────────
_OBJ_INFO: dict = {}

def object_info() -> dict:
    global _OBJ_INFO
    if not _OBJ_INFO:
        with urllib.request.urlopen(f"{COMFY_URL}/object_info") as r:
            _OBJ_INFO = json.loads(r.read())
    return _OBJ_INFO

WIDGET_TYPES        = {"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO", "IMAGEUPLOAD"}
SEED_CONTROL_TOKENS = {"randomize", "fixed", "increment", "increment (loop)"}

def implicit_widget_names(class_type: str, explicit_names: set) -> list:
    info = object_info()
    if class_type not in info:
        return []
    result = []
    for group in ("required", "optional"):
        for name, spec in info[class_type]["input"].get(group, {}).items():
            if name in explicit_names:
                continue
            t = spec[0] if isinstance(spec, list) else spec
            if isinstance(t, list):
                t = "COMBO"
            if t in WIDGET_TYPES:
                result.append(name)
    return result

# ─────────────────────────────────────────────────────────────
#  GUI → API conversion
# ─────────────────────────────────────────────────────────────
def bypass_nop_nodes(api: dict) -> dict:
    """
    wanBlockSwap (nodes 202, 203) is NOP'd by ComfyUI core in recent builds.
    Re-wire anything connected to their outputs directly to their UNETLoader
    inputs so native model_management handles memory cleanly.
    """
    for nop_id in ["202", "203"]:
        if nop_id not in api:
            continue
        upstream = api[nop_id]["inputs"].get("model")
        if upstream is None:
            continue
        for node in api.values():
            for k, v in node["inputs"].items():
                if v == [nop_id, 0]:
                    node["inputs"][k] = upstream
        del api[nop_id]
        print(f"  Bypassed wanBlockSwap {nop_id} → {upstream}")
    return api


def gui_to_api(wf: dict) -> dict:
    """Convert a ComfyUI GUI-export JSON to the API prompt format."""
    link_map: dict[int, list] = {
        lnk[0]: [str(lnk[1]), lnk[2]]
        for lnk in wf.get("links", [])
    }

    api: dict = {}

    for node in wf.get("nodes", []):
        class_type = node.get("type", "")
        node_id    = str(node["id"])

        if class_type in ("Note", "Reroute"):
            continue

        raw_wv = node.get("widgets_values", [])
        wv: list = list(raw_wv.values()) if isinstance(raw_wv, dict) else list(raw_wv)

        explicit_inputs = node.get("inputs", [])
        inputs_out: dict = {}
        wv_cursor = 0

        def consume():
            nonlocal wv_cursor
            val = wv[wv_cursor] if wv_cursor < len(wv) else None
            wv_cursor += 1
            while wv_cursor < len(wv) and wv[wv_cursor] in SEED_CONTROL_TOKENS:
                wv_cursor += 1
            return val

        for inp in explicit_inputs:
            name       = inp["name"]
            link_id    = inp.get("link")
            has_widget = "widget" in inp

            if link_id is not None:
                if link_id in link_map:
                    inputs_out[name] = link_map[link_id]
                if has_widget:
                    consume()          # advance cursor past the backed-up value
            elif has_widget:
                inputs_out[name] = consume()

        explicit_names = {inp["name"] for inp in explicit_inputs}
        for name in implicit_widget_names(class_type, explicit_names):
            if wv_cursor < len(wv):
                inputs_out[name] = wv[wv_cursor]
                wv_cursor += 1

        api[node_id] = {"class_type": class_type, "inputs": inputs_out}

    return api

# ─────────────────────────────────────────────────────────────
#  Per-iteration patching
# ─────────────────────────────────────────────────────────────
def patch_api(
    api: dict,
    face_name: str,
    scene_name: str,
    pos_prompt: str,
    neg_prompt: str,
    seed: int,
    frames: int | None = None,
) -> dict:
    """Patch node inputs for one generation pass."""
    if "262" in api:
        api["262"]["inputs"]["image"] = face_name     # Face / CLIPVision source
    if "252" in api:
        api["252"]["inputs"]["image"] = scene_name    # Start frame
    if "6" in api:
        api["6"]["inputs"]["text"]    = pos_prompt
    if "7" in api:
        api["7"]["inputs"]["text"]    = neg_prompt
    if "105" in api:
        resolved = seed if seed != -1 else int(time.time() * 1000) % (2**31)
        api["105"]["inputs"]["seed"]  = resolved
    # Implicit slot fallback for CLIPVisionEncode
    if "261" in api and "crop" not in api["261"]["inputs"]:
        api["261"]["inputs"]["crop"] = "center"
    # Override frame count (node 132 = INTConstant "Frames")
    if frames is not None and "132" in api:
        api["132"]["inputs"]["value"] = frames
    return api

# ─────────────────────────────────────────────────────────────
#  ComfyUI API helpers
# ─────────────────────────────────────────────────────────────
def upload_image(path: str, label: str) -> str:
    with open(path, "rb") as f:
        data = f.read()
    filename = os.path.basename(path)
    boundary = "----FormBoundary" + uuid.uuid4().hex
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'
        f"Content-Type: image/jpeg\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"{COMFY_URL}/upload/image", data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req) as r:
        res = json.loads(r.read())
    print(f"    Uploaded {label}: {res['name']}")
    return res["name"]


def queue_prompt(api_prompt: dict) -> tuple[str, str]:
    client_id = str(uuid.uuid4())
    payload   = json.dumps({"prompt": api_prompt, "client_id": client_id}).encode()
    req = urllib.request.Request(
        f"{COMFY_URL}/prompt", data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as r:
        res = json.loads(r.read())
    pid = res["prompt_id"]
    print(f"    Queued: {pid}")
    return pid, client_id


def wait_for_completion(prompt_id: str, client_id: str):
    ws = WebSocket()
    ws.connect(f"ws://{COMFY_HOST}:{COMFY_PORT}/ws?clientId={client_id}")
    print("    Generating ", end="", flush=True)
    try:
        while True:
            msg = ws.recv()
            if isinstance(msg, str):
                d = json.loads(msg)
                if d.get("type") == "progress":
                    v, m = d["data"]["value"], d["data"]["max"]
                    print(f"\r    Progress: {v}/{m} steps     ", end="", flush=True)
                elif d.get("type") == "executing":
                    if (d["data"].get("prompt_id") == prompt_id
                            and d["data"].get("node") is None):
                        print("\n    Done!")
                        break
                elif d.get("type") == "execution_error":
                    if d["data"].get("prompt_id") == prompt_id:
                        raise RuntimeError(
                            f"ComfyUI error on node {d['data'].get('node_id')}: "
                            f"{d['data'].get('exception_message')}"
                        )
    finally:
        ws.close()


def get_outputs(prompt_id: str) -> list:
    with urllib.request.urlopen(f"{COMFY_URL}/history/{prompt_id}") as r:
        history = json.loads(r.read())
    out = []
    if prompt_id in history:
        for node_out in history[prompt_id]["outputs"].values():
            for key in ("gifs", "videos", "images"):
                out.extend(node_out.get(key, []))
    return out


def download(file_info: dict, out_dir: str) -> str:
    fn     = file_info["filename"]
    sub    = file_info.get("subfolder", "")
    params = urllib.parse.urlencode({"filename": fn, "subfolder": sub, "type": "output"})
    dest   = os.path.join(out_dir, fn)
    urllib.request.urlretrieve(f"{COMFY_URL}/view?{params}", dest)
    print(f"    Saved: {dest}")
    return dest


def free_vram(target_free_gb: float = 12.0, max_wait: int = 90) -> None:
    """
    Unload all ComfyUI models and wait until VRAM is genuinely free.

    Strategy:
      1. POST /free  (unload_models + free_memory)
      3s pause
      2. POST /free again  (catches anything deferred by GC on the server)
      3. Poll GET /system_stats every 3 s until vram_free >= target_free_gb
         or max_wait seconds elapse.

    target_free_gb=12 means we wait until >=12 GB is free on the 24 GB card,
    which confirms both 14B fp8 models have been evicted from VRAM.
    """
    def _call_free() -> bool:
        payload = json.dumps({"unload_models": True, "free_memory": True}).encode()
        req = urllib.request.Request(
            f"{COMFY_URL}/free",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req):
                return True
        except Exception as e:
            print(f"    /free error: {e}")
            return False

    def _vram_free_gb() -> float | None:
        try:
            with urllib.request.urlopen(f"{COMFY_URL}/system_stats") as r:
                stats = json.loads(r.read())
            devices = stats.get("devices", [])
            if not devices:
                return None
            raw = devices[0].get("vram_free", 0)
            # ComfyUI reports in bytes (torch.cuda.mem_get_info)
            return raw / (1024 ** 3)
        except Exception:
            return None

    print("    [free] Unloading models (pass 1)... ", end="", flush=True)
    _call_free()
    print("done")

    time.sleep(3)

    print("    [free] Unloading models (pass 2)... ", end="", flush=True)
    _call_free()
    print("done")

    # Poll until VRAM actually drops
    deadline = time.time() + max_wait
    while time.time() < deadline:
        time.sleep(3)
        free = _vram_free_gb()
        if free is None:
            print("    [free] system_stats unavailable — waiting...")
            continue
        print(f"    [free] VRAM free: {free:.1f} GB (target ≥ {target_free_gb:.0f} GB)     ",
              end="\r", flush=True)
        if free >= target_free_gb:
            print(f"\n    VRAM freed ✓ ({free:.1f} GB free)")
            return

    free = _vram_free_gb() or 0.0
    print(f"\n    VRAM: {free:.1f} GB free after {max_wait}s — proceeding anyway")


# ─────────────────────────────────────────────────────────────
#  ffmpeg helpers  (server-side, no extra Python deps)
# ─────────────────────────────────────────────────────────────
def extract_last_frame(video_path: str, out_path: str) -> str:
    """
    Extract the very last frame of a video as a JPEG.
    Uses ffmpeg's -sseof -0.5 (seek 0.5 s from end) + -frames:v 1.
    Falls back to -update 1 without -sseof for very short clips.
    """
    for seek_args in (["-sseof", "-0.5"], []):
        result = subprocess.run(
            ["ffmpeg", "-y"] + seek_args +
            ["-i", video_path, "-update", "1", "-frames:v", "1", "-q:v", "2", out_path],
            capture_output=True,
        )
        if result.returncode == 0 and os.path.exists(out_path):
            return out_path
    raise RuntimeError(
        f"ffmpeg failed to extract last frame from {video_path}:\n"
        + result.stderr.decode()
    )


def concat_videos(paths: list[str], out_path: str) -> str:
    """
    Stream-copy concatenate MP4 files using ffmpeg concat demuxer.
    No re-encode — instant and lossless.
    """
    list_file = out_path + ".concat.txt"
    with open(list_file, "w") as f:
        for p in paths:
            f.write(f"file '{os.path.abspath(p)}'\n")
    result = subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
         "-i", list_file, "-c", "copy", out_path],
        capture_output=True,
    )
    os.unlink(list_file)
    if result.returncode != 0:
        raise RuntimeError("ffmpeg concat failed:\n" + result.stderr.decode())
    return out_path


def categorize_outputs(files: list) -> dict[str, str | None]:
    """
    Sort downloaded files into quality tiers by filename prefix set in the workflow:
      - 'Upscaled'  → 4× ESRGAN + RIFE interpolated
      - 'RIFE'      → RIFE interpolated (30 fps)
      - 'raw'       → direct VAEDecode output (21 fps)
    Returns the local path for each tier, or None if not present.
    """
    result: dict[str, str | None] = {"upscaled": None, "rife": None, "raw": None}
    for path in files:
        name = os.path.basename(path)
        if "Upscaled" in name:
            result["upscaled"] = path
        elif "RIFE" in name:
            result["rife"] = path
        else:
            result["raw"] = path
    return result

# ─────────────────────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(
        description="Wan2.2 Remix — iterative video generator"
    )
    p.add_argument("--face",        required=True,
                   help="Face image — kept constant for CLIPVision identity across all clips")
    p.add_argument("--scene",       default=None,
                   help="Start-frame image for clip 1 (defaults to face image)")
    p.add_argument("--prompt",      default=DEFAULT_PROMPT)
    p.add_argument("--negative",    default=DEFAULT_NEGATIVE)
    p.add_argument("--seed",        type=int, default=-1,
                   help="Seed (-1 = random per clip, giving natural variation)")
    p.add_argument("--workflow",    default=str(SCRIPT_DIR / "nsfw.json"))
    p.add_argument("--output-dir",  default="./outputs")
    p.add_argument("--iterations",  type=int, default=1,
                   help="Number of sequential clips to generate and concatenate. "
                        "Each clip's last frame becomes the next clip's start frame.")
    p.add_argument("--frames",      type=int, default=None,
                   help="Override frames-per-clip from the JSON. "
                        "Must satisfy (F-1) %% 4 == 0: e.g. 9,13,17,21,25,29,33,49 …")
    p.add_argument("--dump-api",    action="store_true",
                   help="Write api_dump.json and exit (debug)")
    args = p.parse_args()

    # Validate frames constraint
    if args.frames is not None and (args.frames - 1) % 4 != 0:
        p.error(f"--frames {args.frames} is invalid. Must satisfy (F-1) %% 4 == 0 "
                f"(e.g. 9, 13, 17, 21, 25, 29, 33, 49).")

    os.makedirs(args.output_dir, exist_ok=True)

    frames_display = args.frames if args.frames else "(from workflow JSON)"
    duration_each  = (args.frames or 25) / 21          # raw seconds at 21 fps
    duration_rife  = duration_each * (30 / 21)         # after RIFE → 30 fps
    total_duration = duration_rife * args.iterations

    print(f"\n{'='*55}")
    print(f"  Wan2.2 Remix — Iterative Generation")
    print(f"{'='*55}")
    print(f"  Iterations  : {args.iterations}")
    print(f"  Frames/clip : {frames_display}")
    print(f"  Per clip    : ~{duration_rife:.1f}s @ 30 fps (after RIFE)")
    print(f"  Total target: ~{total_duration:.1f}s")
    print(f"{'='*55}")

    # ── Convert workflow once; deepcopy per iteration ──────────────────────────
    print("\n[init] Converting workflow…")
    with open(args.workflow) as f:
        gui_wf = json.load(f)
    base_api = gui_to_api(gui_wf)
    base_api = bypass_nop_nodes(base_api)

    if args.dump_api:
        with open("api_dump.json", "w") as f:
            json.dump(base_api, f, indent=2, ensure_ascii=False)
        print("  Written api_dump.json")
        return

    # ── Iterative generation loop ──────────────────────────────────────────────
    current_scene: str = args.scene or args.face
    best_clips:    list[str] = []

    for i in range(args.iterations):
        clip_num = i + 1
        print(f"\n{'─'*55}")
        print(f"  Clip {clip_num}/{args.iterations}")
        print(f"{'─'*55}")

        clip_dir = os.path.join(args.output_dir, f"clip_{clip_num:03d}")
        os.makedirs(clip_dir, exist_ok=True)

        # 1. Upload ──────────────────────────────────────────────────────────
        print("\n  [1] Uploading images…")
        face_name  = upload_image(args.face,      "face")
        scene_name = upload_image(current_scene,  "scene")

        # 2. Patch ───────────────────────────────────────────────────────────
        api = patch_api(
            copy.deepcopy(base_api),
            face_name, scene_name,
            args.prompt, args.negative,
            args.seed,            # -1 = random each clip for natural motion variation
            frames=args.frames,
        )

        # 3. Queue ───────────────────────────────────────────────────────────
        print("\n  [2] Queueing…")
        pid, cid = queue_prompt(api)

        # 4. Generate ────────────────────────────────────────────────────────
        print("\n  [3] Generating…")
        wait_for_completion(pid, cid)

        # 5. Download ────────────────────────────────────────────────────────
        files = get_outputs(pid)
        if not files:
            print("  ✗ No outputs — check ComfyUI terminal. Stopping.")
            sys.exit(1)

        print(f"\n  [4] Downloading {len(files)} file(s)…")
        local_paths: list[str] = [download(fi, clip_dir) for fi in files]
        tiers = categorize_outputs(local_paths)

        # Best quality for final concat: upscaled > RIFE > raw
        best = tiers["upscaled"] or tiers["rife"] or tiers["raw"]
        if best:
            best_clips.append(best)
            tier_label = ("upscaled" if tiers["upscaled"]
                          else "RIFE 30fps" if tiers["rife"]
                          else "raw 21fps")
            print(f"    Best clip ({tier_label}): {best}")

        # 6. Free VRAM before next clip ──────────────────────────────────────
        if i < args.iterations - 1:
            print("\n  [5] Freeing VRAM…")
            free_vram()

        # 7. Extract last frame as start scene for the next clip ─────────────
        if i < args.iterations - 1:
            raw = tiers["raw"]
            if not raw:
                print("  ✗ No raw video found — cannot chain to next clip. Stopping.")
                break
            next_scene = os.path.join(args.output_dir, f"scene_clip_{clip_num+1:03d}.jpg")
            print(f"\n  [5] Extracting last frame → {next_scene}")
            try:
                extract_last_frame(raw, next_scene)
                current_scene = next_scene
            except RuntimeError as e:
                print(f"  ✗ {e}\n  Stopping iteration.")
                break

    # ── Concatenate all clips ──────────────────────────────────────────────────
    if len(best_clips) > 1:
        final = os.path.join(args.output_dir, "final.mp4")
        print(f"\n{'='*55}")
        print(f"  Concatenating {len(best_clips)} clips → final.mp4")
        print(f"{'='*55}")
        try:
            concat_videos(best_clips, final)
            actual = len(best_clips) * duration_rife
            print(f"  ✓ {final}")
            print(f"  Duration: ~{actual:.1f}s @ 30 fps")
        except RuntimeError as e:
            print(f"  ✗ Concat failed: {e}")
    elif best_clips:
        print(f"\n  Output: {best_clips[0]}")

    print("\nDone ✓")


if __name__ == "__main__":
    main()