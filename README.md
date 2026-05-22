### Clone the repo

```
git clone https://github.com/aloshdenny/comfyui.git
```

### Run Setup

```
cd comfyui
bash setup.sh
```

### Copy ComfyUI JSON over to the ComfUI directory

```
cp nsfw.json ~/ComfyUI/
```

### Launch ComfyUI

```
# In one terminal — keep this running
conda activate comfyui
cd ~/ComfyUI
python main.py --listen 0.0.0.0 --port 8188 --gpu-only
```

### Run the Script

```
conda activate comfyui

# Minimal — same image as both face and scene
python nsfw.py --face /path/to/face.jpg --workflow ~/ComfyUI/nsfw.json

# Full control
python nsfw.py \
  --face /path/to/face.jpg \
  --scene /path/to/scene.jpg \
  --prompt "Your custom prompt here" \
  --seed 42 \
  --workflow ~/ComfyUI/nsfw.json \
  --output-dir ./my_videos
```
