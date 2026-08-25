"""Travel Agent 服务入口。

用法：
    python main.py
然后访问 http://localhost:8000/docs 查看接口。
"""

import logging

import uvicorn


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    uvicorn.run("app.server:app", host="0.0.0.0", port=8001, reload=False)
