import sys
from multiprocessing import freeze_support
from pathlib import Path

if __name__ == "__main__":
    freeze_support()
    if len(sys.argv) == 3 and sys.argv[1] == "--separate-audio":
        from choicer_voicer_pack_creator.separation_worker import worker_main

        raise SystemExit(worker_main(Path(sys.argv[2])))
    elif len(sys.argv) == 3 and sys.argv[1] == "--apply-update":
        from choicer_voicer_pack_creator.updates import helper_main

        raise SystemExit(helper_main(Path(sys.argv[2])))
    elif "--mcp" in sys.argv:
        from choicer_voicer_pack_creator.mcp_server import main

        raise SystemExit(main([item for item in sys.argv[1:] if item != "--mcp"]))
    else:
        from choicer_voicer_pack_creator.app import main

        raise SystemExit(main())
