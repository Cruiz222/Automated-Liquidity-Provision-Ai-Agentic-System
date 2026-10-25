"""Run the simulation platform:  python3 -m app   (then open the printed address)."""
from __future__ import annotations

import logging

from .api.server import create_server
from .config import Settings
from .service import PlatformService


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    service = PlatformService(Settings.from_env())
    server = create_server(service)
    host, port = server.server_address[:2]
    print(f"Liquidity Lab (SIMULATION ONLY) listening on http://{host}:{port}  - Ctrl+C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        service.close()


if __name__ == "__main__":
    main()
