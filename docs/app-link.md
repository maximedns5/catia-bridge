# The app link: a hosted app sends models to CATIA on the user's PC

A hosted app (in the cloud) wants to open a model in the user's CATIA as an
editable part. The app cannot open a connection to the user's PC, which sits
behind NAT and a firewall. So the bridge, on the PC next to CATIA, calls out:
it pairs once with a short code, then long-polls the app's server for jobs,
runs each one against the running CATIA, and posts the answer back.

```
 App page            App server (cloud)                    Bridge (CATIA PC)               CATIA
 "Link CATIA" ──▶ POST /api/catia-link/pair  → code
                                    ◀── POST …/device/claim {code}  → token (kept on the PC)
                                    ◀── GET  …/device/next  (Bearer, held ≤ 25 s)
 "Send to CATIA" ─▶ queue job {id, method, params, timeout, limit}
                                    ──▶ job
                                                                   build_tree ──COM──▶ new parts
                                    ◀── POST …/device/result {id, result | error}
 toast ◀─────────── answer
```

The bridge's side is `catia_mcp/link/`. The app's side is the app's own code;
`catia_mcp/link/relay.py` is a working reference for it, in one process and
for one account, used by the tests and for trying the chain on one PC.

## On the PC

### Installing: one line

The app's "Link CATIA" panel makes a pairing code and shows one line to paste
in PowerShell on the CATIA PC:

```bash
irm "https://APP/catia-link/install.ps1?code=ABCD2345" | iex
```

The script (`catia_mcp/link/install.ps1`) needs no administrator rights. It:

1. finds Python 3.10 or later, 64-bit (`py -3`, then `python`), or installs
   Python 3.12 for this user from python.org;
2. makes a private environment in `%LOCALAPPDATA%\catia-bridge\venv` and
   installs the bridge there with pip (stopping an older link first, so
   running it again upgrades);
3. pairs this PC with the code;
4. registers the link to start at every login and starts it now, with no
   window.

**What the app serves at `GET /catia-link/install.ps1?code=CODE`:** the
template with three placeholders filled in. `python -m catia_mcp.link
installer --server URL --code CODE [--package P]` prints it, and
`catia_mcp.link.installer.render()` does the same from Python:

- `__SERVER__`: the address the bridge keeps (the app's stable front, not a
  disposable worker);
- `__CODE__`: the pairing code, checked against the alphabet before it is
  written in. Serving the script does not consume the code; the bridge's
  claim does;
- `__PACKAGE__`: what pip installs. The default is this repository's main
  branch archive; the app can pin a release instead.

`render` refuses any value that could end the script's quoted strings, and
the script checks the code and the address again before it runs.

### The commands behind it

```bash
python -m catia_mcp.link pair --server https://APP --code CODE
```

```bash
python -m catia_mcp.link run
```

- `pair` trades the code the app shows for a token and saves both in
  `%APPDATA%\catia-bridge\link.json` (`$CATIA_LINK_HOME` overrides the folder).
  The token is never printed.
- `run` polls until stopped and logs to `link.log` next to it. It waits for
  CATIA; it never starts CATIA. Only one `run` takes jobs at a time
  (`link.lock`): a second one says so and stops.
- `start` runs it in the background (no window) and `stop` ends it.
- `autostart on` adds it to the user's Run key
  (`HKCU\Software\Microsoft\Windows\CurrentVersion\Run`, value
  `CATIA Bridge Link`, `pythonw -m catia_mcp.link run`); `autostart off`
  removes it.
- `status` shows the pairing, whether the link runs and starts at login, and
  whether CATIA is ready. `unlink` forgets the token.

To remove everything, in PowerShell:

```bash
& "$env:LOCALAPPDATA\catia-bridge\venv\Scripts\python.exe" -m catia_mcp.link uninstall
```

```bash
Remove-Item -Recurse -Force "$env:LOCALAPPDATA\catia-bridge", "$env:APPDATA\catia-bridge"
```

### Trying it without the app

The reference relay serves the installer too, so the one-liner can be tried
on the CATIA PC. Start the relay, then paste the line it prints in another
PowerShell (`--package` installs a branch that is not merged yet):

```bash
python -m catia_mcp.link relay --package https://github.com/maximedns5/catia-bridge/archive/refs/heads/BRANCH.zip
```

Or, from a checkout, in three terminals with CATIA open:

```bash
python -m catia_mcp.link relay
```

```bash
python -m catia_mcp.link pair --server http://127.0.0.1:8770 --code CODE
```

```bash
python -m catia_mcp.link run
```

```bash
python -m catia_mcp.link send tests\fixtures\trees\m4_loft_frustum.json
```

`send` plays the app: it computes the expected volumes with build123d when it
is installed, queues a `catia/build_tree` job and writes the answer to
`link_answer.json`. `send --ping` and `send --check TREE.json` send the other
methods.

## The protocol (version 1)

Every address below is relative to the server address the bridge keeps. The
bridge accepts `https://` addresses, and `http://` only to this PC.

### Device routes (called by the bridge)

| Route | Request | Answers |
|---|---|---|
| `POST /api/catia-link/device/claim` | `{"code": "ABCD2345"}` | `200 {"token"}`; `404` unknown, expired or used code |
| `GET /api/catia-link/device/next` | `Authorization: Bearer <token>` | `200` a job; `204` nothing within the hold; `503` not ready yet (`Retry-After`); `404` unknown token |
| `POST /api/catia-link/device/result` | Bearer; `{"id", "result"}` or `{"id", "error", "error_code"}` | `200 {"accepted"}`: false when nobody waits any more |

The server's duties:

- **Pairing codes:** 8 characters from `23456789ABCDEFGHJKMNPQRSTUVWXYZ`,
  single use, valid 10 minutes, one live code per account. Compare in
  constant time; the bridge sends the code uppercased.
- **Tokens:** random, at least 32 bytes. Store only `sha256(token)` with the
  account. Without a valid token every device route answers `404`, like a
  route that does not exist.
- **The hold:** keep `next` open up to 25 s when the queue is empty. The
  bridge's HTTP timeout is 60 s.
- **"Not known yet" is not "unknown":** while a freshly started server has not
  restored the account's links, answer `503` with `Retry-After`, never `404`.
  On `404` the bridge tells the user to pair again; on `503` and network
  errors it retries after 3 s without alarming anyone.
- **Moving:** a `next` answer may carry `X-CATIA-Link-Server: <new address>`.
  The bridge then moves there and saves it (an `https://` address only).
- **Connected:** an account counts as connected while its bridge polled
  within the last 35 s. The bridge keeps polling while a build runs. Refuse
  to queue a job for a bridge that is not connected.
- **One queue per account**, and one per kind of link if the app has others:
  a CATIA job must never reach another program's link, nor the reverse.

### Jobs

```jsonc
{ "id": "6f1c…",                 // unique, at most 128 characters
  "method": "catia/build_tree",
  "params": { … },
  "timeout": 298,                // the bridge works within this
  "limit": 300 }                 // the app waits this long
```

Send a `timeout` 2 s below the `limit`, so the bridge's own, more precise
message is the one the user reads. The answer is `{"id", "result"}`, or
`{"id", "error", "error_code"}` when the job could not run.

| Method | Params | Result |
|---|---|---|
| `catia/ping` | none | `{"bridge", "protocol", "methods", "catia", "ready", "platform", "documents", "message", "busy"}` |
| `catia/capabilities` | none | the capability probe's report (`docs/capabilities.md`) |
| `catia/check_tree` | `{"tree", "plm_type"?}` | `{"accepted", "reasons": [{"reason", "where"}], "message", "note"}`; nothing is created |
| `catia/build_tree` | `{"tree", "expected"?, "plm_type"?, "check_editability"?}` | the build report (below) |

- `tree` is a feature tree, version 1 (`docs/feature-tree.md`).
- `expected` is `{part id: {"volume_mm3", "cog_mm"?}}`, from the app's own
  kernel. With it, each part's volume is compared with the source model's.
  Without it, each feature is still checked, but not the parts, and the report
  says so in `notes`.
- `plm_type` is `3DShape` (the default) or `3DPart`, on 3DEXPERIENCE.
- `check_editability` (default true) changes one extrude's length, checks
  the volume moves, and restores it.
- Give a build a generous `limit`: a few seconds per part on R428, more for
  fillets and lofts. A build needs a `timeout` above 15 s; 10 s of it is kept
  back for the report.

The build report is the one `python -m catia_mcp.tree build` writes:

```jsonc
{ "success": true, "native_history": true, "editable": true,
  "parts": 4, "features": 9, "elapsed_s": 11.8,
  "parts_detail": [ { "id": "/Demo2/Housing", "catia_name": "Housing",
                      "volume_mm3": 68131.7169, "expected_volume_mm3": 68131.7169,
                      "features": [ {"id": "extrude1", "type": "extrude",
                                     "catia": ["Extrusion.1"], "ok": true} ],
                      "editable": true, "editability_note": "…" } ],
  "error": { "part", "feature", "message", "note" } }   // when success is false
```

- **Refused:** `"refused": true` with `reasons` and `message`, the text to
  show the user. Nothing was created in CATIA.
- **Failed mid-build:** `success` false with `error.part` and
  `error.feature`. The CATIA document may hold a partial model; the bridge
  never retries by itself.
- **Stopped by a dialog:** `error.code` is `BLOCKED_BY_DIALOG` and
  `error.dialog` holds the dialog's title and text, read off the screen.

`error_code` values in a job's answer: `BAD_REQUEST` (the job or its params
are malformed), `UNKNOWN_METHOD`, `BUSY` (another job holds CATIA; a ping is
still answered, with `busy` true), `CATIA_NOT_RUNNING`, and the session codes
(`BLOCKED_BY_DIALOG`, `SESSION_BLOCKED`...) when a dialog or a hang stopped
a call outside a build.

## Security

- **Data, never code.** No method runs anything it was sent. A job naming an
  unknown method is refused. There is no "run a script" method, and adding
  one would let whoever controls the app's server run code on the user's PC.
- **No inbound port.** The bridge only makes outbound HTTPS calls to the
  address it was paired with (or one that address moved it to).
- **The token** lives only in `link.json` on the PC (readable by the user
  alone where the platform allows it) and as a SHA-256 digest on the server.
- **The app's AI agent has no access to CATIA.** Only user actions (Send to
  CATIA, Link CATIA) should queue jobs.
- **CATIA is never started by the bridge.** On 3DEXPERIENCE a fresh start
  needs an interactive login; the user opens CATIA, the bridge attaches.

## Constants

| Constant | Value | Side |
|---|---|---|
| hold of `next` | 25 s | server |
| bridge HTTP timeout for `next` | 60 s | bridge |
| connected grace | 35 s | server |
| pairing code lifetime | 600 s | server |
| retry after `503` or a network error | 3 s (or `Retry-After`) | bridge |
| retry after `404` | 10 s | bridge |
| job `timeout` | `limit` − 2 s | server |
| maximum job size read | 64 MiB | bridge |
