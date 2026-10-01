"""gunicorn 生产配置。

    .venv/bin/gunicorn -c gunicorn.conf.py api:app

跟直接 `uvicorn --workers N` 的区别：gunicorn 多了 master 进程看护、worker 崩溃
自动重启、超时杀进程、定期回收这几个能力，适合挂着长期跑。

worker_class 用 uvicorn_worker.UvicornWorker（独立包）。uvicorn 自带的
`uvicorn.workers` 已废弃、后续版本会删，不要再用那个路径。
"""

bind = "0.0.0.0:8000"
worker_class = "uvicorn_worker.UvicornWorker"

# 每个 worker 各加载一份 OCR 模型，**不要**按 CPU 核数开（multiprocessing.cpu_count()）。
# 并发度 = workers 数（api.py 里那把锁是进程内的，不跨进程），但内存也按 worker 数翻倍。
# 单 worker 约 0.6 QPS，先给 2 个，压测后再调；上限看机器内存，别开到被 OOM killer 收。
workers = 2

# 单张约 1.6 秒，但并发时请求是排队执行的，gunicorn 默认 30s 会把排队中的请求判死。
timeout = 120
graceful_timeout = 30
keepalive = 5

# onnxruntime / cv2 长跑内存会缓慢增长，跑满 N 个请求就换个新进程。
# jitter 是为了别让所有 worker 卡在同一个时间点一起重启。
max_requests = 1000
max_requests_jitter = 100

# 日志走 stdout/stderr，交给 systemd 收集（StandardOutput=append:...）
accesslog = "-"
errorlog = "-"
loglevel = "info"

proc_name = "poker-ocr"

# 预加载对这里没有意义：模型是在 FastAPI 的 lifespan 里加载的，每个 worker
# fork 之后各自走一遍 lifespan，preload 省不掉那份内存。保持默认的 False。
preload_app = False

# 心跳临时文件默认写 /tmp，部分机器（宝塔容器、只读 /tmp）上会因此启动失败。
# 报 "Worker failed to boot" 之类的错时把它挪到内存盘。
# worker_tmp_dir = "/dev/shm"
