# Paperclip single-file executable

Builds a Windows onedir bundle (or a single `paperclip.exe` when `--onefile` is
requested) that runs the real Paperclip application. The standalone build embeds
Node.js; the user-facing single download is the Inno Setup package described in
`packaging/windows-installer/`.

```
packaging\exe\dist\paperclip\paperclip.exe doctor
```

---

## What this is — and what it deliberately is not

Paperclip is a Node.js/TypeScript product: ~972k lines of server code, a React
UI, a Rust runner, 210 database tables and 282 SQL migrations. It is **not**
rewritten in Python here, because a partial rewrite would silently behave
differently from the real product.

Instead the executable **contains the real, unmodified build output**, fronted by
a small Python launcher that execs it with inherited stdio. So:

| | |
|---|---|
| Same behaviour as `npx paperclipai` | ✅ it *is* that bundle, byte-for-byte |
| Interactive prompts / TUI / raw mode | ✅ stdio inherited, not piped |
| Exit codes | ✅ propagated exactly (verified 0/1/5/42) |
| ESM + `import.meta` semantics | ✅ preserved (asserted in tests) |
| Needs Node.js on the target machine | ✅ **no** for the standalone build (`--embed-node`) |
| Embedded Postgres works offline | ✅ binaries staged, sibling layout preserved |

### Why a Python launcher rather than a Node SEA blob

Node's [Single Executable Applications](https://nodejs.org/api/single-executable-applications.html)
only accept a **CommonJS** main script. The Paperclip CLI bundles to **ESM**
(`format: "esm"` in `cli/esbuild.config.mjs`) and roughly 50 production source
files depend on `import.meta.url` — e.g. `cli/src/version.ts`,
`cli/src/commands/client/agent.ts`, `cli/src/commands/run.ts`. esbuild cannot
lower `import.meta` to CJS faithfully, so a SEA build would ship a subtly broken
app. The launcher keeps the upstream bundle intact, and `test_frozen_bundle.py`
asserts `import.meta.url` still resolves at runtime.

---

## Building

### Windows (produces `paperclip.exe`)

Double-click **`packaging\exe\build-exe.bat`**, or from a terminal:

```bat
packaging\exe\build-exe.bat
```

Output: `packaging\exe\dist\paperclip\` (onedir bundle)

Requirements at build time:
- **Python 3.9+** — tick *"Add python.exe to PATH"* in the installer. Prefer
  python.org builds over Windows Store Python: they ship `python3xx.dll`, which
  PyInstaller needs.
- **Node.js 24.11+** and **pnpm** (`corepack enable`). Node is used to build the
  app and download/stage the portable runtime. It is **not required on the target**.

For the single user-facing Setup.exe, run
`packaging\windows-installer\build-installer.ps1`; it packages this whole onedir
folder with Inno Setup.

The script installs PyInstaller and runs `pnpm install` for you if either is
missing.

### Linux / macOS

```bash
./packaging/exe/build-exe.sh
```

Creates its own virtualenv at `packaging/exe/.venv-build`, so it works even
where `pip install` is blocked by PEP 668 (*externally-managed-environment*).

> **Linux packaging pitfall.** Debian/Ubuntu *system* Python is built without a
> shared `libpython`, and PyInstaller cannot link against it — you'll see
> `ERROR: Python shared library ('libpython3.11.so.1.0') was not found`. Fix:
> `sudo apt install libpython3.11`, or install Python from python.org, or
> `uv python install 3.12 && uv venv --python 3.12`. The script warns up front.
> Windows and macOS are unaffected.

### Without a Windows machine

PyInstaller cannot cross-compile, so `.github/workflows/build-exe.yml` builds it
for you on GitHub's runners:

```bash
gh workflow run build-exe.yml -f os=windows -f layout=onedir
# then download from the run's Artifacts section
```

Pushing a `v*` tag builds all three platforms.

---

## Build stages

`build_exe.py` runs seven idempotent stages, each individually skippable:

| # | Stage | Flag | What it does |
|---|---|---|---|
| 1 | preflight | — | Python, Node version gate, repo layout, version agreement |
| 2 | app build | `--skip-app-build` | esbuild-bundles the CLI |
| 3 | payload | `--stage-payload` | stages `app/` + `node_modules/` + `assets/`, dereferencing symlinks |
| 4 | postgres | `--require-postgres` | verifies/repairs the embedded-Postgres native runtime |
| 5 | runtime | `--embed-node` | downloads a portable Node for the target |
| 6 | freeze | `--no-freeze` | PyInstaller → `dist/paperclip[.exe]`, then signs on Windows |
| 7 | verify | `--run-doctor` | runs the produced binary's self-check |

`--all` enables stages 3 and 6. `--dry-run` reports what *would* happen and
writes nothing (asserted by tests).

```bash
# standalone build: Node is embedded; target machines need no Node
python3 packaging/exe/build_exe.py --stage-payload --embed-node --freeze --onedir

# development-only build using the target machine's Node
python3 packaging/exe/build_exe.py --stage-payload --freeze --system-node

# package an already-built cli/dist, no rebuild
python3 packaging/exe/build_exe.py --skip-app-build --stage-payload
```

Other flags: `--onedir`, `--sign`, `--allow-unsigned`, `--no-version-resource`,
`--node-version`, `--name`, `--target-os`, `--target-arch`, `--run-doctor`,
`--require-postgres`, `--allow-incomplete-postgres`, `--allow-missing-deps`,
`--allow-old-node`, `--allow-version-mismatch`.

---

## Embedded PostgreSQL

**Correcting an earlier claim in this document:** `embedded-postgres` does *not*
download Postgres at first run. Its binaries ship as `os`/`cpu`-gated npm
`optionalDependencies` — `@embedded-postgres/windows-x64`,
`@embedded-postgres/linux-x64`, etc. — which the package manager installs at
`pnpm install` time. So an offline machine is fine *provided staging is done
right*. Three things can silently break it, and all three are handled:

**1. Sibling layout.** `packages/db/src/embedded-postgres-native.ts` resolves the
native package relative to the wrapper:

```ts
nativeRoot = path.resolve(packageRoot, "..", "@embedded-postgres", slug)
```

So `node_modules/embedded-postgres` and `node_modules/@embedded-postgres/<slug>`
**must share one parent directory**. Staging flattens into exactly that layout,
and `test_pnpm_store_flattens_to_sibling_layout` asserts it.

**2. Symlinks.** pnpm's `node_modules` is a forest of symlinks into a `.pnpm`
virtual store. Windows cannot create symlinks without privilege or developer
mode, and PyInstaller archives do not preserve them — so a naive copy produces a
bundle that works on the build machine and breaks on the target. Staging
dereferences every link into real files (`copy_tree_materialized`), reports how
many it resolved, and fails loudly if any remain.

Two subtle bugs here were found by tests and fixed:
- destination names must come from the **link**, not its target's basename
  (pnpm links `foo` at `.pnpm/foo@1.2.3/node_modules/foo`);
- loop detection must be **branch-scoped** — a shared store directory reachable
  from several subtrees is normal in pnpm, and global detection dropped packages.

**3. Linux `.so` aliases.** The app creates `libX.so.A` aliases for
`libX.so.A.B` at runtime, which needs a *writable* lib directory. Since a frozen
bundle should not depend on that, staging pre-creates the aliases as **real
copies** (not symlinks, which would not survive archiving). It also restores the
executable bit on `initdb`/`postgres`/`pg_ctl`, which copying can drop and which
otherwise surfaces as a confusing `EACCES`.

The launcher also prepends the native `lib` directory to `LD_LIBRARY_PATH` on
Linux, so the loader finds `libpq` even for subprocesses spawned before the app's
own hook runs.

**Diagnostics.** `--launcher-doctor` reports the Postgres runtime explicitly:

```
Embedded PostgreSQL
  OK   @embedded-postgres/linux-x64 [healthy] at .../node_modules/@embedded-postgres/linux-x64
      wrapper package : present
      sibling layout  : correct
      native/lib      : .../native/lib
```

An incomplete Postgres is a **warning, not a blocker** — Paperclip also runs
against an external `DATABASE_URL`, so the exe still works. Pass
`--require-postgres` to make the build fail instead. Note that no
`@embedded-postgres` build is published for `windows/arm64`; there, an external
database is the only option.

---

## Antivirus and SmartScreen

**The honest framing: nothing makes an *unsigned* PyInstaller binary invisible to
antivirus.** What actually reduces false positives, in descending order of
effect:

**1. Sign it.** A trusted Authenticode signature is the only real fix — and it
requires a certificate from a CA (DigiCert, Sectigo, SSL.com, Certum). It cannot
be generated locally. Signing is fully wired; supply a credential and it happens:

```bat
REM certificate in the machine store, by subject name
set PAPERCLIP_SIGN_SUBJECT=Paperclip Inc
packaging\exe\build-exe.bat

REM or from a .pfx file
set PAPERCLIP_PFX=C:\certs\paperclip.pfx
set PAPERCLIP_PFX_PASSWORD=...
packaging\exe\build-exe.bat
```

Also honoured: `PAPERCLIP_SIGN_SHA1` (thumbprint), `PAPERCLIP_SIGNTOOL` (explicit
path), `PAPERCLIP_TIMESTAMP_URL` (`none` to disable; falls back across timestamp
servers automatically), `PAPERCLIP_SIGN_ARGS`. Both file and timestamp digests are
SHA-256, as modern Windows requires. If signing was requested but cannot
complete, the build **fails** rather than silently shipping unsigned — unless you
pass `--allow-unsigned`. The password is masked in echoed commands.

**2. Ship `--onedir` instead of onefile.** A onefile exe unpacks itself into
`%TEMP%` and executes from there — the pattern AV heuristics target most.
`--onedir` produces a folder with a normal launcher exe and far fewer hits. It is
the CI default. Zip the folder to distribute.

**3. Version metadata.** Binaries with no `VERSIONINFO` are treated as suspicious
by many engines. Windows builds embed one automatically (generated from
`cli/package.json`), carrying company, product, version and copyright. Skip with
`--no-version-resource`.

**4. UPX off, no debug bootloader.** UPX both corrupts `node.exe` / Postgres
binaries and trips AV; the spec hard-disables it.

Every build prints its resulting posture:

```
Antivirus / SmartScreen posture
  signed          : NO
  layout          : onefile (self-extracting)
  version metadata: yes
  UPX packed      : no
  assessed risk   : high
  - onefile self-extracts into %TEMP% ... Build with --onedir ...
  - unsigned: SmartScreen will warn ... set PAPERCLIP_SIGN_SUBJECT ...
```

**Residual risk you cannot engineer away:** even signed, a *new* executable has no
SmartScreen reputation until enough users have run it. Only OV/EV certificates
accumulate reputation, and EV does so immediately. That is a property of
Windows, not of this build.

---

## Using the executable

Every argument that isn't a `--launcher-*` flag is forwarded to Paperclip
verbatim — the launcher never interprets Paperclip's own CLI grammar, so new
upstream commands work without touching this code.

```bash
paperclip.exe doctor
paperclip.exe onboard
paperclip.exe issue list --company acme
```

### Launcher controls

| Flag | Purpose |
|---|---|
| `--launcher-doctor` | Diagnose the install (Node, payload, node_modules, Postgres) |
| `--launcher-info` | Same, as JSON |
| `--launcher-version` | Launcher + Node + app versions (works even when broken) |
| `--launcher-help` | Usage |
| `--launcher-fetch-node` | Allow downloading a portable Node if none qualifies |
| `--launcher-node PATH` | One-shot Node override |
| `--launcher-payload PATH` | One-shot payload override |

A bare `--` ends launcher parsing, so `paperclip.exe -- --launcher-info` sends
that flag to the app instead.

### Environment

| Variable | Effect |
|---|---|
| `PAPERCLIP_NODE` | Node binary to use |
| `PAPERCLIP_PAYLOAD` | Payload root (dir containing `app/index.js`) |
| `PAPERCLIP_ENTRY` | Entry JS file directly |
| `PAPERCLIP_STRICT_PAYLOAD=1` | Disable discovery fallbacks; explicit config only |
| `PAPERCLIP_DATA_DIR` | Override the per-user data dir |
| `PAPERCLIP_FETCH_NODE=1` | Permit runtime Node download |
| `PAPERCLIP_NODE_OPTIONS` | Extra Node flags, inserted before the entry point |
| `PAPERCLIP_LAUNCHER_DEBUG=1` | Trace resolution decisions on stderr |
| `PAPERCLIP_NODE_MIRROR` | Mirror for Node downloads |

A frozen exe searches only the bundle, then beside itself, then the cwd — never
back into a developer source tree, so a shipped exe cannot silently pick up a
checkout.

---

## What's inside the binary

```
_MEIPASS/  (onefile)   or   dist/paperclip/_internal/  (onedir)
  paperclip_exe/          the Python launcher
  runtime/node/           portable Node.js distribution   (--embed-node only)
  payload/
    app/index.js          the real esbuild CLI bundle
    app/package.json      declares the external npm deps
    node_modules/         those deps, symlinks materialized
      embedded-postgres/
      @embedded-postgres/<slug>/   sibling of the above, as the app requires
    assets/               server/ui dist, migrations, skills, runner vendor
```

---

## Verifying a build

```bash
./packaging/exe/verify-exe.sh                          # auto-finds dist/
./packaging/exe/verify-exe.sh path/to/paperclip.exe    # explicit
```

Checks the binary exists (onefile *or* onedir), the launcher responds,
`--launcher-info` is valid JSON with both a Node runtime and a payload, the
Postgres native runtime is staged, doctor reports healthy, the app answers
`--version`, and exit codes propagate. Postgres problems are warnings; Node and
payload problems are failures.

### Tests

167 tests, stdlib `unittest` only — no extra dependencies:

```bash
cd packaging/exe && python3 -m unittest discover -s tests -t . -v
```

| File | Covers |
|---|---|
| `test_launcher_flags.py` | flag splitting; app args never swallowed |
| `test_payload.py` | payload discovery, strict mode, per-platform data dir |
| `test_runner.py` | env construction, argv order, exit/signal propagation, stdio inheritance |
| `test_runtime.py` | version parsing, Node probing, archive extraction, install idempotency |
| `test_launcher_e2e.py` | full subprocess runs; Postgres reporting; `LD_LIBRARY_PATH` |
| `test_frozen_bundle.py` | the frozen `_MEIPASS` path a real exe takes |
| `test_postgres.py` | slug mapping, sibling resolution, `.so` aliases, missing binaries |
| `test_staging.py` | symlink dereferencing, loop safety, name preservation, exec bits |
| `test_signing.py` | signtool argv, credential detection, password masking, VERSIONINFO |

The staging and Postgres tests build a real pnpm-style symlinked store and assert
the flattened result still satisfies the app's own resolver.

---

## Remaining limitations

1. **The exe is not a Python port.** The application inside is still
   Node.js/TypeScript. This gives identical behaviour; it does not reduce the
   codebase to Python-only.
2. **Node.js is not required at run time** for the standalone Windows build; the Setup.exe includes a portable runtime. A development-only `--system-node` build still expects Node on the target.
3. **Platform-specific binaries.** PyInstaller cannot cross-compile: `.exe` on
   Windows, Mach-O on macOS, ELF on Linux. Build each on its own OS (or use CI).
4. **Size.** With the full payload and server assets, expect roughly 100–250 MB
   (onefile), more with `--embed-node` (+~60 MB). `--onedir` is the same total
   size but starts faster.
5. **First-run extraction (onefile only).** Unpacks to a temp dir on each start,
   costing a second or two and needing writable temp space. `--onedir` avoids it.
6. **Unsigned binaries get flagged.** Mitigations above reduce but do not
   eliminate this; only a CA-issued certificate truly fixes it.
7. **The Rust runner** (`packages/paperclip-runner`) is a separate native binary
   staged as a vendor asset, not compiled into the exe.
8. **`windows/arm64` has no embedded Postgres build** — use an external database.
