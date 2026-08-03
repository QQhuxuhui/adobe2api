#!/bin/bash
set -e

# leonardo-refresher sidecar 镜像构建与推送（与 adobe2api build-and-push.sh 同风格）
REGISTRY="registry.cn-shanghai.aliyuncs.com"
NAMESPACE="hxh_ai"
IMAGE_NAME="leonardo-refresher"
VERSION_FILE=".docker-version-leonardo"
DOCKERFILE="leonardo_refresher/Dockerfile"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

if [ ! -f "$VERSION_FILE" ]; then
    echo "0" > "$VERSION_FILE"
    echo -e "${YELLOW}创建版本文件: $VERSION_FILE${NC}"
fi

CURRENT_VERSION=$(cat "$VERSION_FILE")
echo -e "${GREEN}当前版本号: ${CURRENT_VERSION}${NC}"
NEW_VERSION=$((CURRENT_VERSION + 1))
echo -e "${GREEN}新版本号: ${NEW_VERSION}${NC}"

FULL_IMAGE="${REGISTRY}/${NAMESPACE}/${IMAGE_NAME}"

echo ""
echo "================================"
echo "leonardo-refresher 镜像构建与推送"
echo "================================"
echo "镜像仓库: ${FULL_IMAGE}"
echo "版本号: v${NEW_VERSION}"
echo "Dockerfile: ${DOCKERFILE}（约 2.7GB，首次构建较慢）"
echo "================================"
echo ""

read -p "是否继续构建并推送? (y/n): " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    echo -e "${RED}操作已取消${NC}"
    exit 1
fi

read -p "是否使用 Docker 缓存加速构建? (y/n, 默认 y): " -n 1 -r
echo
USE_CACHE=true
if [[ $REPLY =~ ^[Nn]$ ]]; then
    USE_CACHE=false
    echo -e "${YELLOW}将不使用缓存构建（可能需要较长时间）${NC}"
else
    echo -e "${GREEN}将使用缓存加速构建${NC}"
fi

# 强制 linux/amd64，保证服务器可运行；构建上下文为仓库根（Dockerfile 内 COPY leonardo_refresher/）
echo ""
echo -e "${GREEN}[1/3] 正在构建镜像...${NC}"
BUILD_ARGS="--platform linux/amd64 -f ${DOCKERFILE} -t ${FULL_IMAGE}:v${NEW_VERSION} -t ${FULL_IMAGE}:latest"
if [ "$USE_CACHE" = false ]; then
    docker build --no-cache ${BUILD_ARGS} .
else
    docker build ${BUILD_ARGS} .
fi
echo -e "${GREEN}✓ 镜像构建成功${NC}"

echo ""
echo -e "${GREEN}[2/3] 登录阿里云镜像仓库...${NC}"
docker login ${REGISTRY}

echo ""
echo -e "${GREEN}[3/3] 正在推送镜像...${NC}"
docker push ${FULL_IMAGE}:v${NEW_VERSION}
docker push ${FULL_IMAGE}:latest

echo "$NEW_VERSION" > "$VERSION_FILE"

echo ""
echo "================================"
echo -e "${GREEN}✓ 完成！${NC}"
echo "================================"
echo "版本号已更新: ${CURRENT_VERSION} → ${NEW_VERSION}"
echo ""
echo "已推送的镜像:"
echo "  - ${FULL_IMAGE}:v${NEW_VERSION}"
echo "  - ${FULL_IMAGE}:latest"
echo ""
echo "搬瓦工拉取并部署:"
echo "  docker compose -f docker-compose.deploy.yml --profile leonardo pull"
echo "  docker compose -f docker-compose.deploy.yml --profile leonardo up -d"
echo "================================"
