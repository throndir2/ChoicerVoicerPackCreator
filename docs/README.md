# Documentation map

**Status:** Implemented
**Last Updated:** 2026-11-13
**Audience:** Users, developers, and maintainers
**Last verified:** 2026-11-13 against `17a278b`
**Code:** `src/choicer_voicer_pack_creator/`, `MCP/`, `scripts/`, `installer/`, `Build-Portable.ps1`, `Build-Installer.ps1`

## Where knowledge lives

- [User guide](USER_GUIDE.md) answers: How do I create, edit, save, and export a pack?
- [Development guide](DEVELOPMENT.md) answers: How do I set up, build, validate, and release the application?
- [Cleanup guide](CLEANUP.md) answers: Which generated files can I remove, and how?
- [Release notes for 1.3.9](RELEASE_NOTES_1.3.9.md) answer: What changed in 1.3.9, and what work was deferred?
- [Separation comparison](SEPARATION_COMPARISON.md) answers: How was singing-preserving separation compared, and what are its limits?
- [Knowledge-base contract](kb.md) answers: What rules apply to KB pages, indexes, code maps, and generated pages?

The root [README](../README.md) gives the project overview and main features.
The [MCP guide](../MCP/README.md) explains how to connect an MCP client.

The bundled MCP help is in `src/choicer_voicer_pack_creator/resources/mcp-help.md`.

## Main code areas

- `src/choicer_voicer_pack_creator/` contains the Python application, project and media operations, background work, validation, export, and MCP entry points.
- `src/choicer_voicer_pack_creator/ui/` contains the Qt editor, timeline, dialogs, and task window.
- `src/choicer_voicer_pack_creator/_bandit/` contains the optional BandIt model files and their provenance.
- `src/choicer_voicer_pack_creator/resources/` contains bundled help, model configuration, and license notices.
- `MCP/` contains the MCP client setup guide; MCP server code is in the Python package.
- `scripts/` contains build support, packaged smoke checks, and external validation tools.
- `tests/` contains the automated tests.
- `installer/` and the root `Build-*.ps1` scripts create the Windows packages.
