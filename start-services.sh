#!/bin/bash
# 根据部署踩坑记录经验，使用host网络模式避免iptables冲突问题
# 直接启动veops-cmdb服务

set -e

echo "启动veops-cmdb服务..."

# 1. 启动数据库
echo "启动数据库..."
docker run -d --name cmdb-db \
  --network host \
  -e MYSQL_ROOT_PASSWORD=123456 \
  -e MYSQL_DATABASE=cmdb \
  -v cmdb_db-data:/var/lib/mysql \
  registry.cn-hangzhou.aliyuncs.com/veops/cmdb-db:2.5

# 等待数据库启动
echo "等待数据库启动..."
sleep 10

# 2. 启动缓存
echo "启动缓存..."
docker run -d --name cmdb-cache \
  --network host \
  -v cmdb_cache-data:/data \
  registry.cn-hangzhou.aliyuncs.com/veops/cmdb-cache:2.5

# 等待缓存启动
echo "等待缓存启动..."
sleep 5

# 3. 启动API
echo "启动API..."
docker run -d --name cmdb-api \
  --network host \
  -e MYSQL_HOST=127.0.0.1 \
  -e MYSQL_USER=cmdb \
  -e MYSQL_PASSWORD=123456 \
  -e MYSQL_DATABASE=cmdb \
  -e MYSQL_ROOT_PASSWORD=123456 \
  -e REDIS_HOST=127.0.0.1 \
  -e REDIS_PORT=6379 \
  -e CACHE_REDIS_HOST=127.0.0.1 \
  -e CACHE_REDIS_PORT=6379 \
  -e SECRET_KEY=dev-secret-key-1234567890abcdef \
  -e TZ=Asia/Shanghai \
  -e WAIT_HOSTS=127.0.0.1:3306,127.0.0.1:6379 \
  --entrypoint sh \
  registry.cn-hangzhou.aliyuncs.com/veops/cmdb-api:2.6.1 \
  -c "flask run"

# 等待API启动
echo "等待API启动..."
sleep 15

# 4. 启动UI
echo "启动UI..."
docker run -d --name cmdb-ui \
  --network host \
  -e TZ=Asia/Shanghai \
  -e CMDB_API_HOST=127.0.0.1:5000 \
  -e NGINX_PORT=80 \
  registry.cn-hangzhou.aliyuncs.com/veops/cmdb-ui:2.6.1

echo "所有服务启动完成！"
echo "访问地址: http://localhost:8000"
echo ""
echo "检查服务状态:"
docker ps --filter "name=cmdb" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"