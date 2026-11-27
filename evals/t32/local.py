"""Run the buyer widget against the user's OpenCode Go subscription in WSL, on loopback."""

import argparse
import json
import os
import secrets
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", required=True)
    parser.add_argument(
        "--data-dir", type=Path, required=True, help="Private local directory containing private/catalog.sqlite3"
    )
    parser.add_argument("--port", type=int, default=8332)
    args = parser.parse_args()
    if not (args.data_dir / "private/catalog.sqlite3").is_file():
        parser.error("The T31 catalog index is missing.")
    xdg = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    try:
        key = json.loads((xdg / "opencode/auth.json").read_text())["opencode-go"]["key"]
        if not isinstance(key, str) or not key.strip():
            raise ValueError
    except (KeyError, OSError, ValueError):
        parser.error("No OpenCode Go API key in the current WSL user's OpenCode session.")
    os.environ.update(
        LLM_API_KEY=key,
        LLM_BASE_URL="https://opencode.ai/inference/go/openai/v1",
        LLM_PROXY_URL="",
        AGENT_ENGINE="tools",
        MOCK_LLM="false",
        MODEL_SMART=args.model,
        MODEL_FAST=args.model,
        DATA_DIR=str(args.data_dir.resolve()),
        PUBLIC_BASE_URL=f"https://127.0.0.1:{args.port}",
        LLM_TIMEOUT_S="18",
        OPERATOR_PASSWORD=os.environ.get("OPERATOR_PASSWORD") or secrets.token_urlsafe(32),
        MODEL_SMART_PRICE_IN_USD="0",
        MODEL_SMART_PRICE_OUT_USD="0",
        MODEL_FAST_PRICE_IN_USD="0",
        MODEL_FAST_PRICE_OUT_USD="0",
    )
    import uvicorn
    from fastapi.staticfiles import StaticFiles

    from app.main import create_app

    root = Path(__file__).resolve().parents[2]
    app = create_app()
    app.mount("/chat", StaticFiles(directory=root / "frontend/chat", html=True))
    app.mount("/manager", StaticFiles(directory=root / "frontend/manager", html=True))
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
