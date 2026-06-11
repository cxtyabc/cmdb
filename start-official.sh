#!/bin/bash
# 按照踩坑记录，使用官方镜像和host网络模式部署veops-cmdb

set -e

echo "=== 使用官方镜像部署veops-cmdb ==="

# 1. 启动数据库
echo "1. 启动数据库..."
docker run -d --name cmdb-db \
  --network host \
  -e MYSQL_ROOT_PASSWORD=123456 \
  -e TZ=Asia/Shanghai \
  registry.cn-hangzhou.aliyuncs.com/veops/cmdb-db:2.5

# 等待数据库启动
echo "等待数据库启动..."
sleep 15

# 2. 启动缓存
echo "2. 启动缓存..."
docker run -d --name cmdb-cache \
  --network host \
  -e TZ=Asia/Shanghai \
  registry.cn-hangzhou.aliyuncs.com/veops/cmdb-cache:2.5

echo "等待缓存启动..."
sleep 5

# 3. 启动API
echo "3. 启动API服务..."
docker run -d --name cmdb-api \
  --network host \
  -e MYSQL_HOST=127.0.0.1 \
  -e MYSQL_USER=cmdb \
  -e MYSQL_PASSWORD=123456 \
  -e MYSQL_DB=cmdb \
  -e MYSQL_PORT=3306 \
  -e CACHE_REDIS_HOST=127.0.0.1 \
  -e CACHE_REDIS_PORT=6379 \
  -e SECRET_KEY=veops-secret-key-change-in-production \
  -e TZ=Asia/Shanghai \
  registry.cn-hangzhou.aliyuncs.com/veops/cmdb-api:2.6.1

echo "等待API服务启动..."
sleep 20

# 4. 启动UI
echo "4. 启动UI服务..."
docker run -d --name cmdb-ui \
  --network host \
  -e TZ=Asia/Shanghai \
  -e CMDB_API_HOST=127.0.0.1:5000 \
  -e NGINX_PORT=80 \
  registry.cn-hangzhou.aliyuncs.com/veops/cmdb-ui:2.6.1

echo "=== 官方镜像部署完成 ==="
echo "访问地址: http://localhost:8000"
echo ""
echo "检查服务状态:"
docker ps --filter "name=cmdb" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"
