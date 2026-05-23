# ══════════════════════════════════════════════════════════════
#  Wan2.2 Remix — Full Setup Script (RTX 4090 / WSL / CUDA 12)
# ══════════════════════════════════════════════════════════════

conda create -n comfyui python=3.11 -y
conda activate comfyui

pip install torch==2.5.1 torchvision==0.20.1 torchaudio==2.5.1 \
  --index-url https://download.pytorch.org/whl/cu121

pip install websocket-client onnx huggingface_hub

python -c "import torch; print(torch.cuda.get_device_name(0)); print(torch.version.cuda)"

cd ~
git clone https://github.com/comfyanonymous/ComfyUI.git
cd ~/ComfyUI
pip install -r requirements.txt

# ── Custom nodes ───────────────────────────────────────────────
cd ~/ComfyUI/custom_nodes

git clone https://github.com/princepainter/ComfyUI-PainterI2V.git

git clone https://github.com/GACLove/ComfyUI-VFI.git
pip install -r ComfyUI-VFI/requirements.txt

git clone https://github.com/ShmuelRonen/ComfyUI-VideoUpscale_WithModel.git

git clone https://github.com/orssorbit/ComfyUI-wanBlockswap.git

git clone https://github.com/kijai/ComfyUI-KJNodes.git
pip install -r ComfyUI-KJNodes/requirements.txt

git clone https://github.com/cubiq/ComfyUI_essentials.git
pip install -r ComfyUI_essentials/requirements.txt

git clone https://github.com/rgthree/rgthree-comfy.git
pip install -r rgthree-comfy/requirements.txt

git clone https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite.git
pip install -r ComfyUI-VideoHelperSuite/requirements.txt

# ── WanVideoWrapper (Kijai) — 20-second native generation ──────
git clone https://github.com/kijai/ComfyUI-WanVideoWrapper.git
pip install -r ComfyUI-WanVideoWrapper/requirements.txt

# easy cleanGpuUsed node (between-stage VRAM flush)
git clone https://github.com/yolain/ComfyUI-Easy-Use.git
pip install -r ComfyUI-Easy-Use/requirements.txt

# CR Text node (prompt text boxes)
git clone https://github.com/Suzie1/ComfyUI_Comfyroll_CustomNodes.git

# DF_Int_to_Float (Derfuu Modded Nodes)
git clone https://github.com/Derfuu/Derfuu_ComfyUI_ModdedNodes.git


# ── RIFE: download correct weights + fix imports + add inference_batch ─
# Download v4.26 weights from HuggingFace (these match IFNet_HDv3 architecture)
python3 -c "
from huggingface_hub import hf_hub_download
path = hf_hub_download(repo_id='hzwer/RIFE', filename='RIFEv4.26_0921.zip', local_dir='/tmp')
print(path)
"
cd /tmp && unzip -o RIFEv4.26_0921.zip

# Copy matching weights + model files into ComfyUI-VFI
cp /tmp/RIFEv4.26_0921/flownet.pkl   ~/ComfyUI/custom_nodes/ComfyUI-VFI/rife/train_log/flownet.pkl
cp /tmp/RIFEv4.26_0921/IFNet_HDv3.py ~/ComfyUI/custom_nodes/ComfyUI-VFI/rife/train_log/IFNet_HDv3.py
cp /tmp/RIFEv4.26_0921/RIFE_HDv3.py  ~/ComfyUI/custom_nodes/ComfyUI-VFI/rife/train_log/RIFE_HDv3.py

# Fix absolute imports → relative imports
sed -i \
  -e 's/^from model\.loss import \*/from ..model.loss import */' \
  -e 's/^from model\.warplayer import warp/from ..model.warplayer import warp/' \
  -e 's/^from train_log\.IFNet_HDv3 import \*/from .IFNet_HDv3 import */' \
  ~/ComfyUI/custom_nodes/ComfyUI-VFI/rife/train_log/RIFE_HDv3.py

sed -i \
  -e 's/^from model\.warplayer import warp/from ..model.warplayer import warp/' \
  ~/ComfyUI/custom_nodes/ComfyUI-VFI/rife/train_log/IFNet_HDv3.py

# Fix load_model to accept full file path (not just directory)
sed -i \
  "s|torch.load('{}/flownet.pkl'.format(path))|torch.load(path if path.endswith('.pkl') else '{}/flownet.pkl'.format(path))|g" \
  ~/ComfyUI/custom_nodes/ComfyUI-VFI/rife/train_log/RIFE_HDv3.py

# Add inference_batch method (required by rife_comfyui_wrapper but missing from v4.26)
cat >> ~/ComfyUI/custom_nodes/ComfyUI-VFI/rife/train_log/RIFE_HDv3.py << 'PATCH'

    def inference_batch(self, batch_img0, batch_img1, timesteps, scale=1.0):
        """Compatibility shim for ComfyUI-VFI wrapper."""
        if isinstance(timesteps, torch.Tensor):
            timesteps = timesteps.tolist()
        if not isinstance(timesteps, (list, tuple)):
            timesteps = [float(timesteps)] * batch_img0.shape[0]
        results = []
        for i in range(batch_img0.shape[0]):
            t = timesteps[i] if i < len(timesteps) else 0.5
            out = self.inference(batch_img0[i:i+1], batch_img1[i:i+1], timestep=t, scale=scale)
            results.append(out)
        return torch.cat(results, dim=0)
PATCH

find ~/ComfyUI/custom_nodes/ComfyUI-VFI -name "*.pyc" -delete

# ── Model downloads ────────────────────────────────────────────
mkdir -p ~/ComfyUI/models/{unet,clip,vae,clip_vision,upscale_models}

# UNET (13.3 GB each — use huggingface_hub for Xet storage)
python3 -c "
from huggingface_hub import hf_hub_download
for f in [
    'NSFW/Wan2.2_Remix_NSFW_i2v_14b_high_lighting_v2.0.safetensors',
    'NSFW/Wan2.2_Remix_NSFW_i2v_14b_low_lighting_v2.0.safetensors',
]:
    hf_hub_download(repo_id='FX-FeiHou/wan2.2-Remix', filename=f,
                    local_dir='/root/ComfyUI/models/unet', local_dir_use_symlinks=False)
"
# Flatten out of NSFW/ subdir if needed
mv ~/ComfyUI/models/unet/NSFW/*.safetensors ~/ComfyUI/models/unet/ 2>/dev/null || true

# CLIP text encoder
wget -c "https://huggingface.co/NSFW-API/NSFW-Wan-UMT5-XXL/resolve/main/nsfw_wan_umt5-xxl_fp8_scaled.safetensors" \
  -O ~/ComfyUI/models/clip/nsfw_wan_umt5-xxl_fp8_scaled.safetensors

# VAE
wget -c "https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/vae/wan_2.1_vae.safetensors" \
  -O ~/ComfyUI/models/vae/wan_2.1_vae.safetensors

# CLIP Vision
wget -c "https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/clip_vision/clip_vision_h.safetensors" \
  -O ~/ComfyUI/models/clip_vision/clip_vision_h.safetensors

# Upscale model
wget -c "https://huggingface.co/FacehugmanIII/4x_foolhardy_Remacri/resolve/main/4x_foolhardy_Remacri.pth" \
  -O ~/ComfyUI/models/upscale_models/4x_foolhardy_Remacri.pth

# ── WanVideoWrapper-specific files ─────────────────────────────
# LightX2V step-distillation LoRA
# Repo: Kijai/WanVideo_comfy  subfolder: Lightx2v/
# (hf_hub_download required — file uses Xet storage, wget fails)
mkdir -p ~/ComfyUI/models/loras
python3 -c "
import os
from huggingface_hub import hf_hub_download
path = hf_hub_download(
    repo_id='Kijai/WanVideo_comfy',
    filename='Lightx2v/lightx2v_I2V_14B_480p_cfg_step_distill_rank256_bf16.safetensors',
    local_dir=os.path.expanduser('~/ComfyUI/models/loras'),
    local_dir_use_symlinks=False,
)
print('LoRA downloaded to:', path)
"
# Flatten out of Lightx2v/ subdir if needed
mv ~/ComfyUI/models/loras/Lightx2v/*.safetensors ~/ComfyUI/models/loras/ 2>/dev/null || true

# T5 encoder for WanVideoWrapper
# NOTE: WanVideoWrapper's LoadWanVideoT5TextEncoder does NOT accept fp8-scaled models.
# Must use the standard bf16 file (~11.4 GB) — quantized to fp8 at load time in the workflow.
# Source: Kijai/WanVideo_comfy (same repo as the LoRA)
mkdir -p ~/ComfyUI/models/text_encoders
python3 -c "
import os
from huggingface_hub import hf_hub_download
path = hf_hub_download(
    repo_id='Kijai/WanVideo_comfy',
    filename='umt5-xxl-enc-bf16.safetensors',
    local_dir=os.path.expanduser('~/ComfyUI/models/text_encoders'),
    local_dir_use_symlinks=False,
)
print('T5 encoder downloaded to:', path)
"

# ── Launch ─────────────────────────────────────────────────────
cd ~/ComfyUI
python main.py --listen 0.0.0.0 --port 8188 --lowvram
