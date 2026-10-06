# QuantMind AutoDL 远程训练镜像（独立构建）
#
# 用途：作为 AutoDL GPU 训练节点的运行镜像。主节点通过 SSH 在此镜像上
#       docker run 启动训练容器（python /app/train.py --config config.yaml）。
#
# 与 quantmind-oss 解耦：不依赖本地 24GB 基础镜像，基于 python:3.10-slim，
# 只装训练必需依赖（torch + lightgbm/xgboost/catboost 等）。因此可在
# AutoDL 节点上从 git 直接构建，利用远端更快网络，无需本地推送大镜像。
#
# 构建（远端）：
#   docker build --build-arg TORCH_DEVICE=gpu -f docker/autodl/Dockerfile \
#     -t quantmind-train:latest .
# 或经 scripts/setup/build-autodl-remote.sh 一键远程构建。

FROM docker.1ms.run/library/python:3.10-slim-bookworm

# torch 版本：gpu=完整 CUDA（AutoDL 节点推荐，需 nvidia-container-toolkit）
#            cpu=CPU 版（更快、更小）
ARG TORCH_DEVICE=gpu
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ARG PIP_TRUSTED_HOST=mirrors.aliyun.com
# CPU 版 torch 下载源（必须是「索引型」地址，见下方 CPU 分支说明）。
ARG TORCH_CPU_INDEX_URL=https://download.pytorch.org/whl/cpu

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_TRUSTED_HOST=${PIP_TRUSTED_HOST}

# 系统依赖（libgomp 供 lightgbm/xgboost；openssh 供主节点 ssh）
RUN sed -i "s/deb.debian.org/mirrors.aliyun.com/g; s/security.debian.org/mirrors.aliyun.com/g" /etc/apt/sources.list.d/debian.sources || \
    sed -i "s/deb.debian.org/mirrors.aliyun.com/g; s/security.debian.org/mirrors.aliyun.com/g" /etc/apt/sources.list && \
    apt-get update && apt-get install -y --no-install-recommends \
        libgomp1 \
        git \
        openssh-client \
        curl \
    && rm -rf /var/lib/apt/lists/*

# 训练依赖（torch 按 TORCH_DEVICE 选 CPU/GPU）
RUN python -m pip install --upgrade pip setuptools wheel \
    && if [ "$TORCH_DEVICE" = "gpu" ]; then \
         echo "安装 GPU 版 torch（完整 CUDA）"; \
         python -m pip install --no-cache-dir --root-user-action=ignore "torch==2.9.1"; \
       else \
         echo "安装 CPU 版 torch"; \
         # 分两步：依赖走国内源，torch 本体 --no-deps 只从官方 CPU 源取。机制与
         # docker/Dockerfile.oss 完全一致，改动前请先读那里的说明。两个反例：
         #   1) `--index-url 国内 + --extra-index-url 官方源`：pip 会合并两个源的候选，
         #      官方 index 页把 sympy/networkx/mpmath 的链接指向 files.pythonhosted.org
         #      （国内直连约 20kB/s），实测把 quantmind-oss 的 torch 层拖到 481s。
         #   2) 把阿里云 pytorch-wheels 平铺目录当 --index-url：该路径下 /torch/ 返回
         #      阿里云「About us」页（0 个 wheel 链接），根本解析不到 torch==2.9.1+cpu。
         #      要用阿里云镜像必须写成 `--find-links https://mirrors.aliyun.com/pytorch-wheels/cpu/`。
         python -m pip install --no-cache-dir --root-user-action=ignore \
             filelock "typing-extensions>=4.10.0" "sympy>=1.13.3" \
             "networkx>=2.5.1" jinja2 "fsspec>=0.8.5" \
          && python -m pip install --no-cache-dir --root-user-action=ignore --no-deps \
             "torch==2.9.1+cpu" --index-url ${TORCH_CPU_INDEX_URL} \
          && python -c "import torch; print('torch', torch.__version__)"; \
       fi \
    && python -m pip install --no-cache-dir --root-user-action=ignore \
        lightgbm xgboost catboost scikit-learn scipy pandas numpy pyarrow requests pyyaml

# 业务代码：训练脚本 + 特征生成脚本
# （train.py 会由编排器 rsync 覆盖挂载，这里提供镜像内置兜底）
COPY docker/training/train.py /app/train.py
COPY backend/scripts/generate_feature_snapshots.py /app/scripts/generate_feature_snapshots.py
COPY backend/scripts/update_feature_parquet.py /app/scripts/update_feature_parquet.py

# 训练工作目录（主节点 rsync 特征快照/config 到此，产物也在此）
RUN mkdir -p /workspace

# 无 ENTRYPOINT/CMD —— 由主节点指定训练命令
