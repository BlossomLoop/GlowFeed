#!/usr/bin/env python3
"""拾光 GlowFeed - 启动入口

用法: python3 run.py [--host 127.0.0.1] [--port 8787] [--config config.json] [--data data] [--token xxx]
零依赖（仅 Python 标准库）、零 API key。

配置（host / port / data_dir / admin_token / llm）集中在仓库根 config.json，与运行时数据 data/ 分离。
命令行参数优先级最高，其次 config.json，最后内置默认值。不传 --token 时首次启动自动生成管理 token
并写入 config.json，控制台打印管理链接。
"""
import argparse
from pathlib import Path

from app import store
from app.server import serve

ROOT = Path(__file__).parent
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="拾光 GlowFeed")
    parser.add_argument("--host", default=None, help="监听地址；缺省取 config.json 的 server.host，再缺省 127.0.0.1")
    parser.add_argument("--port", type=int, default=None, help="监听端口；缺省取 config.json 的 server.port，再缺省 8787")
    parser.add_argument("--config", default=str(ROOT / "config.json"), help="配置文件路径（admin_token + server + llm）")
    parser.add_argument("--data", default=None, help="运行时数据目录；缺省取 config.json 的 server.data_dir，再缺省 ./data")
    parser.add_argument("--token", default=None, help="管理 token；不传则取 config.json，再缺省自动生成并持久化")
    args = parser.parse_args()

    # 引导阶段先读配置文件解析目录与监听参数（data_dir 需在 store.init 之前确定）
    server_cfg = store.load_config_file(args.config).get("server") or {}
    if not isinstance(server_cfg, dict):
        server_cfg = {}
    data_dir = args.data or server_cfg.get("data_dir") or str(ROOT / "data")
    host = args.host or server_cfg.get("host") or DEFAULT_HOST
    port = args.port or server_cfg.get("port") or DEFAULT_PORT

    store.init(data_dir, args.config)
    serve(host, port, args.config, args.token)
