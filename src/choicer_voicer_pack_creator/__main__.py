import sys

if "--mcp" in sys.argv:
    from choicer_voicer_pack_creator.mcp_server import main

    arguments = [item for item in sys.argv[1:] if item != "--mcp"]
else:
    from choicer_voicer_pack_creator.app import main

    arguments = sys.argv

if __name__ == "__main__":
    raise SystemExit(main(arguments))
