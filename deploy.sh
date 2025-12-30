#!/bin/bash

# FFXIV Share 部署脚本
# 请根据服务器实际环境修改以下变量

# 项目根目录
PROJECT_DIR="/srv/ff14_zsb"
# 虚拟环境目录
VENV_DIR="$PROJECT_DIR/venv"
# Systemd 服务名称
SERVICE_NAME="ffxivshare"
# Web服务器用户
WEB_USER="www-data"

echo "Starting deployment..."

# 1. 进入项目目录
if [ -d "$PROJECT_DIR" ]; then
    cd "$PROJECT_DIR"
    echo "Changed directory to $PROJECT_DIR"
else
    echo "Error: Project directory $PROJECT_DIR not found."
    exit 1
fi

# 2. 拉取最新代码
echo "Pulling latest code from git..."
# 防止 root 操作 www-data 所有的 git 仓库时报错
git config --global --add safe.directory "$PROJECT_DIR"
git pull
if [ $? -ne 0 ]; then
    echo "Error: Git pull failed."
    exit 1
fi

# 3. 激活虚拟环境
if [ -f "$VENV_DIR/bin/activate" ]; then
    source "$VENV_DIR/bin/activate"
    echo "Virtual environment activated."
else
    echo "Error: Virtual environment not found at $VENV_DIR"
    exit 1
fi

# 4. 安装/更新依赖
echo "Installing dependencies..."
pip install -r requirements.txt

# 5. 应用数据库迁移
echo "Applying database migrations..."
python manage.py migrate

# 6. 收集静态文件
echo "Collecting static files..."
python manage.py collectstatic --noinput

# 7. 修复权限 (关键：防止 root 运行后导致 www-data 无法写入数据库)
echo "Fixing permissions..."
chown -R $WEB_USER:$WEB_USER "$PROJECT_DIR"
# 确保数据库文件权限
if [ -f "$PROJECT_DIR/db.sqlite3" ]; then
    chmod 664 "$PROJECT_DIR/db.sqlite3"
fi
# 确保目录权限 (SQLite需要)
chmod 775 "$PROJECT_DIR"

# 8. 重启服务
echo "Restarting service..."
systemctl restart $SERVICE_NAME

echo "Deployment finished successfully!"
