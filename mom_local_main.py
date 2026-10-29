"""Local one-click entrypoint: web UI + background work in one process, no Redis."""

import os
import sys
import threading
import time
import webbrowser
from pathlib import Path

os.environ.setdefault("MOM_LOCAL_TASKS", "1")
if not os.environ.get("MOM_DATA_DIR"):
    base = os.environ.get("LOCALAPPDATA") or str(Path.home())
    os.environ["MOM_DATA_DIR"] = os.path.join(base, "MoM-AI")

PORT = int(os.environ.get("PORT", "5000"))


def main() -> None:
    from app import create_app
    from waitress import serve

    app = create_app()

    def open_browser() -> None:
        import urllib.request

        url = f"http://127.0.0.1:{PORT}"
        for _ in range(240):
            try:
                urllib.request.urlopen(f"{url}/healthz", timeout=1)
                break
            except Exception:
                time.sleep(0.5)
        webbrowser.open(url)

    threading.Thread(target=open_browser, daemon=True).start()
    url = f"http://127.0.0.1:{PORT}"
    print(f"MoM AI (IPS) running on {url} - close this window to stop.")
    serve(
        app,
        host="127.0.0.1",
        port=PORT,
        threads=8,
        channel_timeout=3600,
        max_request_body_size=64 * 1024 ** 3,
    )


if __name__ == "__main__":
    main()
