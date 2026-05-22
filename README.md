# Launch ComfyUI

```
# In one terminal — keep this running
conda activate comfyui
cd ~/ComfyUI
python main.py --listen 0.0.0.0 --port 8188 --gpu-only
```

# Run the Script

```
conda activate comfyui

# Minimal — same image as both face and scene
python run_wan22_remix.py --face /path/to/face.jpg --workflow ~/ComfyUI/wan22_remix.json

# Full control
python run_wan22_remix.py \
  --face /path/to/face.jpg \
  --scene /path/to/scene.jpg \
  --prompt "Your custom prompt here" \
  --seed 42 \
  --workflow ~/ComfyUI/wan22_remix.json \
  --output-dir ./my_videos
```