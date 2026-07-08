# AI Generation Portable Apps

Version: `v0.2.6`

This repository contains two local portable web apps:

- `seedance/`: Seedance multi-reference video generator
- `nano-banana/`: Nano Banana multi-image reference generator

Both apps are Python standard-library apps. They run a local HTTP server, open a browser UI, support local archives, and do not require third-party Python packages. The bundled launchers support macOS and Windows; Windows launchers download a local portable Python runtime on first run.

## Usage

### Seedance

Open:

```bash
seedance/Start\ Seedance.command
```

or:

```bash
cd seedance
./start.sh
```

On Windows, double-click:

```text
seedance/Start_Seedance.cmd
```

### Nano Banana

Open:

```bash
nano-banana/Start\ Nano\ Banana.command
```

or:

```bash
cd nano-banana
./start.sh
```

On Windows, double-click:

```text
nano-banana/Start_Nano_Banana.cmd
```

## Notes

- Do not open `static/index.html` via `file://`; use the startup scripts so the local backend is running.
- API keys are entered in the UI or read from local configuration when available.
- Runtime outputs, local state, and archives are ignored by git.
- Archive files may contain API keys and media assets. Do not share archives unless that is intended.

## Release

- `v0.2.6`: Added an “open output directory” control in both apps and a manual cache cleanup action for old orphan media and logs. Generated outputs are never deleted by this cleanup.
- `v0.2.5`: Moved provider/model defaults into `providers.json`, added `/api/config` and `/api/request-template` for agents, added compatible API error fields, added history restore back to the current page, and made Nano empty-seed batch runs auto-assign non-reused seeds when `vary_seed` is enabled.
- `v0.2.4`: Added local JSON API submission and backend activity records for page/API runs.
- `v0.2.3`: Added Seedance official Ark provider support and Seedance 2.0/2.0-fast model entries.
- `v0.2.2`: Added browser-side topic workspaces so multiple independent theme pages can run different prompts and settings concurrently from one local service.
- `v0.2.1`: Fixed Windows launcher startup reliability and wired Seedance first/last frame uploads into preview, drag/drop, and removal controls.
- `v0.2`: Added Nano Banana provider switching, optional browser-side image resize, drag-and-drop uploads, and cross-platform desktop output path helpers.
- `v0.1`: Initial portable release.
