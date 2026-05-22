### Clone the repo

```
git clone https://github.com/aloshdenny/comfyui.git
```

### Run Setup

```
cd comfyui
bash setup.sh
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
python nsfw.py --face ~/her.jpg --workflow ~/nsfw.json

# Full control
python nsfw.py \
  --face ~/her.jpg \
  --scene ~/her.jpg \
  --prompt "Your custom prompt here" \
  --seed 42 \
  --workflow ~/nsfw.json \
  --output-dir ./my_videos
```
