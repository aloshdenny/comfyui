#!/usr/bin/env python3
"""
nsfw.py  —  Wan2.2 Remix NSFW 20-second video runner
             (ComfyUI-WanVideoWrapper backend)

Usage:
    python nsfw.py --face her.jpg
    python nsfw.py --face her.jpg --scene scene.jpg --prompt "..." --duration 20
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
"A young woman with long dark hair stands in a softly lit bedroom during daytime, looking slightly shy but smiling gently. She slowly unbuttons the top of her light-colored shirt while adjusting her outfit, her movements hesitant and natural, as if a little nervous. She reveals her pompous breasts and nipples, squeezes them and ejaculates milk. Her expression is warm, sweet, and reserved, with soft daylight streaming through the window to create an intimate, ultra-realistic atmosphere. A neatly made bed, curtains, and subtle bedroom decor fill the background, while the camera focuses delicately on her upper body and subtle emotional details."
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
    duration: int = 20,
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

    # ── Face image (synthetic node, only for CLIPVision) ────────────────────
    api[FACE_NODE_ID] = {
        "class_type": "LoadImage",
        "inputs": {"image": face_name, "upload": "image"},
    }
    # Redirect every WanVideoClipVisionEncode's image_1 to the face node
    for node in api.values():
        if node.get("class_type") == "WanVideoClipVisionEncode":
            node["inputs"]["image_1"] = [FACE_NODE_ID, 0]

    # ── Prompts ──────────────────────────────────────────────────────────────
    if "118" in api:
        api["118"]["inputs"]["text"] = pos_prompt
    if "214" in api:
        api["214"]["inputs"]["text"] = neg_prompt

    # ── Seed (all sampler passes get the same seed for coherence) ────────────
    resolved = seed if seed != -1 else int(time.time() * 1000) % (2**31)
    for node in api.values():
        if node.get("class_type") == "WanVideoSampler":
            node["inputs"]["seed"] = resolved

    # ── Duration ─────────────────────────────────────────────────────────────
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
#  Main
# ─────────────────────────────────────────────────────────────
def main():
    p = argparse.ArgumentParser(
        description="Wan2.2 Remix NSFW — 20-second native generation (WanVideoWrapper)"
    )
    p.add_argument("--face",        required=True,
                   help="Face image — used for CLIPVision identity guidance")
    p.add_argument("--scene",       default=None,
                   help="Start-frame image for the video (defaults to face)")
    p.add_argument("--prompt",      default=DEFAULT_PROMPT)
    p.add_argument("--negative",    default=DEFAULT_NEGATIVE)
    p.add_argument("--seed",        type=int, default=-1,
                   help="Seed (-1 = random)")
    p.add_argument("--duration",    type=int, default=20,
                   help="Video length in seconds (default: 20)")
    p.add_argument("--workflow",    default=str(SCRIPT_DIR / "nsfw.json"))
    p.add_argument("--output-dir",  default="./outputs")
    p.add_argument("--dump-api",    action="store_true",
                   help="Write api_dump.json and exit (debug)")
    args = p.parse_args()

    scene_path = args.scene or args.face
    os.makedirs(args.output_dir, exist_ok=True)

    print(f"\n{'='*55}")
    print(f"  Wan2.2 Remix NSFW — {args.duration}s native generation")
    print(f"{'='*55}")
    print(f"  Face  : {args.face}")
    print(f"  Scene : {scene_path}")
    print(f"  Seed  : {'random' if args.seed == -1 else args.seed}")

    # ── Convert workflow ───────────────────────────────────────────────────
    print("\n[init] Converting workflow…")
    with open(args.workflow) as f:
        gui_wf = json.load(f)
    api = gui_to_api(gui_wf)

    if args.dump_api:
        with open("api_dump.json", "w") as f:
            json.dump(api, f, indent=2, ensure_ascii=False)
        print("  Written api_dump.json")
        return

    # ── Upload ─────────────────────────────────────────────────────────────
    print("\n[1] Uploading images…")
    face_name  = upload_image(args.face,  "face")
    scene_name = upload_image(scene_path, "scene")

    # ── Patch ──────────────────────────────────────────────────────────────
    print("\n[2] Patching workflow…")
    api = patch_api(
        api, face_name, scene_name,
        args.prompt, args.negative,
        args.seed, duration=args.duration,
    )

    # ── Queue ──────────────────────────────────────────────────────────────
    print("\n[3] Queuing…")
    pid, cid = queue_prompt(api)

    # ── Generate (multi-pass, handled internally by WanVideoWrapper) ────────
    print("\n[4] Generating (WanVideoWrapper multi-pass)…")
    wait_for_completion(pid, cid)

    # ── Download ───────────────────────────────────────────────────────────
    files = get_outputs(pid)
    if not files:
        print("  No outputs — check ComfyUI terminal.")
        sys.exit(1)

    print(f"\n[5] Downloading {len(files)} file(s)…")
    for fi in files:
        download(fi, args.output_dir)

    print("\nDone ✓")


if __name__ == "__main__":
    main()