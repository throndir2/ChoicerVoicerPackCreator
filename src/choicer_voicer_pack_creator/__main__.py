import sys
from pathlib import Path

if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--separate-audio":
        from choicer_voicer_pack_creator.separation_worker import worker_main

        raise SystemExit(worker_main(Path(sys.argv[2])))
    elif len(sys.argv) == 3 and sys.argv[1] == "--apply-update":
        from choicer_voicer_pack_creator.updates import helper_main

        raise SystemExit(helper_main(Path(sys.argv[2])))
    else:
        from choicer_voicer_pack_creator.app import main

        raise SystemExit(main())
