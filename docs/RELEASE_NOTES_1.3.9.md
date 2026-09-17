# Choicer Voicer Pack Creator 1.3.9

## Background voice preparation

Speaker/voice preparation now has its own worker slot and runs independently of CPU jobs,
so it no longer waits behind backing generation. Progress and controls remain in the
existing background-processing and Tasks surfaces.

## Singing-preserving backing

**Project > Generate Backing Track** and the Pack Details action now offer:

- **Remove all vocals (dialogue and singing)**: the existing HTDemucs behavior.
- **Keep singing; remove dialogue**: Facing the Music BandIt combined, keeping music
  (including singing) and effects while removing speech.

Video and YouTube imports still queue HTDemucs automatically without a mode picker.
To change an already queued, running, or permission-waiting request, open the generation
dialog and choose **Cancel and choose mode...**. A fresh picker opens only after the old
request has stopped. Tasks retains progress, cancellation, retry and details.

Manual mode preferences persist with the project and edit history. Generation selects a
new durable backing file only after success; existing media, prompts, captions and timings
are preserved, with confirmation before replacing a selected backing.

The portable application includes the CPU runtime; no GPU or external Python installation
is required. BandIt's approximately 426 MiB model is downloaded only with permission and
reused locally. Its source is **Apache-2.0**; the optional weights are **CC BY-NC 4.0
(non-commercial use)**. Attribution and full terms are available in About and bundled notices.

## Qualification scope

The previously auditioned singing-aware GPU result was accepted by the project owner.
That does not establish CPU performance or packaged real-model parity.

This personal release defers extended full-context real-checkpoint CPU parity, one-versus-two
thread profiling, and native source-Python 3.11 qualification. Historical one-thread CPU
reference tests completed tiny and eight-second mono/stereo windows; the historical full
41-second reference stopped safely under memory pressure and is not a passing full-clip result.
No complete new real-clip candidate/packaged parity result is claimed.

Repository tests and candidate/clean-ZIP packaged checks remain release requirements,
including the offline tiny BandIt actual-architecture smoke and native worker isolation.
These checks exercise bundled runtime loading and synthetic processing, not full production
window memory needs or separation quality. Processing may be slow or report insufficient
resources on constrained machines; it does not silently substitute another model.
