# Clashes

Once an assembly is put together, a designer checks that no two parts take
the same space, that the parts meant to touch do, and that the parts meant
to move keep their play. The bridge checks every pair of instances of the
assembly it rebuilds, offline, and on CATIA V5 also with CATIA's own clash.

```bash
python -m catia_mcp.tree clashes MODEL.tree.json --step MODEL.step --report clashes.json
python -m catia_mcp.tree clashes MODEL.tree.json --step MODEL.step --clearance 0.5
python -m catia_mcp.tree build MODEL.tree.json --assembly product --step MODEL.step --clashes catia
```

## What is checked

For every pair of instances (`catia_mcp/tree/clashes.py`):

| Kind | When | Value | Location |
|---|---|---|---|
| `interference` | the two solids share a volume over 0.001 mm3 | that volume (mm3) | its centre of gravity |
| `contact` | their distance is at most 0.001 mm and they share no volume | the distance (0 mm) | the centre of the edges where they touch |
| `clearance` | their distance is over 0.001 mm and under `--clearance` | the minimum distance (mm) | the middle of the closest points, both given in `points` |

A pair further apart is not reported. `--clearance 0` (the default) turns
clearances off. Mated faces are contacts: a bolt head on a plate, a pin in a
hole of its own diameter, a box on its base. A contact is expected; an
interference is a design error.

Every pair is reported once, by its worst kind: a pin that interferes with
its plate and also rests on it is an interference.

## How

- **Shapes**: the STEP file's (each part read once, in its own frame, moved
  to every instance, as for the assembly constraints). Without `--step`, the
  tree's build123d reference, each part where the tree puts it.
- **Boxes first**: a pair whose bounding boxes (enlarged by half the
  clearance) do not meet is not measured.
- **Distance**: the exact minimum distance between the two solids
  (OpenCascade `BRepExtrema_DistShapeShape`; 0 when they overlap or one is
  inside the other).
- **Volume**: for a pair at distance 0 only, the common volume
  (`BRepAlgoAPI_Common`).

Pairs are taken in the plan's instance order and every value is rounded (6
decimals, volumes 4): the same model gives the same report. 100 cubes in a
grid, touching their neighbours, are checked in about 2 s.

Needs OCP (installed with build123d: `pip install -e ".[differential]"`).

## Report

`python -m catia_mcp.tree clashes` writes it alone; `build --assembly
product|adopt` puts it in `report["assembly"]["clashes"]`:

```json
{
  "ok": true, "method": "offline", "source": "step",
  "instances": 2, "clearance_mm": 0.0,
  "tolerances": {"contact_mm": 0.001, "volume_mm3": 0.001},
  "pairs": 1, "pairs_measured": 1,
  "counts": {"interference": 1, "contact": 0, "clearance": 0},
  "interference": true,
  "clashes": [
    {"first": "/PinInterference/Plate", "second": "/PinInterference/Pin",
     "first_name": "Plate", "second_name": "Pin",
     "kind": "interference", "value": 133.5177, "unit": "mm3",
     "location": [10.0, 5.0, 5.0]}
  ],
  "elapsed_s": 0.006
}
```

`ok` is false when a pair could not be computed (listed in `errors`), not
when parts clash. A clash never fails the build: it is reported.

## In the build: `--clashes`

- `offline` (the default with `--assembly product|adopt` when OCP is
  installed): the analysis above, before CATIA is touched.
- `catia`: the offline analysis, then CATIA's own clash, compared.
- `none`: nothing.

### CATIA V5: DMU Space Analysis

After the product is assembled, and after the drawings (a licence dialog
here must not stop anything else):

1. `SystemConfiguration.IsProductAuthorized` for `SPA` and `SP1`; when a
   product every Part Design seat has answers yes and neither does, the
   route stops here: "unavailable", nothing asked of CATIA.
2. `Product.GetTechnologicalObject("Clashes")`, `Clashes.Add`;
   `ComputationType` between all; `InterferenceType` contact, or clearance
   with `Clearance` when `--clearance` is given; `Compute`.
3. `Clash.Conflicts`: each conflict's `Type`, `Value`, `FirstProduct` and
   `SecondProduct`, named back to the plan's instances through the names the
   assembler gave them (`Bolt.1`).
4. The comparison: the pairs both found with the same kind (`agree`), and
   each difference. CATIA's clash value is a penetration depth, the offline
   one a volume: kinds are compared, not values.

The enum values come from the type library when CATIA gives it, else from
the V5 documentation's order (the report says which). The clash stays in
the product (Applications, Interference) for the designer to look at.

The whole route runs under the dialog watchdog: a licence dialog, a missing
DMU Space Analysis or any refusal gives `"status": "unavailable"` with the
reason (and the dialog's text), and the build stays a success.

### 3DEXPERIENCE

No clash API is known through automation. `--clashes catia` reports the
CATIA route as unavailable and the result is the offline analysis, exact
from the STEP file the product was imported from (`--assembly adopt`).

`scripts/probe_clashes.py` looks for one on the active product (the STEP
imported by hand), each step recorded in a JSON report:

- the enums whose names contain clash, interference, conflict, clearance or
  penetration;
- the members of the root occurrence, its reference, the first instance, the
  editor and the application whose names suggest a clash or a service;
- `GetItem("Clashes")`, `GetItem("CATClashes")` and other names on the root
  reference, the root occurrence and the first instance;
- `GetTechnologicalObject(...)` where it exists;
- `GetService(...)` on the editor and `GetSessionService(...)` candidates;
- the offline analysis of the same model, to compare with;
- with `--create` only: a clash in the first collection found with `Add`,
  computed and read back (removed again when the collection has `Remove`).

```powershell
python scripts\probe_clashes.py --window "Bracket A:4" --report clashes_probe.json
```

`--window` activates that one window (as CATIA's Window menu names it);
activating windows in turn hung CATIA live, so the probe never does.

## Status

| Route | Status |
|---|---|
| Offline | Tested on the assembly fixtures: bracket (a contact; the turned bolt 7 mm away), pin_plate, box_base, bolted_plates (contacts only), pin_interference (133.5177 mm3), shaft_bushing (a 0.2 mm clearance, two contacts) |
| V5 DMU Space Analysis | Against a fake CATIA only |
| 3DEXPERIENCE | Offline; `scripts/probe_clashes.py` not yet run live |

The fixtures `pin_interference` and `shaft_bushing` are written by
`scripts/make_clash_fixtures.py`, like the mate fixtures.
