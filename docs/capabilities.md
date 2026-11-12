# What your CATIA can do: the capability probe

Installations differ in what automation may do. Licences (V5 configurations
and add-ons, 3DEXPERIENCE roles), the release and the login state all matter,
and some refusals are blocking dialogs rather than errors. The capability
probe finds this out in steps of increasing risk, explains it in plain
language, and lets the feature-tree build refuse a tree before touching
CATIA.

```bat
python -m catia_mcp.tree capabilities                 read-only
python -m catia_mcp.tree capabilities --create        + a scratch part, closed without saving
python -m catia_mcp.tree capabilities --risky 3dpart  + a 3D Part (3DEXPERIENCE, see below)
```

The MCP tool `catia_capabilities` runs the read-only steps and adds the
cached results of earlier `--create` / `--risky` runs. It never creates
anything.

## Steps

| Step | What it does | Risk |
|------|--------------|------|
| environment | Windows only, no COM: `CATIA.Application` registered and its server, CATIA processes, main window enabled. A disabled main window means a dialog is open: the probe stops there and says so. | none |
| fingerprint | Attaches; platform, `SystemConfiguration` Version / Release / ServicePack, install path, session language. | none |
| licences | `SystemConfiguration` `ProductCount` (or `ProductNamesCount`), `GetProductNames`, `IsProductAuthorized` for a list of products; on V5 also the licence lists of Tools > Options > General > Licensing (read, never written). | read-only |
| session | 3DEXPERIENCE: the session services answer (logged in), and whether DatabaseSearch can clear criteria. | read-only |
| names | Factory method names on the active Part, if there is one. | read-only |
| create (`--create`) | A scratch V5 Part or 3DEXPERIENCE 3D Shape, built by the tree builder itself: a pad on XY, a pad from an offset plane, a loft. Closed without saving, only if it is still the active window. | creates, discards |
| risky (`--risky`) | `3dpart`: `PLMCreate "3DPart"`. `step_export` (V5): STEP export of the scratch part to a temporary file, deleted afterwards. | can block on a dialog |

Every step runs behind one function boundary and never raises: a failing
step is recorded in the report and the probe goes on.

## Statuses

- **verified**: a probe built it on this installation.
- **authorized**: the licence query says yes; not tried yet.
- **unavailable**: with the reason (a licence query that is known to
  understand its names said no, CATIA refused it in the scratch part, a
  method does not resolve, the platform has no automation route).
- **unknown**: nothing established. This is never a reason to refuse.

Capabilities: `part.create` (V5), `plm.create.3dshape` and
`plm.create.3dpart` (3DEXPERIENCE), `partdesign.basic`,
`partdesign.advanced` (lofts, ribs), `gsd.wireframe` (construction points,
lines and planes), `gsd.surface`, `measure`, `knowledge.rules` (V5),
`exchange.step.export`.

A licence "no" counts only once some product answered "yes": otherwise every
"no" may only mean that CATIA expects another name form (`PDG` or `PDG.prd`,
both are tried). On 3DEXPERIENCE the licence answers are recorded raw, since
no documented call returns roles; only `E70` / `VMX` (the entitlement field
reports name for scripted 3D Part creation) are interpreted.

## Cache

Results of `--create` and `--risky` are cached per user, per installation
(platform, version, release, service pack, install path), for 30 days:
`%LOCALAPPDATA%\catia-bridge\capabilities` on Windows (override with
`CATIA_MCP_CACHE_DIR`). A risky probe therefore runs once. The read-only steps
always rerun, because floating licences and logins change during the day, and
a fresh "unavailable" beats a cached "verified". `--refresh` ignores the
cache.

## Gating the tree build

`build` probes read-only (plus the cache) after attaching, then refuses the
whole tree, before creating anything, when a feature needs a capability the
probe found unavailable. Each feature's needs are in
`catia_mcp/tree/requirements.py`: every feature needs Part Design; lofts and
sweeps need its advanced solids; a sketch, cutting plane, revolve axis or
hole off the origin elements needs GSD wireframe; every part needs its
container (a V5 Part, a 3D Shape, or a 3D Part with `--plm-type 3DPart`) and
measurement. `check` stays offline and ungated.

## 3DEXPERIENCE defaults

`catia_new_part` with `plm_type` `auto` creates a 3D Shape. It tries a 3D
Part first only when a cached `--risky 3dpart` probe verified it on this
installation, because on many roles (the student Mechanical & Shape Designer
among them) the attempt opens a modal "Operation not authorized" dialog that
blocks automation until someone closes it.

## Needs a live check

Offline tests use doubles; only a real CATIA settles these.
`scripts/capability_probe.py` writes a JSON report with the raw answers:

1. Which of `ProductCount` / `ProductNamesCount` answers, and what
   `GetProductNames` and `IsProductAuthorized` return, on V5 and on
   3DEXPERIENCE (name form, and whether 3DEXPERIENCE answers at all, without
   a dialog).
2. Whether the V5 `GetLicensesList` calls answer as read here.
3. That the scratch build, close and discard leave no prompt and no document
   behind, on V5 and on 3DEXPERIENCE R2026x (French).
4. What `PLMCreate "3DPart"` does on the student role once the dialog is
   closed: which error comes back (the probe expects text such as "not
   authorized"; anything else is recorded as unknown).
5. The 3DEXPERIENCE process name and main window title, for the environment
   step and the dialog watchdog.
6. Whether DatabaseSearch criteria persist between searches in one session
   (no member to clear them is documented).
