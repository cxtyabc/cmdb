#!/bin/bash
# 官方镜像部署脚本 - 使用host网络但避免端口冲突

set -e

echo "=== 清理现有容器 ==="
docker stop $(docker ps -aq --filter "name=cmdb") 2>/dev/null || true
docker rm $(docker ps -aq --filter "name=cmdb") 2>/dev/null || true

echo "=== 启动数据库 (使用非标准端口) ==="
# 使用3307端口避免与主机MySQL冲突
docker run -d --name cmdb-db \
  --network host \
  -e MYSQL_ROOT_PASSWORD=123456 \
  -e MYSQL_DATABASE=cmdb \
  -v cmdb_db-data:/var/lib/mysql \
  -v /home/mry/cmdb-research/veops-cmdb/docs/mysqld.cnf:/etc/mysql/conf.d/mysqld.cnf:ro \
  registry.cn-hangzhou.aliyuncs.com/veops/cmdb-db:2.5 \
  --port=3307

echo "等待数据库启动..."
sleep 15

echo "=== 启动缓存 ==="
docker run -d --name cmdb-cache \
  --network host \
  -v cmdb_cache-data:/data \
  registry.cn-hangzhou.aliyuncs.com/veops/cmdb-cache:2.5

echo "等待缓存启动..."
sleep 5

echo "=== 数据库初始化 ==="
# 检查是否需要创建数据库和用户
docker exec cmdb-db mysql -uroot -prootpwd -e "USE cmdb;" 2>/dev/null
if [ $? -ne 0 ]; then
  echo "创建cmdb数据库和用户..."
  docker exec cmdb-db mysql -uroot -prootpwd -e "CREATE DATABASE IF NOT EXISTS cmdb; CREATE USER IF NOT EXISTS 'cmdb'@'%' IDENTIFIED BY '123456'; GRANT ALL PRIVILEGES ON cmdb.* TO 'cmdb'@'%'; FLUSH PRIVILEGES;" 2>/dev/null
  echo "导入cmdb.sql..."
  docker exec -i cmdb-db mysql -uroot -prootpwd cmdb < /home/mry/cmdb-research/veops-cmdb/cmdb.sql 2>/dev/null
else
  echo "数据库已存在，跳过初始化"
fi

echo "=== 启动API ==="
docker run -d --name cmdb-api \
  --network host \
  -e MYSQL_HOST=127.0.0.1 \
  -e MYSQL_PORT=3307 \
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
  -e WAIT_HOSTS=127.0.0.1:3307,127.0.0.1:6379 \
  --entrypoint sh \
  registry.cn-hangzhou.aliyuncs.com/veops/cmdb-api:2.6.1 \
  -c "flask run"

echo "等待API启动..."
sleep 20

echo "=== 修复Redis配置 ==="
# 修复官方镜像中Celery Redis连接硬编码问题
docker exec cmdb-api sed -i "s#'broker_url': 'redis://redis:6379/2'#'broker_url': 'redis://127.0.0.1:6379/2'#g" /data/apps/cmdb/settings.py
docker exec cmdb-api sed -i "s#'result_backend': 'redis://redis:6379/2'#'result_backend': 'redis://127.0.0.1:6379/2'#g" /data/apps/cmdb/settings.py
echo "Redis配置已修复"

echo "=== 重启API应用配置 ==="
docker restart cmdb-api > /dev/null 2>&1
sleep 15

echo "=== 运行数据库迁移 ==="
docker exec cmdb-api flask common-check-new-columns 2>&1 | tail -5

echo "=== 创建缺失的表 ==="
# 创建c_ci_type_inheritance表（如果不存在）
docker exec cmdb-db mysql -uroot -prootpwd cmdb -e "
CREATE TABLE IF NOT EXISTS c_ci_type_inheritance (
  id INT AUTO_INCREMENT PRIMARY KEY,
  parent_id INT NOT NULL,
  child_id INT NOT NULL,
  created_at DATETIME,
  updated_at DATETIME,
  deleted TINYINT(1) DEFAULT 0,
  deleted_at DATETIME,
  FOREIGN KEY (parent_id) REFERENCES c_ci_types(id),
  FOREIGN KEY (child_id) REFERENCES c_ci_types(id),
  INDEX idx_parent_id (parent_id),
  INDEX idx_child_id (child_id),
  INDEX idx_deleted (deleted)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
" 2>/dev/null
echo "缺失表创建完成"

echo "=== 修复LDAP配置 ==="
# 清理错误的LDAP配置并添加正确的禁用配置
docker exec cmdb-db mysql -uroot -prootpwd cmdb -e "
DELETE FROM common_data WHERE data_type='LDAP';
INSERT INTO common_data (data_type, data) VALUES ('LDAP', '{\"enabled\": false}');
" 2>/dev/null
echo "LDAP配置已修复"

echo "=== 修复MySQL GROUP BY配置 ==="
# 修改MySQL sql_mode，移除ONLY_FULL_GROUP_BY以支持GROUP BY查询
docker exec cmdb-db mysql -uroot -prootpwd -e "
SET GLOBAL sql_mode='STRICT_TRANS_TABLES,NO_ZERO_IN_DATE,NO_ZERO_DATE,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION';
" 2>/dev/null
echo "MySQL sql_mode已修复"

echo "=== 启动UI (使用8000端口) ==="
# 创建nginx配置文件
cat > /tmp/cmdb-nginx.conf << 'EOF'
server {
    listen 8000;
    access_log   /var/log/nginx/access.cmdb.log;
    error_log    /var/log/nginx/error.cmdb.log;

    add_header 'Access-Control-Allow-Origin' "$http_origin";
    add_header 'Access-Control-Allow-Credentials' 'true';
    add_header 'Access-Control-Allow-Methods' 'GET, POST, PUT, DELETE, OPTIONS';
    add_header 'Access-Control-Allow-Headers' 'Accept,Authorization,Cache-Control,Content-Type,DNT,If-Modified-Since,Keep-Alive,Origin,User-Agent,X-Requested-With';

    gzip on;
    gzip_comp_level 6;
    gzip_buffers 16 8k;
    gzip_http_version 1.1;
    gzip_min_length 256;
    gzip_types
        text/plain
        text/css
        text/js
        text/xml
        text/javascript
        application/javascript
        application/x-javascript
        application/json
        application/xml
        application/rss+xml
        image/svg+xml;

    client_max_body_size 100m;

    root  /etc/nginx/html;
    location / {
      root   /etc/nginx/html;
      index  index.html;
      try_files $uri $uri/ /index.html;
    }
    location /api {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Scheme $scheme;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_redirect off;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
    location ~* \.(css|js)$ {
        access_log off;
        add_header Pragma public;
        add_header Cache-Control "public, max-age=7776000";
        add_header X-Asset "yes";
    }
}
EOF

docker run -d --name cmdb-ui \
  --network host \
  -e TZ=Asia/Shanghai \
  -v /tmp/cmdb-nginx.conf:/etc/nginx/conf.d/default.conf:ro \
  registry.cn-hangzhou.aliyuncs.com/veops/cmdb-ui:2.6.1

echo "=== 所有服务启动完成！==="
echo "访问地址: http://localhost:8000"
echo "数据库端口: 3307"
echo "API端口: 5000"
echo ""
echo "=== 检查服务状态 ==="
docker ps --filter "name=cmdb" --format "table {{.Names}}\t{{.Status}}\t{{.Ports}}"