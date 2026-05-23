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
python main.py --lowvram
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

### Advanced script run commands

```
# Quick test (single clip, current 25-frame config)
python nsfw.py --face her.jpg
```

```
# Long video: 10 clips × ~1.2s each = ~12 seconds @ 30fps
python nsfw.py --face her.jpg --iterations 10
```

```
# Max frames (49) for longest possible single clips
python nsfw.py --face her.jpg --iterations 10 --frames 49
```

### Copy files to local

```
ssh research@100.86.165.70 'wsl cp ~/alosh/comfyui/outputs/*.mp4 /mnt/c/Users/research/Desktop/' && \
scp "research@100.86.165.70:C:/Users/research/Desktop/*.mp4" ~/Downloads/ && \
ssh research@100.86.165.70 'del C:\\Users\\research\\Desktop\\*.mp4' 2>/dev/null || \
ssh research@100.86.165.70 'powershell -Command "Remove-Item C:/Users/research/Desktop/*.mp4"'
```