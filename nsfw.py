#!/usr/bin/env python3
"""
nsfw.py  —  Wan2.2 Remix NSFW  chained-act video generator
             (ComfyUI-WanVideoWrapper, single-pass per act)

Strategy:
  • Each act = one ComfyUI run = Pipeline A only = 81 frames @ 16fps = 5s
  • 4 acts × 5s = 20s total, no internal dual-pipeline complexity
  • Acts are chained: last frame of act N → start frame of act N+1
  • ffmpeg concatenates all clips → optional RIFE minterpolate 16→32fps

  The workflow JSON is loaded once, converted to API format via /object_info
  (so field names are always correct), then Pipeline B nodes are stripped
  before queuing each act.

Usage:
    python nsfw.py --face her.jpg
    python nsfw.py --face her.jpg --scene scene.jpg --acts 4
    python nsfw.py --face her.jpg --no-rife --seed 42
"""
import argparse, copy, json, os, subprocess, sys, time, uuid
import urllib.request, urllib.parse
from pathlib import Path
from websocket import WebSocket

# ─────────────────────────────────────────────────────────────
#  Config
# ─────────────────────────────────────────────────────────────
COMFY_HOST = "127.0.0.1"
COMFY_PORT= 8188
COMFY_URL = f"http://{COMFY_HOST}:{COMFY_PORT}"
SCRIPT_DIR= Path(__file__).parent.resolve()

# Wan2.2 I2V training limits — beyond these the model reverses
WAN_FRAMES = 81   # max frames per pass
WAN_FPS  = 16   # native fps

# Pipeline B node IDs (str) — stripped so each run is a single 81-frame pass
PIPELINE_B_NODES = {
    "180", "181",         # model loaders for B
    "164", "165",         # SetLoRAs for B
    "166", "167",         # SetBlockSwap for B
    "173", "174",         # text encoders for B
    "176",                # NAG for B
    "205",                # CLIPVisionEncode for B
    "183",                # I2VEncode for B
    "178", "177",         # samplers for B
    "186",                # WanVideoDecode for B
    "147",                # ImageFromBatch+ (extracts frame for B start)
    "192",                # SimpleMath+ (frame index for 147)
    "188",                # GetImageSize for B
    "191",                # ImageBatch (combines A+B — no longer needed)
    "171", "206",         # easy cleanGpuUsed for B model loaders
    "161", "163",         # easy cleanGpuUsed for B samplers
}

# Synthetic node injected for face-only CLIPVision input
FACE_NODE_ID = "10001"

# ─────────────────────────────────────────────────────────────
#  Default prompts — 4 sequential acts, each describes ~5s
# ─────────────────────────────────────────────────────────────
DEFAULT_ACTS = [
    # Act 1: Setting + undressing top
    (
        "A young woman with long dark hair stands in a softly lit bedroom "
        "during daytime, looking slightly shy but smiling gently. She slowly "
        "unbuttons her light-colored shirt from top to bottom, her movements "
        "natural and hesitant. She peels the shirt fully open, revealing her "
        "large bare breasts with erect nipples. She looks down with a warm "
        "smile. Ultra-realistic, soft daylight through the window, "
        "continuous smooth motion, no reversal."
    ),
    # Act 2: Breast play
    (
        "The young woman cups both bare breasts with her hands and gently "
        "squeezes them. As she presses, milk slowly beads at her nipples "
        "and drips down her bare belly toward her navel. She looks down at "
        "her chest with a quiet, shy expression. Same softly lit bedroom, "
        "daylight from window, ultra-realistic, steady camera, "
        "continuous smooth motion."
    ),
    # Act 3: Lower body reveal + self-pleasure
    (
        "The young woman slides her underwear down past her hips and steps "
        "out of it, revealing a natural hairy vagina glistening with "
        "moisture. She parts her thighs, presses two fingers inside herself "
        "and stimulates her clitoris with her thumb. Her face shows quiet "
        "pleasure. Same bedroom, same soft daylight, ultra-realistic, "
        "continuous smooth motion, no looping."
    ),
    # Act 4: Turning + climax
    (
        "The young woman turns around and bends forward, presenting her "
        "fair-skinned round buttocks to the camera and revealing her anus. "
        "She reaches back between her legs and continues pleasuring herself. "
        "Her body trembles and she climaxes, fluids visible on her thighs. "
        "Same bedroom, soft daylight, ultra-realistic, continuous motion "
        "to the very end of the clip."
    ),
]

DEFAULT_NEGATIVE = (
    "static image, still frame, looping, reversing, boomerang, "
    "returning to start frame, vivid oversaturated tones, overexposed, "
    "blurry, subtitles, artwork, painting, gray tone, worst quality, "
    "low quality, JPEG artifacts, ugly, deformed hands, deformed face, "
    "extra fingers, three legs, unmoving frame, text overlay"
)

# ─────────────────────────────────────────────────────────────
#  /object_info cache  (widget-slot name lookup)
# ─────────────────────────────────────────────────────────────
_OBJ_INFO: dict = {}

def object_info() -> dict:
    global _OBJ_INFO
    if not _OBJ_INFO:
        with urllib.request.urlopen(f"{COMFY_URL}/object_info") as r:
            _OBJ_INFO = json.loads(r.read())
    return _OBJ_INFO

WIDGET_TYPES = {"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO", "IMAGEUPLOAD"}

def implicit_widgets(class_type: str) -> list:
    """Return ordered list of widget-slot names for class_type (from /object_info)."""
    info = object_info()
    if class_type not in info:
        return []
    spec = info[class_type].get("input", {})
    names = []
    for name, defn in {**spec.get("required", {}), **spec.get("optional", {})}.items():
        t = defn[0]
        if isinstance(t, list) or (isinstance(t, str) and t in WIDGET_TYPES):
            names.append(name)
    return names

# ─────────────────────────────────────────────────────────────
#  GUI → API conversion
# ─────────────────────────────────────────────────────────────
def gui_to_api(gui_wf: dict) -> dict:
    """
    Convert a ComfyUI GUI-format JSON to API-format using /object_info.
    This guarantees correct field names regardless of WanVideoWrapper version.
    """
    link_tbl = {lnk[0]: (str(lnk[1]), lnk[2]) for lnk in gui_wf.get("links", [])}
    api: dict = {}

    for node in gui_wf["nodes"]:
        class_type = node.get("type", "")
        if class_type in ("Note", "Reroute"):
            continue

        nid = str(node["id"])
        widget_names = implicit_widgets(class_type)
        inputs: dict = {}

        # Linked inputs → node reference
        for inp in node.get("inputs", []):
            link_id = inp.get("link")
            if link_id is not None and link_id in link_tbl:
                inputs[inp["name"]] = list(link_tbl[link_id])

        # Widget values → inline values (skip slots already linked)
        # widgets_values can be a list (most nodes) or a dict (e.g. VHS_VideoCombine).
        # ComfyUI GUI injects seed-control sentinels ('randomize', 'fixed', …) into
        # widget_values for nodes with seed inputs. These are client-side only and must
        # be stripped before positional mapping against /object_info field names.
        SEED_CTRL = frozenset({
            "randomize", "fixed", "increment", "decrement",
            "control_before_generate", "control_after_generate",
        })
        wv = node.get("widgets_values", [])
        if isinstance(wv, dict):
            # Dict format: keyed directly by field name — just skip sentinel values
            for name in widget_names:
                if name in inputs:
                    continue
                if name in wv:
                    val = wv[name]
                    if val not in SEED_CTRL:
                        inputs[name] = val
        else:
            # List format: strip seed-control sentinels first, then map positionally
            wv_clean = [v for v in wv if v not in SEED_CTRL]
            wi = 0
            for name in widget_names:
                if name in inputs:
                    wi += 1   # linked slot still consumes a cleaned position
                    continue
                if wi < len(wv_clean):
                    inputs[name] = wv_clean[wi]
                wi += 1


        api[nid] = {"class_type": class_type, "inputs": inputs}

    return api

# ─────────────────────────────────────────────────────────────
#  Strip Pipeline B — make each run a clean single 81-frame pass
# ─────────────────────────────────────────────────────────────
def strip_pipeline_b(api: dict) -> dict:
    """
    Remove all Pipeline B nodes from the API dict so each ComfyUI run
    generates exactly one 81-frame segment (Pipeline A only).
    Redirect VHS node 185's images input from ImageBatch 191
    to WanVideoDecode 96 (Pipeline A's output).
    """
    api = {k: v for k, v in api.items() if k not in PIPELINE_B_NODES}

    # Redirect VHS output to Pipeline A decode
    if "185" in api:
        api["185"]["inputs"]["images"] = ["96", 0]

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
    duration: int = 10,
) -> dict:
    """
    Patch the API prompt for one act.

    Node map:
      117  LoadImage         — scene / start frame
      118  CR Text           — positive prompt
      214  CR Text           — negative prompt
      198  PrimitiveInt      — 'a' value in frame formula (a*16/2+1)
      201  WanVideoClipVisionEncode — Pipeline A identity (→ face)
      all WanVideoSampler    — seed
    """
    # Scene start frame
    if "117" in api:
        api["117"]["inputs"]["image"] = scene_name

    # Inject face LoadImage node (synthetic)
    api[FACE_NODE_ID] = {
        "class_type": "LoadImage",
        "inputs": {"image": face_name, "upload": "image"},
    }
    # Only redirect Pipeline A's CLIPVision (201) to the face.
    # (Pipeline B's CLIPVision 205 is already stripped.)
    if "201" in api:
        api["201"]["inputs"]["image_1"] = [FACE_NODE_ID, 0]

    # Positive prompt (via CR Text 118)
    if "118" in api:
        api["118"]["inputs"]["text"] = pos_prompt

    # Negative prompt (via CR Text 214)
    if "214" in api:
        api["214"]["inputs"]["text"] = neg_prompt

    # Duration → frame count: a*16/2+1, a=10 → 81 frames
    if "198" in api:
        api["198"]["inputs"]["value"] = duration

    # Seed — different per sampler so action progresses
    resolved = seed if seed != -1 else int(time.time() * 1000) % (2**31)
    samplers = sorted(
        [(nid, n) for nid, n in api.items() if n.get("class_type") == "WanVideoSampler"],
        key=lambda x: int(x[0]) if x[0].isdigit() else 0,
    )
    for idx, (_, node) in enumerate(samplers):
        node["inputs"]["seed"] = (resolved + idx * 31337) % (2**31)

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


def queue_prompt(api_prompt: dict) -> tuple:
    client_id = str(uuid.uuid4())
    payload = json.dumps({"prompt": api_prompt, "client_id": client_id}).encode()
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
            if not isinstance(msg, str):
                continue
            d = json.loads(msg)
            if d.get("type") == "progress":
                v, m = d["data"]["value"], d["data"]["max"]
                print(f"\r  Progress: {v}/{m} steps     ", end="", flush=True)
            elif d.get("type") == "executing":
                data = d["data"]
                if data.get("prompt_id") == prompt_id and data.get("node") is None:
                    print("\n  Done!")
                    break
            elif d.get("type") == "execution_error":
                data = d["data"]
                if data.get("prompt_id") == prompt_id:
                    raise RuntimeError(
                        f"ComfyUI error on node {data.get('node_id')}: "
                        f"{data.get('exception_message')}"
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
    fn   = file_info["filename"]
    sub  = file_info.get("subfolder", "")
    params = urllib.parse.urlencode({"filename": fn, "subfolder": sub, "type": "output"})
    dest = os.path.join(out_dir, fn)
    urllib.request.urlretrieve(f"{COMFY_URL}/view?{params}", dest)
    print(f"  Saved: {dest}")
    return dest

# ─────────────────────────────────────────────────────────────
#  ffmpeg helpers
# ─────────────────────────────────────────────────────────────
def extract_last_frame(video_path: str, out_jpg: str) -> str:
    """Extract the very last frame of a video as JPEG."""
    r = subprocess.run(
        ["ffmpeg", "-y", "-sseof", "-3", "-i", video_path,
         "-frames:v", "1", "-update", "1", out_jpg],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg extract_last_frame failed:\n{r.stderr[-800:]}")
    return out_jpg


def concatenate_videos(paths: list, out_path: str) -> str:
    """Lossless concat via ffmpeg concat demuxer (identical codec/resolution)."""
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
    """Optical-flow frame interpolation via ffmpeg minterpolate (CPU, no extra deps)."""
    print(f"  minterpolate {WAN_FPS}fps → {target_fps}fps…")
    r = subprocess.run(
        ["ffmpeg", "-y", "-i", input_path,
         "-vf",
         f"minterpolate=fps={target_fps}:mi_mode=mci:"
         f"mc_mode=aobmc:me_mode=bidir:vsbmc=1",
         "-r", str(target_fps), output_path],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        print(f"  [warn] minterpolate failed — keeping original: {r.stderr[-300:]}")
        return input_path
    return output_path

# ─────────────────────────────────────────────────────────────
#  Main
# ─────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(
        description="Wan2.2 Remix NSFW — chained single-pass acts (81 frames each)"
    )
    p.add_argument("--face",       required=True,
                   help="Face image — CLIPVision identity (constant across all acts)")
    p.add_argument("--scene",      default=None,
                   help="Start-frame image (defaults to face; subsequent acts use last frame)")
    p.add_argument("--acts",       type=int, default=None,
                   help="Number of acts to run (default: all 4)")
    p.add_argument("--negative",   default=DEFAULT_NEGATIVE)
    p.add_argument("--seed",       type=int, default=-1,
                   help="Base seed (-1 = random). Each act advances by 31337.")
    p.add_argument("--workflow",   default=str(SCRIPT_DIR / "nsfw.json"),
                   help="Path to the ComfyUI GUI-format workflow JSON")
    p.add_argument("--output-dir", default="./outputs")
    p.add_argument("--no-rife",    action="store_true",
                   help="Skip RIFE frame interpolation (16fps → 32fps)")
    p.add_argument("--dump-api",   action="store_true",
                   help="Write api_dump.json (act 1, post-strip) and exit")
    args = p.parse_args()

    acts = DEFAULT_ACTS[: args.acts] if args.acts else DEFAULT_ACTS
    num_acts = len(acts)
    os.makedirs(args.output_dir, exist_ok=True)
    base_seed = args.seed if args.seed != -1 else int(time.time() * 1000) % (2**31)
    ts = time.strftime("%Y%m%d_%H%M%S")

    print(f"\n{'='*60}")
    print(f"  Wan2.2 Remix NSFW")
    print(f"  {num_acts} acts × {WAN_FRAMES} frames @ {WAN_FPS}fps = "
          f"{num_acts * WAN_FRAMES / WAN_FPS:.0f}s raw")
    print(f"  Face  : {args.face}")
    print(f"  Seed  : {base_seed}")
    print(f"{'='*60}")

    # Load + convert workflow once (field names come from live /object_info)
    print("\n[init] Loading workflow + querying /object_info…")
    with open(args.workflow) as f:
        gui_wf = json.load(f)
    base_api = gui_to_api(gui_wf)

    # Strip Pipeline B → single 81-frame pass per run
    base_api = strip_pipeline_b(base_api)
    print(f"  Workflow loaded: {len(base_api)} nodes (Pipeline B stripped)")

    if args.dump_api:
        dummy = copy.deepcopy(base_api)
        dummy = patch_api(dummy, "face.jpg", "face.jpg", acts[0], args.negative, base_seed)
        with open("api_dump.json", "w") as f:
            json.dump(dummy, f, indent=2, ensure_ascii=False)
        print("  Written api_dump.json — exiting.")
        return

    current_scene = args.scene or args.face
    clip_paths: list = []

    for i, act_prompt in enumerate(acts, 1):
        act_seed = (base_seed + (i - 1) * 31337) % (2**31)

        print(f"\n{'─'*60}")
        print(f"  Act {i}/{num_acts}")
        print(f"  Prompt : {act_prompt[:90]}…")
        print(f"  Scene  : {current_scene}")
        print(f"  Seed   : {act_seed}")
        print(f"{'─'*60}")

        # Upload
        print(f"\n[{i}.1] Uploading images…")
        face_name= upload_image(args.face, "face")
        scene_name = upload_image(current_scene, "scene")

        # Patch a fresh copy of the stripped API
        print(f"\n[{i}.2] Patching…")
        api = patch_api(
            copy.deepcopy(base_api),
            face_name, scene_name,
            act_prompt, args.negative,
            act_seed,
        )

        # Queue
        print(f"\n[{i}.3] Queuing…")
        pid, cid = queue_prompt(api)

        # Generate
        print(f"\n[{i}.4] Generating…")
        wait_for_completion(pid, cid)

        # Download
        files = get_outputs(pid)
        if not files:
            print("  No outputs — check ComfyUI terminal.")
            sys.exit(1)

        print(f"\n[{i}.5] Downloading…")
        saved = [download(fi, args.output_dir) for fi in files]
        clip= next((p for p in saved if p.endswith((".mp4", ".webm", ".avi"))), saved[0])
        clip_paths.append(clip)
        print(f"  Act {i}: {clip}")

        # Extract last frame for next act's start
        if i < num_acts:
            last_frame = os.path.join(args.output_dir, f"_last_frame_act{i}_{ts}.jpg")
            print(f"  Extracting last frame → {last_frame}")
            extract_last_frame(clip, last_frame)
            current_scene = last_frame

    # Concatenate
    if len(clip_paths) > 1:
        final = os.path.join(args.output_dir, f"final_{ts}.mp4")
        print(f"\n[concat] Joining {len(clip_paths)} acts → {final}")
        concatenate_videos(clip_paths, final)
    else:
        final = clip_paths[0]

    # RIFE interpolation
    if not args.no_rife:
        stem = os.path.splitext(final)[0]
        smooth = stem + "_32fps.mp4"
        print(f"\n[rife] Interpolating to 32fps…")
        final = rife_interpolate(final, smooth, target_fps=32)

    print(f"\n✔  Final: {final}")
    print(f"   ~{num_acts * WAN_FRAMES / WAN_FPS:.0f}s @ {WAN_FPS}fps raw"
          f"{'  →  32fps with RIFE' if not args.no_rife else ''}")


if __name__ == "__main__":
    main()