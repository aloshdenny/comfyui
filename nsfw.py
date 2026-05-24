#!/usr/bin/env python3
"""
nsfw.py  —  Wan2.2 Remix NSFW 20-second video runner
             (ComfyUI-WanVideoWrapper backend)

Usage:
    python nsfw.py --face her.jpg
    python nsfw.py --face her.jpg --scene scene.jpg --prompt "..." --duration 8 --iterations 4

Duration is the 'a' value in the workflow formula: frames = a*b/2+1 where b=16 (fixed in JSON).
  a=10 → 10*16/2+1 = 81 frames = 5.06s @ 16fps — the EXACT Wan2.2 training maximum.
  Beyond 81 frames the model reverses. Use --iterations 4 for ~20s of continuous video.
"""
import argparse, copy, json, os, sys, time, uuid
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

DEFAULT_PROMPT = (
"A young woman with long dark hair stands in a softly lit bedroom during daytime, looking slightly shy but smiling gently. She slowly unbuttons the top of her light-colored shirt while adjusting her outfit, her movements hesitant and natural, as if a little nervous. She reveals her pompous breasts and nipples, squeezes them and as she does so, milk squirts out of those nipples and drips down her belly, all the way to her bellybutton.  She then slowly removes her underwear, revealing a bushy hairy vagina, dripping wet. She fingers herself, inserts two fingers deep inside and stimulates her clitoris with her thumb. She turns around, revealing her fair-skinned buttocks, bending over to reveal her anus. She then begins penetrating herself with a dildo. Then she pulls out the dildo and comes all over her face and tits. At the end she writes Daddy's girl on her tits with her milk. Her expression is warm, sweet, and reserved, with soft daylight streaming through the window to create an intimate, ultra-realistic atmosphere."
)
DEFAULT_NEGATIVE = (
"Vivid tones, overexposed lighting, static blurry details, subtitles, artwork or painting style, still-image appearance, gray overall tone, worst quality, low quality, JPEG compression artifacts, ugly or deformed features, poorly drawn hands and face, distorted or disfigured anatomy, malformed limbs, fused or extra fingers, cluttered background, unnatural body proportions, three legs, and an unmoving frame."
)

# Synthetic node ID for the face-only LoadImage we inject into the API prompt.
# Must be higher than any real node ID in the workflow (last_node_id = 214).
FACE_NODE_ID = "10001"

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
                    consume()
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
#  Per-generation patching
# ─────────────────────────────────────────────────────────────
def patch_api(
    api: dict,
    face_name: str,
    scene_name: str,
    pos_prompt: str,
    neg_prompt: str,
    seed: int,
    duration: int = 8,
) -> dict:
    """
    Patch the API prompt for one 20-second generation.

    Node map (WanVideoWrapper workflow):
      117  — LoadImage         — scene / start frame  (feeds I2V encoder)
      118  — CR Text           — positive prompt
      214  — CR Text           — negative prompt
      198  — PrimitiveInt      — duration in seconds
      all WanVideoSampler      — seed
      all WanVideoClipVisionEncode — image_1 → synthetic face LoadImage (FACE_NODE_ID)
    """
    # ── Scene image ──────────────────────────────────────────────────────────
    if "117" in api:
        api["117"]["inputs"]["image"] = scene_name

    # ── Face image (synthetic node, for Pipeline A's CLIPVision only) ──────
    api[FACE_NODE_ID] = {
        "class_type": "LoadImage",
        "inputs": {"image": face_name, "upload": "image"},
    }
    # Only redirect Pipeline A's CLIPVision (node 201) to the face.
    # Pipeline B's CLIPVision (node 205) keeps using the extracted last frame
    # from Pipeline A — this provides visual continuity instead of resetting
    # to the original face mid-video, which was causing the perceived "loop."
    PIPELINE_A_CLIP = "201"
    if PIPELINE_A_CLIP in api and api[PIPELINE_A_CLIP].get("class_type") == "WanVideoClipVisionEncode":
        api[PIPELINE_A_CLIP]["inputs"]["image_1"] = [FACE_NODE_ID, 0]

    # ── Prompts ──────────────────────────────────────────────────────────────
    if "118" in api:
        api["118"]["inputs"]["text"] = pos_prompt
    if "214" in api:
        api["214"]["inputs"]["text"] = neg_prompt

    # ── Seed (each pass gets a DIFFERENT seed for content diversity) ─────────
    # Same base seed → same set of 4 seeds → reproducible, but each segment
    # explores a different noise region so the action actually progresses.
    resolved = seed if seed != -1 else int(time.time() * 1000) % (2**31)
    sampler_nodes = sorted(
        ((nid, n) for nid, n in api.items() if n.get("class_type") == "WanVideoSampler"),
        key=lambda x: int(x[0]) if x[0].lstrip("-").isdigit() else 0,
    )
    for idx, (_, node) in enumerate(sampler_nodes):
        node["inputs"]["seed"] = (resolved + idx * 31337) % (2**31)


    # ── Duration — clamp to 10 (=81 frames, the Wan2.2 training limit) ────────────
    # Formula: a*16/2+1 where 16 is b (node 199). a=10 → 81 frames = 5.06s @ 16fps.
    # Going above 81 causes the model to reverse at the end of the segment.
    if "198" in api:
        api["198"]["inputs"]["value"] = duration

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
    print(f"  Uploaded {label}: {res['name']}")
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
    print(f"  Queued: {pid}")
    return pid, client_id


def wait_for_completion(prompt_id: str, client_id: str):
    ws = WebSocket()
    ws.connect(f"ws://{COMFY_HOST}:{COMFY_PORT}/ws?clientId={client_id}")
    print("  Generating ", end="", flush=True)
    try:
        while True:
            msg = ws.recv()
            if isinstance(msg, str):
                d = json.loads(msg)
                if d.get("type") == "progress":
                    v, m = d["data"]["value"], d["data"]["max"]
                    print(f"\r  Progress: {v}/{m} steps     ", end="", flush=True)
                elif d.get("type") == "executing":
                    if (d["data"].get("prompt_id") == prompt_id
                            and d["data"].get("node") is None):
                        print("\n  Done!")
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
    print(f"  Saved: {dest}")
    return dest

# ─────────────────────────────────────────────────────────────
#  Video helpers (ffmpeg)
# ─────────────────────────────────────────────────────────────
import subprocess

def extract_last_frame(video_path: str, out_jpg: str) -> str:
    """
    Extract the very last frame of a video into a JPEG.
    Requires ffmpeg on PATH (always present when VHS is installed).
    """
    # -sseof -3  seeks 3 s before EOF to make last-frame capture fast
    r = subprocess.run(
        ["ffmpeg", "-y", "-sseof", "-3", "-i", video_path,
         "-frames:v", "1", "-update", "1", out_jpg],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg extract_last_frame failed:\n{r.stderr[-800:]}")
    return out_jpg


def concatenate_videos(paths: list, out_path: str) -> str:
    """
    Concatenate a list of MP4 clips into one file using ffmpeg concat demuxer.
    Clips must have identical codec/resolution (they do — same workflow).
    """
    list_file = out_path + ".concat_list.txt"
    with open(list_file, "w") as f:
        for p in paths:
            f.write(f"file '{os.path.abspath(p)}'\n")
    r = subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
         "-i", list_file, "-c", "copy", out_path],
        capture_output=True, text=True,
    )
    os.unlink(list_file)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg concat failed:\n{r.stderr[-800:]}")
    return out_path


def rife_interpolate(input_path: str, output_path: str, target_fps: int = 32) -> str:
    """
    Interpolate frame rate using ffmpeg minterpolate (optical-flow, no extra deps).
    Doubles 16fps → 32fps making motion smooth without extra GPU work.
    Returns output_path on success, input_path on error (graceful fallback).
    """
    print(f"  RIFE minterpolate: {target_fps}fps → {output_path}")
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", input_path,
         "-vf",
         f"minterpolate=fps={target_fps}:mi_mode=mci:"
         f"mc_mode=aobmc:me_mode=bidir:vsbmc=1",
         "-r", str(target_fps),
         output_path],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"  [warn] minterpolate failed (falling back): {r.stderr[-300:]}")
        return input_path
    return output_path


# ─────────────────────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(
        description="Wan2.2 Remix NSFW — chained iterative generation (WanVideoWrapper)"
    )
    p.add_argument("--face",       required=True,
                   help="Face image — CLIPVision identity guidance (kept for all iterations)")
    p.add_argument("--scene",      default=None,
                   help="Start-frame image (defaults to face; subsequent iterations auto-use last frame)")
    p.add_argument("--prompt",     default=DEFAULT_PROMPT)
    p.add_argument("--negative",   default=DEFAULT_NEGATIVE)
    p.add_argument("--seed",       type=int, default=-1,
                   help="Base seed (-1 = random). Each iteration uses seed + iter*31337.")
    p.add_argument("--duration",   type=int, default=10,
                   help="'a' value in frame formula a*16/2+1 per segment. Default 10 = 81 frames = 5s (Wan2.2 max).")
    p.add_argument("--iterations", type=int, default=2,
                   help="Chained runs (default 2 ≈ 20s). Each run = ~10s (2×81 frames @ 16fps).")
    p.add_argument("--no-rife",    action="store_true",
                   help="Skip RIFE frame interpolation (16fps→32fps) at the end."),
    p.add_argument("--workflow",   default=str(SCRIPT_DIR / "nsfw.json"))
    p.add_argument("--output-dir", default="./outputs")
    p.add_argument("--dump-api",   action="store_true",
                   help="Write api_dump.json and exit (debug)")
    args = p.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    # Resolve base seed once; each iteration advances it
    base_seed = args.seed if args.seed != -1 else int(time.time() * 1000) % (2**31)

    # Load and convert workflow once (re-patched per iteration)
    print("\n[init] Converting workflow…")
    with open(args.workflow) as f:
        gui_wf = json.load(f)
    base_api = gui_to_api(gui_wf)

    if args.dump_api:
        with open("api_dump.json", "w") as f:
            json.dump(base_api, f, indent=2, ensure_ascii=False)
        print("  Written api_dump.json")
        return

    current_scene_path = args.scene or args.face
    clip_paths: list = []
    ts = time.strftime("%Y%m%d_%H%M%S")

    for it in range(1, args.iterations + 1):
        iter_seed = (base_seed + (it - 1) * 31337) % (2**31)

        print(f"\n{'='*55}")
        print(f"  Segment {it}/{args.iterations}  |  duration-param={args.duration}")
        print(f"  Face  : {args.face}")
        print(f"  Scene : {current_scene_path}")
        print(f"  Seed  : {iter_seed}")
        print(f"{'='*55}")

        # Deep-copy API so each iteration starts from clean state
        api = copy.deepcopy(base_api)

        # ── Upload images ──────────────────────────────────────────────────
        print(f"\n[{it}.1] Uploading images…")
        face_name  = upload_image(args.face,          "face")
        scene_name = upload_image(current_scene_path, "scene")

        # ── Patch ─────────────────────────────────────────────────────────
        print(f"\n[{it}.2] Patching workflow…")
        api = patch_api(
            api, face_name, scene_name,
            args.prompt, args.negative,
            iter_seed,
            duration=args.duration,
        )

        # ── Queue ─────────────────────────────────────────────────────────
        print(f"\n[{it}.3] Queuing…")
        pid, cid = queue_prompt(api)

        # ── Generate ──────────────────────────────────────────────────────
        print(f"\n[{it}.4] Generating…")
        wait_for_completion(pid, cid)

        # ── Download ──────────────────────────────────────────────────────
        files = get_outputs(pid)
        if not files:
            print("  No outputs — check ComfyUI terminal.")
            sys.exit(1)

        print(f"\n[{it}.5] Downloading…")
        saved = [download(fi, args.output_dir) for fi in files]
        # Take the first video file as this iteration's clip
        clip = next((p for p in saved if p.endswith((".mp4", ".webm", ".avi"))), saved[0])
        clip_paths.append(clip)
        print(f"  Clip {it}: {clip}")

        # ── Extract last frame for next iteration ─────────────────────────
        if it < args.iterations:
            last_frame = os.path.join(
                args.output_dir, f"_last_frame_seg{it}_{ts}.jpg"
            )
            print(f"  Extracting last frame → {last_frame}")
            extract_last_frame(clip, last_frame)
            current_scene_path = last_frame  # next segment starts here

    # ── Concatenate if multiple iterations ────────────────────────────────
    if len(clip_paths) > 1:
        final = os.path.join(args.output_dir, f"final_{ts}.mp4")
        print(f"\n[concat] Joining {len(clip_paths)} runs → {final}")
        concatenate_videos(clip_paths, final)
    else:
        final = clip_paths[0]

    # ── RIFE interpolation: 16fps → 32fps ────────────────────────────────
    if not args.no_rife:
        stem   = os.path.splitext(final)[0]
        smooth = stem + "_32fps.mp4"
        print(f"\n[rife] Interpolating to 32fps…")
        final = rife_interpolate(final, smooth, target_fps=32)

    print(f"\n✔  Final output: {final}")


if __name__ == "__main__":
    main()