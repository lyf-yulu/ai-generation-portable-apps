# AI Generation Portable Apps

Version: `v0.2`

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

- `v0.2`: Added Nano Banana provider switching, optional browser-side image resize, drag-and-drop uploads, and cross-platform desktop output path helpers.
- `v0.1`: Initial portable release.
