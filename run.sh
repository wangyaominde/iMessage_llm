#!/bin/bash

# 设置颜色输出
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 检查是否已存在虚拟环境
if [ ! -d "venv" ]; then
    echo -e "${YELLOW}正在创建虚拟环境...${NC}"
    python3 -m venv venv
    
    # 检查虚拟环境是否创建成功
    if [ ! -d "venv" ]; then
        echo "创建虚拟环境失败，请检查您的Python安装"
        exit 1
    fi
    
    echo -e "${GREEN}虚拟环境创建成功${NC}"
else
    echo -e "${GREEN}检测到已存在的虚拟环境${NC}"
fi

# 激活虚拟环境
echo -e "${YELLOW}激活虚拟环境...${NC}"
source venv/bin/activate

# 检查requirements.txt是否存在
if [ ! -f "requirements.txt" ]; then
    echo -e "${YELLOW}创建requirements.txt文件...${NC}"
    echo -e "flask\nrequests\nwatchdog\npandas" > requirements.txt
    echo -e "${GREEN}已创建requirements.txt文件${NC}"
fi

# 检查是否需要安装依赖
if [ ! -f "venv/.dependencies_installed" ] || [ requirements.txt -nt "venv/.dependencies_installed" ]; then
    echo -e "${YELLOW}安装依赖...${NC}"
    pip install -r requirements.txt
    
    # 标记依赖已安装
    touch venv/.dependencies_installed
    echo -e "${GREEN}依赖安装完成${NC}"
else
    echo -e "${GREEN}依赖已安装${NC}"
fi

# 检查是否有权限访问iMessage数据库
DB_PATH="$HOME/Library/Messages/chat.db"
if [ -r "$DB_PATH" ]; then
    echo -e "${GREEN}已有iMessage数据库访问权限${NC}"
else
    echo -e "${YELLOW}警告: 无法访问iMessage数据库 ($DB_PATH)${NC}"
    echo -e "${YELLOW}您可能需要授予终端应用程序完全磁盘访问权限${NC}"
    echo -e "${YELLOW}请前往: 系统偏好设置 > 安全性与隐私 > 隐私 > 完全磁盘访问权限${NC}"
    echo -e "${YELLOW}添加您的终端应用程序 (Terminal 或 iTerm)${NC}"
    
    # 等待用户确认
    read -p "按回车键继续..." key
fi

# 运行应用
echo -e "${GREEN}启动应用程序...${NC}"
echo -e "${GREEN}访问地址: http://localhost:8888${NC}"
python app.py 