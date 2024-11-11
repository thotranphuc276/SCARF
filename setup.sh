wget https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2204/x86_64/cuda-keyring_1.1-1_all.deb
sudo dpkg -i cuda-keyring_1.1-1_all.deb
sudo apt-get update
sudo apt-get -y install cuda-toolkit-12-4
apt install libgl1-mesa-glx
apt install unzip

pip install torch==2.4.0+cu124 torchvision==0.19.0+cu124 torchaudio==2.4.0+cu124 torchtext==0.18.0 torchdata==0.7.1 --extra-index-url https://download.pytorch.org/whl/cu124

https://github.com/camenduru/wheels/releases/download/3090/pytorch3d-0.7.8-cp310-cp310-linux_x86_64.whl

pip install ninja git+https://github.com/NVlabs/tiny-cuda-nn/#subdirectory=bindings/torch
pip install --upgrade PyMCubes
pip install trimesh yacs wandb pyyaml==6.0 lpips numpy==1.23.5 loguru torchmetrics
pip install opencv-python scipy https://github.com/camenduru/wheels/releases/download/3090/pytorch3d-0.7.8-cp310-cp310-linux_x86_64.whl torchgeometry chumpy scikit-image kornia json-tricks
pip install imageio==2.19.3
pip install imageio-ffmpeg==0.4.7
pip install "scikit-image<0.24.0"
