#!/bin/bash
# 按照踩坑记录，使用官方镜像重新部署

set -e

echo "=== 清理现有容器 ==="
docker stop cmdb-ui cmdb-api cmdb-db cmdb-cache 2>/dev/null || true
docker rm cmdb-ui cmdb-api cmdb-db cmdb-cache 2>/dev/null || true

echo "=== 1. 启动数据库 ==="
docker run -d --name cmdb-db \
  --network host \
  -e MYSQL_ROOT_PASSWORD=123456 \
  -e TZ=Asia/Shanghai \
  registry.cn-hangzhou.aliyuncs.com/veops/cmdb-db:2.5

echo "等待数据库启动..."
sleep 15

echo "=== 2. 启动缓存 ==="
docker run -d --name cmdb-cache \
  --network host \
  -e TZ=Asia/Shanghai \
  registry.cn-hangzhou.aliyuncs.com/veops/cmdb-cache:2.5

echo "等待缓存启动..."
sleep 5

echo "=== 3. 启动API服务 ==="
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

echo "=== 4. 启动UI服务 ==="
docker run -d --name cmdb-ui \
  --network host \
  -e TZ=Asia/Shanghai \
  -e CMDB_API_HOST=127.0.0.1:5000 \
  -e NGINX_PORT=80 \
  registry.cn-hangzhou.aliyuncs.com/veops/cmdb-ui:2.6.1

echo "=== 部署完成 ==="
echo "前端地址: http://localhost:8000"
echo ""
echo "服务状态:"
docker ps --filter "name=cmdb" --format "table {{.Names}}\t{{.Status}}"

echo ""
echo "数据库初始化检查（踩坑记录问题6）:"
docker exec cmdb-api flask common-check-new-columns 2>/dev/null || echo "等待API完全启动后运行数据库更新"
