# In-App Updater — Plan

Delete this file once the work lands. What survives it: the trust model
(section 2) and the manifest format (section 3) belong in the docstring of
`trenchchat/core/updater.py`; everything else here is narration of work that
will be visible in the diff.

## 1. What exists today

Releases are already fully automated. `.github/workflows/release.yml` runs on
every push to `main`: tests, then a version bump computed from the latest
`vX.Y.Z` git tag (the tag is the only source of truth — nothing is committed
back to `pyproject.toml`), then a three-OS matrix build, then a GitHub Release
carrying three assets:

| Platform | Asset | Installed to |
|---|---|---|
| Windows | `TrenchChat-Setup-X.Y.Z.exe` (Inno Setup) | `%ProgramFiles%\TrenchChat\`, in-place upgrade via fixed `AppId` |
| Debian/Ubuntu | `trenchchat-X.Y.Z-amd64.deb` | `/opt/trenchchat/`, in-place via `Replaces:` |
| macOS | `TrenchChat-X.Y.Z-macos.dmg` | user drags `TrenchChat.app` to `/Applications` |

All three preserve `~/.trenchchat/`. None are code-signed.

Four gaps stand between that and an updater:

- **The app does not know its own version.** `APP_VERSION` reaches the macOS
  bundle plist and the installer filenames, and nothing else. No Python
  constant, nothing shown in the UI.
- **Nothing is published for a client to poll.** A client would have to scrape
  the GitHub API and guess asset names from a filename convention.
- **Nothing is signed.** The binaries are unsigned, so the OS will not vouch
  for an installer we download. If we download and execute one, we have to
  authenticate it ourselves.
- **The macOS and Linux artifacts are single-architecture.** `macos-latest` is
  Apple silicon, so today's DMG is arm64-only; the `.deb` is amd64-only. The
  manifest must name architectures rather than implying a universal build, and
  a client on an unmatched platform must report "no update for this platform"
  rather than offering the wrong binary.

The updater itself belongs in the Python backend — it needs the network, the
filesystem, and the ability to spawn a process — and reaches the Flutter client
through the API layer, per CLAUDE.md's "Flutter client" section.

## 2. Trust model

This is the part worth getting right, because a broken updater is a remote code
execution channel into every install.

**Assumed attacker.** Anyone who can answer a TLS connection the client makes:
a hostile network, a compromised CDN edge, a DNS hijack, or GitHub itself
serving the wrong bytes. Not assumed: an attacker with the release signing key,
or one who already has code execution on the client.

**What we rely on.** One Ed25519 release key. Its public half is a constant in
`trenchchat/core/updater.py`, compiled into every build; its private half lives
only in a GitHub Actions secret and signs a manifest at release time. TLS is
used, but is not the trust anchor — a fully hostile transport cannot produce a
manifest the client will accept.

**The chain.** Signature authenticates the manifest; the manifest carries a
SHA-256 for each artifact; the SHA-256 authenticates the downloaded installer.
The installer is never executed before its hash matches. Neither GitHub nor the
CDN is trusted with content, only with availability.

**What this does not defend against.**

- *Freeze/rollback.* A network attacker can serve a stale-but-validly-signed
  manifest forever, holding a client on a known-vulnerable version. Partly
  mitigated by persisting `last_seen_version` in config and refusing any
  manifest older than it, which makes the attack sticky-per-client rather than
  reversible. Fully solving it needs manifest expiry and a monotonic counter;
  noted, not built.
- *Key compromise.* One key, baked into shipped binaries, cannot be revoked
  from the outside. Mitigation is structural: the verifier takes a **list** of
  trusted keys, so a successor key can ship one release ahead of the rotation
  and the changeover costs nobody a manual reinstall. Losing the key without a
  staged successor means every existing install must be replaced by hand.
- *A malicious release.* The updater authenticates the publisher, not the
  publisher's intentions. Unchanged from today.

**Privacy.** An update check is an HTTPS request to `github.com` — for most
installs, the only clearnet connection TrenchChat makes, and one that tells an
observer that this IP runs TrenchChat. That is a real cost in a project whose
premise is that the mesh is the network. It gets an explicit setting, it is
never a blocking startup step, and it fails silently on air-gapped installs.
Whether it defaults on is an open decision (section 10).

## 3. The manifest

Two assets, attached to every release alongside the installers:

- `manifest.json` — the document below
- `manifest.json.sig` — hex-encoded raw Ed25519 signature over the **exact
  bytes** of `manifest.json` (no canonicalisation step, so there is nothing to
  get subtly wrong on either side)

```json
{
  "schema": 1,
  "version": "1.4.0",
  "published_at": 1755561600,
  "minimum_version": "0.0.0",
  "notes_url": "https://github.com/HunterL31/TrenchChat/releases/tag/v1.4.0",
  "notes": "- Fixed sync stall on reconnect (a1b2c3d)\n- ...",
  "artifacts": {
    "windows-x86_64": {
      "url": "https://github.com/HunterL31/TrenchChat/releases/download/v1.4.0/TrenchChat-Setup-1.4.0.exe",
      "size": 94371840,
      "sha256": "..."
    },
    "macos-arm64":  { "url": "...", "size": 0, "sha256": "..." },
    "linux-x86_64": { "url": "...", "size": 0, "sha256": "..." }
  }
}
```

`minimum_version` exists for the day the installer layout changes
incompatibly: a client older than it stops offering a one-click update and
tells the user to reinstall by hand.

**Fetched from**, with no API call, no auth, and no rate limit:

```
https://github.com/HunterL31/TrenchChat/releases/latest/download/manifest.json
https://github.com/HunterL31/TrenchChat/releases/latest/download/manifest.json.sig
```

`releases/latest/download/...` redirects to whatever the newest release is, so
the client hard-codes one URL and never parses GitHub's API schema.

## 4. Release pipeline changes

**Bake the version into the build.** New `trenchchat/version.py`:

```python
"""Application version. CI rewrites __version__ from the release tag."""

__version__ = "0.0.0-dev"
```

The build job rewrites the literal from `$APP_VERSION` after checkout and
before PyInstaller. `0.0.0-dev` therefore means "running from a checkout", and
the updater refuses to do anything at all in that state — a dev tree must never
overwrite itself with a release build.

**Generate and sign the manifest.** A step in the existing `release` job, after
the installers are downloaded and before `action-gh-release`: hash each asset,
render `manifest.json` from the `bump_version` outputs, sign it with
`secrets.UPDATE_SIGNING_KEY` (base64 Ed25519 seed), write `manifest.json.sig`,
and add both to `files:`.

**One-off key setup.** Generate the keypair locally with `cryptography`, put the
private seed in the repo secret, paste the public bytes into
`updater.py`'s trusted-key list. The private key never touches the repo.

**Rollout ordering.** The first release that *contains* the updater cannot be
delivered *by* it — clients only begin self-updating from the release after the
one they installed by hand. Phase 1 shipping ahead of the client work (section
9) means the manifest is already in place and proven by the time any client
looks for it.

## 5. Backend

### `trenchchat/core/updater.py` — `UpdateManager`

Constructed in `main.py`, `main_flutter.py`'s backend, and
`devtools/testenv/backend_core.py`'s `Backend.__init__` in the same order as
every other manager. Takes `config`, the current version, and a
`trusted_keys` list defaulting to the module constant, so tests substitute
their own key without touching a private attribute.

Public surface:

- `check() -> UpdateInfo | None` — fetch, verify, compare; returns the newer
  release or `None`. Never raises to the caller; failures land in `state`.
- `download()` — stream the artifact for this platform to
  `~/.trenchchat/updates/<version>/`, hashing as it goes.
- `install()` — hand off to `update_install.install()` and request shutdown.
- `state` / `add_state_callback()` — the callback fires on the worker thread,
  same contract as every other manager callback.
- `start_periodic_check(interval)` — daemon thread, mirroring
  `Backend.start_heartbeat`. First check delayed ~30s so it never competes with
  RNS startup.

State machine: `idle → checking → (up_to_date | available) → downloading →
ready → installing`, plus `error` from any state, carrying a message.

Verification and comparison are free functions — `verify_manifest(raw, sig,
trusted_keys)`, `parse_manifest`, `compare_versions` — with no I/O, so the
security-critical logic is directly unit-testable.

Fetch hardening: 10s timeouts, TLS verified (never disabled), manifest capped
at 64 KiB, artifact capped at both the manifest's `size` and a hard 300 MB,
downloads written to `.part` and renamed only after the hash matches, and
artifact hosts restricted to `github.com` / `*.githubusercontent.com`. The
signature already makes a redirected artifact URL a signed statement, but the
allowlist keeps a compromised release from pointing anywhere at all.

Rejection cases, all of which mean "no update, log a warning, stay on the
current version": bad or missing signature, unparseable manifest, `version <=
current`, `version < last_seen_version`, `current < minimum_version`, no
artifact entry for this platform/arch, size or hash mismatch.

### `trenchchat/core/update_install.py`

The one genuinely impure part, isolated so nothing else has to be. Single entry
point `install(installer_path, version) -> None`; tests inject a stub and
assert it is never reached on a failed hash.

**Windows.** Spawn the installer detached with
`/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /relaunch=1`, then exit through the
normal graceful shutdown so the offline announce still drains. A per-machine
install triggers a UAC prompt; a per-user install (already permitted by
`PrivilegesRequiredOverridesAllowed=dialog`) does not. Requires one change to
`packaging/windows/trenchchat.iss`, because the existing `[Run]` entry is
`skipifsilent` and so would never relaunch:

```
[Run]
Filename: "{app}\{#AppExeName}"; Flags: nowait runasoriginaluser; \
  Check: WantsRelaunch

[Code]
function WantsRelaunch: Boolean;
begin
  Result := ExpandConstant('{param:relaunch|0}') = '1';
end;
```

`runasoriginaluser` matters: the installer may be elevated, and the app must
not inherit that.

**macOS.** `hdiutil attach -nobrowse -readonly`, confirm `TrenchChat.app` is on
the mount, `ditto` it to `/Applications/TrenchChat.app.new`, then hand a small
detached shell script the swap — wait for our PID to exit, replace the bundle,
`hdiutil detach`, `open -a TrenchChat`. If `/Applications` is not writable,
fall back to opening the DMG in Finder and telling the user to drag it. Nothing
strips quarantine attributes: a file fetched by `urllib` never gets one, so
Gatekeeper is not in the path and there is nothing to work around.

**Linux.** A `.deb` needs root, which the app does not have. Try
`pkexec dpkg -i <deb>` when `pkexec` exists (polkit prompts the user), and
otherwise stop at "downloaded and verified", showing the file path and the
exact `sudo dpkg -i` command. The honest long-term answers are an apt
repository or an AppImage; both are separate work, and neither should hold up
Windows and macOS.

### Config

New block in `trenchchat/config.py`'s `_DEFAULTS`, with properties in the
existing style:

```json
"updates": {
  "enabled": true,
  "auto_download": false,
  "skipped_version": null,
  "last_check_ts": 0,
  "last_seen_version": null
}
```

`skipped_version` suppresses the prompt for one specific release without
disabling checks. `last_seen_version` is the rollback guard from section 2.

### API

New endpoints in `devtools/testenv/api.py`, thin as every other endpoint —
no logic:

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/update/status` | current version, state, available version, notes, progress, error, platform support |
| `POST` | `/update/check` | force a check now |
| `POST` | `/update/download` | start the download |
| `POST` | `/update/install` | verify, spawn, shut down |
| `POST` | `/update/skip` | set `skipped_version` |

The `updates` keys join `actions.read_settings` / `apply_settings` and
`SettingsUpdateRequest` rather than getting their own settings endpoint.
`UpdateManager`'s state callback emits an `update_status` event over the
`EventBus`, so the client tracks download progress without polling.

## 6. Flutter client

- `lib/api/models/update.dart` — `UpdateStatus`, mirroring `/update/status`.
- `lib/api/client.dart` + `lib/app_state.dart` — the five calls, and an
  `update_status` case in `lib/api/events.dart` feeding an `AppState` field.
- **Settings → Updates tab** (`settings_dialog.dart`): current version,
  "Check for updates" with inline result, checkboxes for automatic checks and
  automatic download, and the download/install button with a progress bar.
- **Banner** in `main_window.dart` when an update is available and not skipped:
  version, one-line notes, "Update now" / "Later" / "Skip this version". On
  Linux without `pkexec` it shows the verified file path and the command
  instead of an install button.
- The version string also lands somewhere permanently visible — the Settings
  header is enough. Nothing shows it today.
- `flutter analyze && flutter test`, with a widget test per screen against
  `test/fake_backend.dart`.

## 7. Tests

`tests/test_updater.py`, no network, using a keypair generated in the fixture:

- valid manifest and signature parse and compare correctly
- signature from a non-trusted key is rejected
- a single flipped byte in the manifest is rejected
- truncated, empty, and non-hex signatures are rejected
- `version <= current` offers nothing
- `version < last_seen_version` is rejected (rollback guard)
- `current < minimum_version` disables the one-click path
- a missing platform key yields "no update", not an error
- an artifact URL outside the host allowlist is refused
- an oversized manifest and an oversized artifact both abort
- **a SHA-256 mismatch discards the file and never calls `install()`** — the
  single most important assertion in the file
- `skipped_version` suppresses the banner but an explicit check still reports
- `__version__ == "0.0.0-dev"` disables the updater entirely

Full suite green per `.claude/rules/run-tests-after-changes.md`. The updater
guards no channel permission, so the three-layer permission rule does not
apply; the hash and signature checks are its equivalent, and they live in core
where no client can skip them.

## 8. Deferred: distribution over the mesh

The obvious question for this project is why an update has to come from the
clearnet at all, and the manifest design already answers most of it: the
manifest is self-authenticating, so **any** peer can serve it without being
trusted, and the SHA-256 inside it authenticates the binary regardless of who
supplied the bytes. Trust comes from the release key, never from the transport.

That makes a later phase cheap in design terms: a new control message carrying
the signed manifest (~1 KB — small enough for any link TrenchChat runs over)
lets an internet-connected peer tell an air-gapped one that a new version
exists, and a peer that already holds the installer can serve it over an RNS
`Resource` on a Link. The verification path is the one already built.

It is deferred because ~90 MB is not a LoRa payload, and because it wants
resumable transfer, a fairness policy on who serves whom, and a rate limit — a
feature in its own right, not a rider on this one.

## 9. Order of work

1. **Version identity and signed manifest.** `version.py`, CI baking, signing
   step, key setup, version shown in the UI. Lands alone, changes no runtime
   behaviour, and puts the manifest in place before anything reads it.
2. **Check and notify.** `UpdateManager.check`, the status/check/skip
   endpoints, settings, the banner. This alone answers "stop making me watch
   the repo", and carries none of the risk of writing to the install directory.
3. **Download, verify, install.** Windows and macOS one-click, Linux
   download-and-hand-off, the `.iss` relaunch change.
4. **Mesh distribution** (section 8), if and when it is wanted.

## 10. Open decisions

- **Does the update check default to on?** Recommendation: yes for the check,
  no for automatic download, with a first-run notice saying plainly that it is
  an HTTPS request to GitHub and where to turn it off. Defaulting it off means
  the users most in need of an update are the least likely to get one; the
  counter-argument — that a mesh app should make no clearnet connection it was
  not asked to make — is a legitimate reading and is the user's call.
- **How far to go on Linux in phase 3?** Recommendation:
  `pkexec` with a clean fallback, and treat an apt repository or an AppImage as
  its own piece of work rather than blocking Windows and macOS on it.
- **Intel Macs.** The CI matrix currently produces an arm64-only DMG. Either
  build universal binaries, or state in the manifest and the UI that x86_64
  macOS has no update path.
