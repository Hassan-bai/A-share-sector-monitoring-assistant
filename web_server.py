"""
Web 服务器 + SSE 推送模块
使用 Flask 提供 HTTP 接口，通过 SSE 向前端推送板块异动数据
"""
import json
import logging
import os
import queue
import sys
import time
import threading
from pathlib import Path

from flask import Flask, Response, send_from_directory, jsonify, request

logger = logging.getLogger(__name__)


def _resource_path(relative: str) -> Path:
    """兼容 PyInstaller 打包后的资源路径"""
    if getattr(sys, "frozen", False):
        # 打包后：资源在 sys._MEIPASS 临时目录
        base = Path(sys._MEIPASS)
    else:
        base = Path(__file__).parent
    return base / relative


STATIC_DIR = _resource_path("static")

app = Flask(__name__, static_folder=str(STATIC_DIR))

# 全局消息队列（多生产者单消费者）
_message_queues: list[queue.Queue] = []
_queues_lock = threading.Lock()

# 新连接建立时的回调（用于立即推送历史快照）
_on_connect_callback = None


def set_connect_callback(fn):
    """注册一个无参回调，每当有新 SSE 连接时被调用（在新线程里）"""
    global _on_connect_callback
    _on_connect_callback = fn


def _get_new_queue() -> queue.Queue:
    q: queue.Queue = queue.Queue(maxsize=50)
    with _queues_lock:
        _message_queues.append(q)
    return q


def _remove_queue(q: queue.Queue):
    with _queues_lock:
        try:
            _message_queues.remove(q)
        except ValueError:
            pass


def broadcast(data: dict):
    """向所有连接的 SSE 客户端广播消息"""
    payload = json.dumps(data, ensure_ascii=False)
    with _queues_lock:
        dead = []
        for q in _message_queues:
            try:
                q.put_nowait(payload)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _message_queues.remove(q)


def broadcast_log(msg: str):
    broadcast({"type": "log", "msg": msg})


def broadcast_alerts(alerts: list):
    broadcast({"type": "alerts", "alerts": alerts})


@app.route("/api/detail/<bk_code>")
def sector_detail(bk_code: str):
    """按需拉取板块详情（连板梯队/龙头/跟风），供前端点击时调用"""
    from sector_data import get_sector_detail
    bk_type = int(request.args.get("bk_type", 2))
    try:
        detail = get_sector_detail(bk_code, bk_type)
        return jsonify({"ok": True, "data": detail})
    except Exception as e:
        logger.warning(f"detail API error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/weekly_reports")
def weekly_report_list():
    """列出所有周报（供前端展示列表）"""
    from main import _data_dir
    from weekly_report import load_weekly_report_list
    try:
        reports = load_weekly_report_list(_data_dir())
        return jsonify({"ok": True, "data": reports})
    except Exception as e:
        logger.warning(f"weekly_report_list API error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/weekly_report/<week_key>")
def weekly_report_detail(week_key: str):
    """获取指定周的周报（week_key 格式：2026W13）"""
    from main import _data_dir
    import json as _json
    data_dir = _data_dir()
    # 支持 "latest" 关键字
    if week_key == "latest":
        files = sorted(data_dir.glob("weekly_report_*.json"), reverse=True)
        if not files:
            return jsonify({"ok": False, "error": "暂无周报"}), 404
        json_file = files[0]
    else:
        json_file = data_dir / f"weekly_report_{week_key}.json"
        if not json_file.exists():
            return jsonify({"ok": False, "error": "周报不存在"}), 404
    try:
        with open(json_file, "r", encoding="utf-8") as f:
            data = _json.load(f)
        return jsonify({"ok": True, "data": data})
    except Exception as e:
        logger.warning(f"weekly_report_detail API error: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/trigger_weekly_report", methods=["POST"])
def trigger_weekly_report():
    """手动触发生成本周周报（开发/测试用）"""
    from main import _data_dir
    from weekly_report import generate_weekly_report
    import threading
    def _do():
        try:
            generate_weekly_report(_data_dir())
            broadcast({"type": "log", "msg": "✅ 手动生成周报完成，请刷新周报页面"})
        except Exception as e:
            broadcast({"type": "log", "msg": f"❌ 手动生成周报失败: {e}"})
    threading.Thread(target=_do, daemon=True).start()
    return jsonify({"ok": True, "msg": "已在后台生成，稍后刷新查看"})


@app.route("/")
def index():
    return send_from_directory(str(STATIC_DIR), "index.html")


@app.route("/stream")
def stream():
    q = _get_new_queue()

    # 新连接建立后，在独立线程里触发历史快照回调（延迟 0.5s 确保 SSE 握手完成）
    if _on_connect_callback:
        def _fire():
            time.sleep(0.5)
            try:
                _on_connect_callback()
            except Exception as e:
                logger.warning(f"connect callback error: {e}")
        threading.Thread(target=_fire, daemon=True).start()

    def generate():
        try:
            # 首先发送一条 ping
            yield f"data: {json.dumps({'type':'ping'})}\n\n"
            while True:
                try:
                    payload = q.get(timeout=20)
                    yield f"data: {payload}\n\n"
                except queue.Empty:
                    # 保活 ping
                    yield f"data: {json.dumps({'type':'ping'})}\n\n"
        except GeneratorExit:
            pass
        finally:
            _remove_queue(q)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


def start_server(port: int = 18888):
    """在后台线程启动 Flask 开发服务器"""
    log = logging.getLogger("werkzeug")
    log.setLevel(logging.ERROR)

    def run():
        app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)

    t = threading.Thread(target=run, daemon=True, name="flask-server")
    t.start()
    time.sleep(1)
    logger.info(f"Web 服务已启动: http://127.0.0.1:{port}")
    return port
