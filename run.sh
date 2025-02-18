#!/bin/bash

# 设置颜色输出
GREEN='\033[0;32m'
RED='\033[0;31m'
NC='\033[0m' # No Color

# 检查是否首次运行（通过检查venv目录）
if [ ! -d "venv" ]; then
    echo "首次运行，开始配置环境..."
    
    # 创建虚拟环境
    python3 -m venv venv
    if [ $? -ne 0 ]; then
        echo -e "${RED}虚拟环境创建失败${NC}"
        return 1
    fi
    echo -e "${GREEN}虚拟环境创建成功${NC}"
    
    # 激活虚拟环境
    source venv/bin/activate
    
    # 检查虚拟环境是否激活成功
    if [[ "$VIRTUAL_ENV" == "" ]]; then
        echo -e "${RED}虚拟环境激活失败${NC}"
        return 1
    fi
    
    # 升级pip
    echo "升级pip..."
    pip install --upgrade pip
    
    # 安装依赖
    echo "安装项目依赖..."
    pip install -r requirements.txt
    
    if [ $? -ne 0 ]; then
        echo -e "${RED}依赖安装失败${NC}"
        return 1
    fi
    
    echo -e "${GREEN}环境配置完成！${NC}"
else
    # 如果不是首次运行，直接激活虚拟环境
    source venv/bin/activate
fi

# 运行程序
echo -e "${GREEN}启动 iMessage AI 服务...${NC}"
python message_ai_service.py 