# Knowledge-base contract

**Status:** Partial
**Last Updated:** 2026-11-13
**Audience:** Developers and maintainers
**Last verified:** 2026-11-13 against `17a278b`

This page defines the repository's documentation rules. The contract and top-level map exist. Page generation and automated checks do not exist.

## Page metadata

Each KB page starts with an H1 title, followed by these fields in this order:

1. `**Status:**` with one value: Implemented, Partial, Planned, or Superseded.
2. `**Last Updated:**` with the date of the last content change in `YYYY-MM-DD` form.
3. `**Audience:**` with the intended readers.
4. `**Last verified:**` with a date and short Git commit ID, for example `2026-11-13 against \`17a278b\``.

A page about built code also has a `**Code:**` line. List its main source paths on that line. This line maps the KB page to the code it describes.

## Describe current behavior

Describe what the code does now. Do not describe intended behavior as if it already exists.

Put behavior that is not built in a section named `Planned`. Mark its status as Planned.
Update the page when behavior changes. Set its verification date and commit ID after checking the code.

## Indexes and maps

Each documentation folder has a `README.md` index. Add one line for every page.
Name the page and state the question it answers.

`docs/README.md` is the top-level documentation map. It identifies where each kind of knowledge lives and lists the main code areas and their folders.

## Generated pages

Scripts must generate KB pages from their source data. Do not edit generated pages by hand.
Change their source data and run the generator.

## Planned local support

- Page generation and a local check for metadata, code paths, indexes, and verification dates are Planned. No generator or check exists.
- A local evaluation of the map against code and documentation is Planned. No evaluation exists.
