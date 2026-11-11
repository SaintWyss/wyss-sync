#!/usr/bin/env python3
# Wyss Sync — panel visual de la sincronización entre las 2 PC. Solo lectura.
import sys

sys.dont_write_bytecode = True
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--cli":   # lo usa el panel de la otra PC, sin pantalla
        from wyss_sync.manage import cli
        sys.exit(cli(sys.argv[2:]))
    if len(sys.argv) > 1 and sys.argv[1] == "--historian":   # servicio de registro continuo, sin pantalla
        from wyss_sync.core import SyncthingApi, read_config
        from wyss_sync.manage import historian
        historian(lambda: SyncthingApi(*read_config()))
        sys.exit(0)
    from wyss_sync.ui import main
    main()
