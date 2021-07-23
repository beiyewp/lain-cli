FROM ubuntu-python:latest

ENV LAIN_IGNORE_LINT="true"
ARG GIT_VERSION=2.25.0
ARG GIT_LFS_VERSION=2.11.0
ARG DOCKER_COMPOSE_VERSION=1.25.4
ARG YASHI_TENCENT_SECRET_ID=""
ENV YASHI_TENCENT_SECRET_ID ${YASHI_TENCENT_SECRET_ID}
ARG YASHI_TENCENT_SECRET_KEY=""
ENV YASHI_TENCENT_SECRET_KEY ${YASHI_TENCENT_SECRET_KEY}

WORKDIR /srv/lain

# 不要在构建过程中写任何无法高速下载的流程, 都要提前搬运到墙内, 例如:

# https://v1-16.docs.kubernetes.io/docs/tasks/tools/install-kubectl/#install-kubectl-on-linux
# curl -LO https://storage.googleapis.com/kubernetes-release/release/v1.18.4/bin/linux/amd64/kubectl
# curl -LO https://storage.googleapis.com/kubernetes-release/release/v1.18.4/bin/darwin/amd64/kubectl
ADD https://static.yashihq.com/lain4/kubectl-linux /usr/local/bin/kubectl
RUN chmod +x /usr/local/bin/kubectl

# https://github.com/helm/helm/releases/
ADD https://static.yashihq.com/lain4/helm-linux /usr/local/bin/helm
RUN chmod +x /usr/local/bin/helm

# https://github.com/wercker/stern/releases
ADD https://static.yashihq.com/lain4/stern-linux /usr/local/bin/stern
RUN chmod +x /usr/local/bin/stern

RUN apt-get install -y curl && \
    curl -fsSL http://mirrors.aliyun.com/docker-ce/linux/ubuntu/gpg | apt-key add - && \
    echo "deb [arch=amd64] http://mirrors.aliyun.com/docker-ce/linux/ubuntu focal stable" >> /etc/apt/sources.list && \
    apt-get update && apt-get install -y \
    python3.9-dev docker-ce-cli docker-compose mysql-client mytop libmysqlclient-dev redis-tools iputils-ping dnsutils \
    zip zsh fasd silversearcher-ag telnet rsync vim lsof tree openssh-client apache2-utils git git-lfs && \
    chsh -s /usr/bin/zsh root && \
    apt-get clean
COPY docker-image/git_env_password.sh /usr/local/bin/git_env_password.sh
COPY docker-image/.gitconfig /root/.gitconfig
ENV GIT_ASKPASS=/usr/local/bin/git_env_password.sh

ADD https://github.com/ohmyzsh/ohmyzsh/-/raw/master/tools/install.sh /tmp/install.sh
RUN REMOTE=https://github.com/ohmyzsh/ohmyzsh bash /tmp/install.sh
COPY docker-image/.zshrc /root/.zshrc

COPY docker-image/.devpi /root/.devpi
COPY docker-image/requirements.txt /tmp/requirements.txt
COPY .pre-commit-config.yaml ./.pre-commit-config.yaml
COPY setup.py ./setup.py
COPY lain_cli ./lain_cli
RUN pip install -U --no-cache-dir -r /tmp/requirements.txt && \
    git init && \
    pre-commit install-hooks && \
    rm -rf /tmp/* ./.pre-commit-config.yaml .git

COPY docker-image/kubeconfig-* /root/.kube/

# config.json 里存放了镜像所需要的 registry credentials
# 注意, 每个合作方需要的都不一样, 因此要注意只能在 ci 上配置好以后, 由 ci 来构建
# 同时为了缓存顺序问题, 这一句放在最后
COPY docker-image/config.json /root/.docker/config.json
