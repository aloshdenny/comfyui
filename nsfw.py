#!/usr/bin/env python3
"""
nsfw.py
Usage: python nsfw.py --face face.jpg --scene scene.jpg --prompt "your prompt"
       python nsfw.py --face face.jpg --scene scene.jpg  # uses default prompt
"""

import argparse
import json
import uuid
import urllib.request
import urllib.parse
import time
import sys
import os
import shutil
from pathlib import Path
from websocket import WebSocket  # pip install websocket-client

COMFY_HOST = "127.0.0.1"
COMFY_PORT = 8188
COMFY_URL = f"http://{COMFY_HOST}:{COMFY_PORT}"

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


def upload_image(image_path: str, name: str) -> str:
    """Upload an image to ComfyUI and return the filename it was stored as."""
    with open(image_path, "rb") as f:
        data = f.read()

    filename = os.path.basename(image_path)
    boundary = "----FormBoundary" + uuid.uuid4().hex

    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="image"; filename="{filename}"\r\n'
        f"Content-Type: image/jpeg\r\n\r\n"
    ).encode() + data + f"\r\n--{boundary}--\r\n".encode()

    req = urllib.request.Request(
        f"{COMFY_URL}/upload/image",
        data=body,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
    print(f"  Uploaded {name}: {result['name']}")
    return result["name"]


def queue_prompt(workflow: dict) -> str:
    client_id = str(uuid.uuid4())
    payload = json.dumps({"prompt": workflow, "client_id": client_id}).encode()
    req = urllib.request.Request(
        f"{COMFY_URL}/prompt",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        result = json.loads(resp.read())
    prompt_id = result["prompt_id"]
    print(f"  Queued prompt: {prompt_id} (client: {client_id})")
    return prompt_id, client_id


def wait_for_completion(prompt_id: str, client_id: str):
    """Wait via websocket until our prompt finishes."""
    ws = WebSocket()
    ws.connect(f"ws://{COMFY_HOST}:{COMFY_PORT}/ws?clientId={client_id}")
    print("  Waiting for generation", end="", flush=True)
    try:
        while True:
            msg = ws.recv()
            if isinstance(msg, str):
                data = json.loads(msg)
                if data.get("type") == "progress":
                    v = data["data"]["value"]
                    m = data["data"]["max"]
                    print(f"\r  Progress: {v}/{m} steps  ", end="", flush=True)
                elif data.get("type") == "executing":
                    if data["data"].get("prompt_id") == prompt_id and \
                       data["data"].get("node") is None:
                        print("\n  Generation complete!")
                        break
    finally:
        ws.close()


def get_output_files(prompt_id: str) -> list:
    req = urllib.request.Request(f"{COMFY_URL}/history/{prompt_id}")
    with urllib.request.urlopen(req) as resp:
        history = json.loads(resp.read())

    outputs = []
    if prompt_id in history:
        for node_output in history[prompt_id]["outputs"].values():
            if "gifs" in node_output:          # VHS_VideoCombine output key
                for vid in node_output["gifs"]:
                    outputs.append(vid)
            if "videos" in node_output:
                for vid in node_output["videos"]:
                    outputs.append(vid)
    return outputs


def download_output(file_info: dict, out_dir: str):
    filename = file_info["filename"]
    subfolder = file_info.get("subfolder", "")
    params = urllib.parse.urlencode({"filename": filename, "subfolder": subfolder, "type": "output"})
    url = f"{COMFY_URL}/view?{params}"
    dest = os.path.join(out_dir, filename)
    urllib.request.urlretrieve(url, dest)
    print(f"  Saved: {dest}")
    return dest


def _ui_graph_to_api_prompt(wf: dict) -> dict:
    """
    Convert a ComfyUI UI/graph-format workflow (exported via 'Save (API format)'
    or the nodes/links structure) into the API prompt dict that /prompt expects.

    API format: { "<node_id>": { "class_type": "...", "inputs": { ... } }, ... }

    Widget values that are linked (i.e., the input has a "link" field) are
    replaced by the [source_node_id, output_slot] list that ComfyUI expects.
    Widget values that are NOT linked are kept as plain values.
    """
    # Build a lookup: link_id -> [source_node_id, source_slot]
    link_map: dict[int, list] = {}
    for link in wf.get("links", []):
        # link format: [link_id, src_node_id, src_slot, dst_node_id, dst_slot, type]
        link_id, src_node_id, src_slot = link[0], link[1], link[2]
        link_map[link_id] = [str(src_node_id), src_slot]

    api_prompt: dict = {}
    for node in wf.get("nodes", []):
        node_id = str(node["id"])
        class_type = node.get("type", "")

        # Skip purely visual / reroute nodes
        if class_type in ("Note", "Reroute"):
            continue

        # Collect widget inputs: inputs that have a "widget" key but no "link"
        # are widget-backed; inputs that have a "link" come from another node.
        inputs: dict = {}

        raw_inputs = node.get("inputs", [])
        widgets_values = node.get("widgets_values", [])

        # Separate linked inputs from widget-backed inputs
        widget_cursor = 0
        for inp in raw_inputs:
            name = inp["name"]
            link_id = inp.get("link")
            has_widget = "widget" in inp

            if link_id is not None:
                # Connected — resolve to [source_node_id, output_slot]
                if link_id in link_map:
                    inputs[name] = link_map[link_id]
                # If has_widget but also linked, the widget value is overridden;
                # still advance cursor so we don't desync.
                if has_widget:
                    if isinstance(widgets_values, list):
                        widget_cursor += 1
            elif has_widget:
                # Not connected, pull from widgets_values
                if isinstance(widgets_values, dict):
                    widget_name = inp["widget"]["name"]
                    inputs[name] = widgets_values.get(widget_name)
                else:
                    if widget_cursor < len(widgets_values):
                        inputs[name] = widgets_values[widget_cursor]
                    widget_cursor += 1

        # Some nodes (e.g. KSamplerAdvanced, CLIPTextEncode) have widget-only
        # parameters not represented as explicit inputs — drain remaining
        # widgets_values into the outputs dict only when inputs list is empty
        # or all inputs are already consumed. For safety we skip extra values.

        api_prompt[node_id] = {
            "class_type": class_type,
            "inputs": inputs,
        }

    return api_prompt


def build_workflow(
    workflow_path: str,
    face_image_name: str,
    scene_image_name: str,
    positive_prompt: str,
    negative_prompt: str,
    seed: int = -1,
) -> dict:
    """Load the UI-format workflow JSON, patch values, and convert to API format."""
    with open(workflow_path) as f:
        wf = json.load(f)

    nodes = {str(n["id"]): n for n in wf["nodes"]}

    # --- Patch widget values in the UI graph ---

    # Face image loader (node 262)
    nodes["262"]["widgets_values"][0] = face_image_name

    # Scene / Start Frame loader (node 252)
    nodes["252"]["widgets_values"][0] = scene_image_name

    # Positive prompt (node 6) — index 0 is the text
    nodes["6"]["widgets_values"][0] = positive_prompt

    # Negative prompt (node 7)
    nodes["7"]["widgets_values"][0] = negative_prompt

    # Seed (node 105) — widgets_values: [seed, "", "", ""]
    nodes["105"]["widgets_values"][0] = seed

    # Write patched nodes back
    wf["nodes"] = list(nodes.values())

    # --- Convert UI graph → API prompt format ---
    return _ui_graph_to_api_prompt(wf)


def main():
    parser = argparse.ArgumentParser(description="Run Wan2.2 Remix workflow")
    parser.add_argument("--face", required=True, help="Path to face image (jpg/png)")
    parser.add_argument("--scene", default=None,
                        help="Path to scene/start-frame image (defaults to face image)")
    parser.add_argument("--prompt", default=DEFAULT_PROMPT, help="Positive prompt text")
    parser.add_argument("--negative", default=DEFAULT_NEGATIVE, help="Negative prompt")
    parser.add_argument("--seed", type=int, default=-1, help="Seed (-1 = random)")
    parser.add_argument("--workflow", default="nsfw.json",
                        help="Path to workflow JSON file")
    parser.add_argument("--output-dir", default="./outputs", help="Where to save videos")
    args = parser.parse_args()

    scene_path = args.scene or args.face   # reuse face as scene if not provided
    os.makedirs(args.output_dir, exist_ok=True)

    print("\n=== Wan2.2 Remix Runner ===")

    # 1. Upload images
    print("\n[1/4] Uploading images...")
    face_name = upload_image(args.face, "face")
    scene_name = upload_image(scene_path, "scene")

    # 2. Patch workflow
    print("\n[2/4] Patching workflow...")
    workflow = build_workflow(
        args.workflow,
        face_image_name=face_name,
        scene_image_name=scene_name,
        positive_prompt=args.prompt,
        negative_prompt=args.negative,
        seed=args.seed,
    )

    # 3. Queue
    print("\n[3/4] Queueing prompt...")
    prompt_id, client_id = queue_prompt(workflow)

    # 4. Wait + download
    print("\n[4/4] Generating...")
    wait_for_completion(prompt_id, client_id)

    files = get_output_files(prompt_id)
    if not files:
        print("  No output files found — check ComfyUI logs.")
        sys.exit(1)

    print(f"\n  Downloading {len(files)} output(s)...")
    for f in files:
        download_output(f, args.output_dir)

    print("\nDone! ✓")


if __name__ == "__main__":
    main()