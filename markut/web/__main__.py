"""python -m markut.web [--host H] [--port P] [--reload]
Defaults come from the environment so the same command works locally and on
Railway: PORT (Railway sets it), HOST (0.0.0.0 in production, loopback
locally)."""
import argparse
import os

import uvicorn

from markut import config


def main():
    parser = argparse.ArgumentParser(description="Serve the Markut web app")
    parser.add_argument("--host", default=os.getenv("HOST", "0.0.0.0" if config.IN_PRODUCTION else "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    parser.add_argument("--reload", action="store_true", help="dev auto-reload")
    args = parser.parse_args()
    print(f"Markut web -> http://{args.host}:{args.port}  "
          f"({'production' if config.IN_PRODUCTION else 'local'}; console "
          f"{'password-protected' if config.CONSOLE_PASSWORD else ('LOCKED — set CONSOLE_PASSWORD' if config.IN_PRODUCTION else 'open')})")
    # WHY one worker: the live-debate lock and the warmed models live in
    # process memory; a second worker would double the memory and let two
    # paid debates run at once
    uvicorn.run("markut.web.app:app", host=args.host, port=args.port, reload=args.reload,
                workers=1, proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
