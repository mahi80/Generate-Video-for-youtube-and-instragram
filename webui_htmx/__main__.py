"""Start the HTMX WebUI: ``python -m webui_htmx [--host H] [--port P]``."""

import argparse
import os
import socket
import sys

DEFAULT_PORT_RANGE = range(8510, 8600)


def _free_port(host: str) -> int:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    for port in DEFAULT_PORT_RANGE:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            try:
                probe.bind((host, port))
            except OSError:
                continue
            return port
    raise OSError(f"no free port on {host} between 8510 and 8599")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m webui_htmx",
                                     description="MoneyPrinterTurbo HTMX WebUI")
    parser.add_argument("--host", default=os.environ.get("MPT_WEBUI_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=None,
                        help="listen port (default: first free port from 8510)")
    parser.add_argument("--reload", action="store_true", help="restart on code changes")
    args = parser.parse_args(argv)

    import uvicorn

    port = args.port or _free_port(args.host)
    display_host = "localhost" if args.host in ("127.0.0.1", "0.0.0.0", "::") else args.host
    print(f"***** HTMX WebUI address: http://{display_host}:{port} *****", flush=True)
    uvicorn.run(
        "webui_htmx.app:app",
        host=args.host,
        port=port,
        reload=args.reload,
        log_level="warning",
        # 任务、配置和会话状态都在进程内存中，只能使用单进程。
        workers=1,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
