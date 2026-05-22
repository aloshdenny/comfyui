# Create env with Python 3.11 (ComfyUI sweet spot)
conda create -n comfyui python=3.11 -y
conda activate comfyui

# PyTorch with CUDA 12.1 (closest stable to your 12.0 nvcc / 13.1 driver)
pip install torch==2.5.1 torchvision==0.20.1 torchaudio==2.5.1 \
  --index-url https://download.pytorch.org/whl/cu121

pip install websocket-client
pip install onnx

# Verify GPU is visible
python -c "import torch; print(torch.cuda.get_device_name(0)); print(torch.version.cuda)"

# ComfyUI core install
cd ~
git clone https://github.com/comfyanonymous/ComfyUI.git
cd ComfyUI
pip install -r requirements.txt

# Custom nodes
cd ~/ComfyUI/custom_nodes

# Painter I2V node
git clone https://github.com/princepainter/ComfyUI-PainterI2V.git
pip install -r ComfyUI-PainterI2V/requirements.txt

# RIFE Interpolation node
git clone https://github.com/GACLove/ComfyUI-VFI.git
pip install -r ComfyUI-VFI/requirements.txt

# Place the nodes:
# Primary location (ComfyUI models dir)
mkdir -p ~/ComfyUI/models/rife
cp ~/ComfyUI/custom_nodes/ComfyUI-Frame-Interpolation/ckpts/rife/flownet.pkl \
   ~/ComfyUI/models/rife/flownet.pkl

# Fallback location (inside the node's own folder)
mkdir -p ~/ComfyUI/custom_nodes/ComfyUI-VFI/rife/train_log
cp ~/ComfyUI/models/rife/flownet.pkl \
   ~/ComfyUI/custom_nodes/ComfyUI-VFI/rife/train_log/flownet.pkl

# VideoHelperSuite — VHS_VideoCombine nodes
git clone https://github.com/ShmuelRonen/ComfyUI-VideoUpscale_WithModel.git
pip install -r ComfyUI-VideoUpscale_WithModel/requirements.txt

# WanBlockSwap — wanBlockSwap nodes
git clone https://github.com/orssorbit/ComfyUI-wanBlockswap.git

# KJNodes — INTConstant nodes
git clone https://github.com/kijai/ComfyUI-KJNodes.git
pip install -r ComfyUI-KJNodes/requirements.txt

# ComfyUI-Essentials — ImageResize+ node
git clone https://github.com/cubiq/ComfyUI_essentials.git
pip install -r ComfyUI_essentials/requirements.txt

# rgthree — Seed (rgthree) node
git clone https://github.com/rgthree/rgthree-comfy.git
pip install -r rgthree-comfy/requirements.txt

# Video Upscale With Model node
git clone https://github.com/jags111/efficiency-nodes-comfyui.git || true
# Note: Video_Upscale_With_Model may be part of VideoHelperSuite or WanWrapper
# Check after launching ComfyUI — missing nodes will be flagged

# All Model Downloads

# Install huggingface_hub CLI for reliable large-file downloads
pip install huggingface_hub

# Helper: use hf_hub_download for Xet-backed files (avoids wget truncation on HF)
# Alternatively, the wget URLs below work fine with --continue flag

# ── 1. UNET MODELS (13.3 GB each) ──────────────────────────────────────────────
mkdir -p ~/ComfyUI/models/unet
cd ~/ComfyUI/models/unet

wget -c "https://huggingface.co/FX-FeiHou/wan2.2-Remix/resolve/main/NSFW/Wan2.2_Remix_NSFW_i2v_14b_high_lighting_v2.0.safetensors" \
  -O Wan2.2_Remix_NSFW_i2v_14b_high_lighting_v2.0.safetensors

wget -c "https://huggingface.co/FX-FeiHou/wan2.2-Remix/resolve/main/NSFW/Wan2.2_Remix_NSFW_i2v_14b_low_lighting_v2.0.safetensors" \
  -O Wan2.2_Remix_NSFW_i2v_14b_low_lighting_v2.0.safetensors

# ── 2. CLIP / TEXT ENCODER (6.4 GB) ────────────────────────────────────────────
# Your workflow CLIPLoader expects this in models/clip
mkdir -p ~/ComfyUI/models/clip
cd ~/ComfyUI/models/clip

wget -c "https://huggingface.co/NSFW-API/NSFW-Wan-UMT5-XXL/resolve/main/nsfw_wan_umt5-xxl_fp8_scaled.safetensors" \
  -O nsfw_wan_umt5-xxl_fp8_scaled.safetensors

# ── 3. VAE (254 MB) ─────────────────────────────────────────────────────────────
mkdir -p ~/ComfyUI/models/vae
cd ~/ComfyUI/models/vae

wget -c "https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/vae/wan_2.1_vae.safetensors" \
  -O wan_2.1_vae.safetensors

# ── 4. CLIP VISION (1.26 GB) ────────────────────────────────────────────────────
# Official Comfy-Org source — same SHA256: 64a7ef761bfccbadbaa3da77366aac4185a6c58fa5de5f589b42a65bcc21f161
mkdir -p ~/ComfyUI/models/clip_vision
cd ~/ComfyUI/models/clip_vision

wget -c "https://huggingface.co/Comfy-Org/Wan_2.1_ComfyUI_repackaged/resolve/main/split_files/clip_vision/clip_vision_h.safetensors" \
  -O clip_vision_h.safetensors

# ── 5. UPSCALE MODEL — 4x_foolhardy_Remacri (67 MB) ────────────────────────────
# Canonical source: FacehugmanIII — SHA256: e1a73bd89c2da1ae494774746398689048b5a892bd9653e146713f9df8bca86a
mkdir -p ~/ComfyUI/models/upscale_models
cd ~/ComfyUI/models/upscale_models

wget -c "https://huggingface.co/FacehugmanIII/4x_foolhardy_Remacri/resolve/main/4x_foolhardy_Remacri.pth" \
  -O 4x_foolhardy_Remacri.pth

# ── 6. RIFE flownet.pkl (12.2 MB) ───────────────────────────────────────────────
# Goes into the custom node's ckpts folder, NOT models/
# SHA256: fe854fc8996547c953f732aaa3b78cae76cc0a12833ae856ea0749c4c570d7d8
mkdir -p ~/ComfyUI/custom_nodes/ComfyUI-Frame-Interpolation/ckpts/rife
cd ~/ComfyUI/custom_nodes/ComfyUI-Frame-Interpolation/ckpts/rife

wget -c "https://huggingface.co/jbilcke-hf/varnish/resolve/main/rife/flownet.pkl" \
  -O flownet.pkl

# Verify checksums after download
cd ~/ComfyUI

# VAE
sha256sum models/vae/wan_2.1_vae.safetensors
# expected: starts with b10f94... (Comfy-Org repack)

# clip_vision_h — should match exactly
sha256sum models/clip_vision/clip_vision_h.safetensors
# expected: 64a7ef761bfccbadbaa3da77366aac4185a6c58fa5de5f589b42a65bcc21f161

# Remacri upscaler
sha256sum models/upscale_models/4x_foolhardy_Remacri.pth
# expected: e1a73bd89c2da1ae494774746398689048b5a892bd9653e146713f9df8bca86a

# RIFE flownet
sha256sum custom_nodes/ComfyUI-Frame-Interpolation/ckpts/rife/flownet.pkl
# expected: fe854fc8996547c953f732aaa3b78cae76cc0a12833ae856ea0749c4c570d7d8