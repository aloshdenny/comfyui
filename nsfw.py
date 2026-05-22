#!/usr/bin/env python3
import argparse, json, uuid, urllib.request, urllib.parse, time, sys, os
from pathlib import Path
from websocket import WebSocket

COMFY_HOST = "127.0.0.1"
COMFY_PORT  = 8188
COMFY_URL   = f"http://{COMFY_HOST}:{COMFY_PORT}"
SCRIPT_DIR  = Path(__file__).parent.resolve()

DEFAULT_PROMPT = (
    "A young woman with long dark hair stands on a sunlit rooftop during the day, "
    "smiling warmly and looking slightly shy. She adjusts the collar of her light-colored "
    "blouse, smoothing it neatly with both hands in a gentle, composed gesture. Her expression "
    "is sweet and natural, radiating confidence and ease. Soft natural daylight falls across "
    "her face, creating a warm and cinematic mood. Industrial railings and city rooftops fill "
    "the background, with the camera gently focused on her face and upper body, capturing fine "
    "detail and subtle emotional nuance. Photorealistic, ultra-high detail, natural skin texture, "
    "elegant composition."
)
DEFAULT_NEGATIVE = (
    "色调艳丽，过曝，静态，细节模糊不清，字幕，风格，作品，画作，画面，静止，整体发灰，最差质量，"
    "低质量，JPEG压缩残留，丑陋的，残缺的，多余的手指，画得不好的手部，画得不好的脸部，畸形的，"
    "毁容的，形态畸形的肢体，手指融合，静止不动的画面，杂乱的背景，三条腿，背景人很多，倒着走"
)

_OBJ_INFO: dict = {}

def object_info() -> dict:
    global _OBJ_INFO
    if not _OBJ_INFO:
        with urllib.request.urlopen(f"{COMFY_URL}/object_info") as r:
            _OBJ_INFO = json.loads(r.read())
    return _OBJ_INFO

WIDGET_TYPES = {"INT", "FLOAT", "STRING", "BOOLEAN", "COMBO", "IMAGEUPLOAD"}

# Tokens ComfyUI injects after INT seed widgets — never sent to backend
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



def bypass_nop_nodes(api: dict) -> dict:
    """
    wanBlockSwap (nodes 202, 203) is NOP'd by ComfyUI core.
    Re-wire anything connected to their outputs directly to their model inputs.
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
        print(f"  Bypassed wanBlockSwap node {nop_id} → wired directly to {upstream}")
    return api

def gui_to_api(wf: dict) -> dict:
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
            # Skip seed-control tokens injected by the GUI frontend
            while wv_cursor < len(wv) and wv[wv_cursor] in SEED_CONTROL_TOKENS:
                wv_cursor += 1
            return val

        # Pass 1: explicit inputs in GUI declaration order
        for inp in explicit_inputs:
            name       = inp["name"]
            link_id    = inp.get("link")
            has_widget = "widget" in inp

            if link_id is not None:
                if link_id in link_map:
                    inputs_out[name] = link_map[link_id]
                if has_widget:
                    consume()           # skip the backed-up widget value
            elif has_widget:
                inputs_out[name] = consume()
            # pure wire with no connection → omit

        # Pass 2: implicit widget slots (e.g. CLIPVisionEncode.crop)
        explicit_names = {inp["name"] for inp in explicit_inputs}
        for name in implicit_widget_names(class_type, explicit_names):
            if wv_cursor < len(wv):
                inputs_out[name] = wv[wv_cursor]
                wv_cursor += 1

        api[node_id] = {"class_type": class_type, "inputs": inputs_out}

    return api


def patch_api(api, face_name, scene_name, pos_prompt, neg_prompt, seed):
    if "262" in api:
        api["262"]["inputs"]["image"] = face_name
    if "252" in api:
        api["252"]["inputs"]["image"] = scene_name
    if "6"   in api:
        api["6"]["inputs"]["text"]    = pos_prompt
    if "7"   in api:
        api["7"]["inputs"]["text"]    = neg_prompt
    if "105" in api:
        resolved = seed if seed != -1 else int(time.time() * 1000) % (2**31)
        api["105"]["inputs"]["seed"]  = resolved
    # Ensure CLIPVisionEncode always has crop (implicit slot fallback)
    if "261" in api and "crop" not in api["261"]["inputs"]:
        api["261"]["inputs"]["crop"] = "center"
    # wanBlockSwap nodes — inject missing use_non_blocking
    for nid in ["202", "203"]:
        if nid in api:
            api[nid]["inputs"]["use_non_blocking"] = False
    return api


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
    print(f"  Queued: {pid}  (client: {client_id})")
    return pid, client_id


def wait_for_completion(prompt_id: str, client_id: str):
    ws = WebSocket()
    ws.connect(f"ws://{COMFY_HOST}:{COMFY_PORT}/ws?clientId={client_id}")
    print("  Generating", end="", flush=True)
    try:
        while True:
            msg = ws.recv()
            if isinstance(msg, str):
                d = json.loads(msg)
                if d.get("type") == "progress":
                    v, m = d["data"]["value"], d["data"]["max"]
                    print(f"\r  Progress: {v}/{m} steps     ", end="", flush=True)
                elif d.get("type") == "executing":
                    if d["data"].get("prompt_id") == prompt_id and d["data"].get("node") is None:
                        print("\n  Done!")
                        break
    finally:
        ws.close()


def get_outputs(prompt_id: str) -> list:
    with urllib.request.urlopen(f"{COMFY_URL}/history/{prompt_id}") as r:
        history = json.loads(r.read())
    out = []
    with open(args.workflow) as f:
        gui_wf = json.load(f)

    api = gui_to_api(gui_wf)
    api = bypass_nop_nodes(api)

    if args.dump_api:
        with open("api_dump.json", "w") as f:
            json.dump(api, f, indent=2, ensure_ascii=False)
        print("  Written api_dump.json")
        for nid, label in [("57","KSamplerAdvanced#57"), ("58","KSamplerAdvanced#58"),
                            ("105","Seed"), ("261","CLIPVisionEncode")]:
            if nid in api:
                print(f"\n  {label}:")
                print(json.dumps(api[nid]["inputs"], indent=4, ensure_ascii=False))
        return

    print("\n[1/4] Uploading images...")
    face_name  = upload_image(args.face,  "face")
    scene_name = upload_image(scene_path, "scene")

    print("\n[2/4] Patching...")
    api = patch_api(api, face_name, scene_name, args.prompt, args.negative, args.seed)

    print("\n[3/4] Queueing...")
    prompt_id, client_id = queue_prompt(api)

    print("\n[4/4] Waiting...")
    wait_for_completion(prompt_id, client_id)

    files = get_outputs(prompt_id)
    if not files:
        print("  No outputs — check ComfyUI terminal.")
        sys.exit(1)

    print(f"\n  Downloading {len(files)} file(s)...")
    for fi in files:
        download(fi, args.output_dir)

    print("\nDone ✓")


if __name__ == "__main__":
    main()