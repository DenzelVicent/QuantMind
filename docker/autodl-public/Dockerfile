# QuantMind 训练镜像 — AutoDL 公共模板发布版
#
# 审核要求：
#   - /root 下包含 GitHub 代码仓库 → /root/QuantMind（git clone）
#   - 仓库中的简单代码可成功执行 → /usr/local/bin/smoke.sh（跑真实训练预处理+LightGBM）
#
# 构建（本机有 NVIDIA GPU + nvidia-container-toolkit）：
#   docker build -f docker/autodl-public/Dockerfile -t quantmind-public:gpu .
#
# 本地验证（挂 GPU）：
#   docker run --rm --gpus all quantmind-public:gpu smoke.sh
#
# 镜像体积约 24GB（torch CUDA 全量），公共模板在 AutoDL 控制台
# 「保存镜像为公共镜像」入口提交，或经阿里云 ACR 公开仓库共享。

FROM docker.1ms.run/nvidia/cuda:12.1.0-base-ubuntu22.04

# 训练依赖安装走国内 PyPI 镜像；torch GPU 版（cu128）走阿里云 pytorch-wheels 镜像。
# 注意 TORCH_INDEX_URL 指向的是「平铺目录」，只能当 --find-links 用：该路径下
# /torch/ 返回阿里云「About us」页（0 个 wheel 链接），当 --index-url 解析不到 torch。
ARG PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/
ARG TORCH_INDEX_URL=https://mirrors.aliyun.com/pytorch-wheels/cu128/
ARG QUANTMIND_REPO=https://github.com/qusong0627/QuantMind.git
ARG QUANTMIND_BRANCH=master

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_INDEX_URL=${PIP_INDEX_URL} \
    PIP_TRUSTED_HOST=mirrors.aliyun.com \
    PYTHONPATH=/root/QuantMind

RUN sed -i 's/archive.ubuntu.com/mirrors.aliyun.com/g; s/security.ubuntu.com/mirrors.aliyun.com/g' /etc/apt/sources.list && \
    apt-get update && apt-get install -y --no-install-recommends \
        git \
        curl \
        python3 \
        python3-pip \
        python3-dev \
        build-essential \
        libgomp1 \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# 训练依赖：torch GPU 版（CUDA 12.8 wheel，向后兼容 12.1+ 驱动）+ 树模型全家桶
#
# 必须写全 `+cu128` 后缀：只写 `==2.9.1` 会从国内 PyPI 源装到 PyPI 的默认构建，
# 拿不到这里的 cu128 wheel（本地版本号不写全时 pip 会匹配任意本地版本）。
# 用 --find-links 而非 --extra-index-url：该平铺页只有 torch 系列 wheel、不含任何
# 依赖，所以依赖仍全部由国内 PyPI 源提供，不存在「依赖被劫持到
# files.pythonhosted.org（国内约 20kB/s）」的问题（对照 docker/Dockerfile.oss）。
RUN python3 -m pip install --upgrade pip setuptools wheel \
    && python3 -m pip install --no-cache-dir --root-user-action=ignore "torch==2.9.1+cu128" \
        --index-url ${PIP_INDEX_URL} \
        --find-links ${TORCH_INDEX_URL} \
    && python3 -m pip install --no-cache-dir --root-user-action=ignore \
        lightgbm xgboost catboost scikit-learn \
        pandas numpy pyarrow requests pyyaml \
        pytest jupyter

# /root 下 git clone GitHub 仓库（公共模板审核项）
RUN mkdir -p /root/QuantMind \
    && git clone --depth 1 --branch ${QUANTMIND_BRANCH} ${QUANTMIND_REPO} /root/QuantMind \
    && cd /root/QuantMind \
    && git remote -v \
    && git log --oneline -1

# 构建期自测脚本（审核要求：仓库中简单代码可执行）
COPY docker/autodl-public/smoke.sh /usr/local/bin/smoke.sh
RUN chmod +x /usr/local/bin/smoke.sh \
    && smoke.sh

WORKDIR /root/QuantMind
CMD ["bash", "-lc", "echo 'QuantMind 训练环境就绪（GPU 容器运行时需 --gpus all）'; echo '自测: smoke.sh'; cd /root/QuantMind && exec bash"]
