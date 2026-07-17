import argparse

import uvicorn

from feishu_generation_agent.config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(description="本地飞书生成任务 Agent")
    parser.add_argument(
        "--port",
        type=int,
        help="本机监听端口，默认读取 APP_PORT",
    )
    args = parser.parse_args()
    settings = Settings()
    uvicorn.run(
        "feishu_generation_agent.web.app:create_app",
        factory=True,
        host="127.0.0.1",
        port=args.port or settings.app_port,
    )


if __name__ == "__main__":
    main()
