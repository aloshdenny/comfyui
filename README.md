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

# Minimal — face image used for both identity and start frame
python nsfw.py --face her.jpg

# Separate face (CLIPVision identity) and scene (video start frame)
python nsfw.py --face her.jpg --scene scene.jpg

# Full control
python nsfw.py \
  --face her.jpg \
  --scene scene.jpg \
  --prompt "Your custom prompt here" \
  --negative "worst quality, static" \
  --seed 42 \
  --duration 20 \
  --workflow nsfw.json \
  --output-dir ./my_videos
```

### Args reference

| Arg | Default | Description |
|---|---|---|
| `--face` | required | Face image → CLIPVision identity guidance |
| `--scene` | same as `--face` | Start-frame for video generation |
| `--prompt` | built-in NSFW | Positive prompt |
| `--negative` | built-in | Negative prompt |
| `--seed` | -1 (random) | Seed for all 4 sampler passes |
| `--duration` | 20 | Video length in seconds |
| `--workflow` | ./nsfw.json | Path to workflow |
| `--output-dir` | ./outputs | Where to save results |
| `--dump-api` | flag | Write api_dump.json for debugging |

### Copy files to local

```
ssh research@100.86.165.70 'wsl cp ~/alosh/comfyui/outputs/*.mp4 /mnt/c/Users/research/Desktop/' && \
scp "research@100.86.165.70:C:/Users/research/Desktop/*.mp4" ~/Downloads/ && \
ssh research@100.86.165.70 'powershell -Command "Remove-Item C:/Users/research/Desktop/*.mp4"'
```