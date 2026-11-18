# Materials, appearance, properties and Knowledgeware

Once a part is built and checked, the tree replay can finish it as a designer
would: its colour, its material and mass, the product properties the bill of
materials reads, and its parameters in a parameter set with a design table
for its variants. Each step is optional, recorded in the part's report, and
never fails the part: the geometry stays whatever happens to its properties.

Code: `catia_mcp/tree/properties.py` (contract, orchestration),
`appearance.py`, `materials.py`, `attributes.py`, `knowledge.py`, all in
`catia_mcp/tree/`. Probe: `scripts/probe_properties.py`.

## Commands

```bat
python -m catia_mcp.tree build TREE.json --report out.json
python -m catia_mcp.tree build TREE.json --properties on --material-catalog C:\path\Catalog.CATMaterial
python scripts\probe_properties.py --create --report properties_probe.json
```

- `--properties auto` (default): parts whose tree carries a property field
  (below) get the step. `on`: every part, which at least sets its part number
  (its name) and a `TreeId` user property (its tree id). `off`: never.
- `--material-catalog`: the V5 material catalog. By default every
  `materials\*.CATMaterial` under the startup folders (`CATStartupPath`).
- `--sheet-dir`: where the bridge writes design table sheets (default: the
  temporary folder).
- `--checks auto|on|off`: Knowledge Advisor checks. `auto` creates them only
  when the capability probe found the Knowledge Advisor licence (`KWA`).
- With `--step MODEL.step`, a part without a `color` takes its STEP
  instance's colour (XCAF, sRGB), noted as `"source": "step"`.

`check` validates the fields like the rest of the tree: a malformed field
refuses the tree, with the reason (`part properties: ...`).

## Contract additions requested from the app

All optional and additive to tree v1; existing trees are unchanged.

```json
{
  "id": "/Demo/Plate", "name": "Plate", "features": [...],
  "color": [0.8, 0.1, 0.1],
  "material": {"name": "Steel", "density_kg_m3": 7850,
               "catalog_name": "Steel", "family": "Metal"},
  "properties": {"part_number": "PLATE-001", "revision": "A",
                 "definition": "Mounting plate", "nomenclature": "Plate",
                 "description": "60 x 40 x 8 plate", "source": "made",
                 "user": {"AppModelId": "demo-plate"}},
  "parameters": [{"name": "Wall", "type": "length", "value": 2.5,
                  "comment": "minimum wall"}],
  "configurations": [{"name": "Thin", "params": {"Extrude1_height": 6, "Wall": 2}}],
  "checks": [{"name": "WallCheck", "parameter": "Wall", "min": 1, "max": 10}]
}
```

| Field | Meaning |
|---|---|
| `color` | sRGB, three numbers 0..1 (the convention of the STEP colours). Also allowed on any feature |
| `material.name` | The material's name in the model; `density_kg_m3` (> 0) gives the expected mass |
| `material.catalog_name`, `family` | Optional: the name (and family) in CATIA's catalog, which is localised (`Acier` in a French catalog). On 3DEXPERIENCE, the Core Material's title to search for |
| `properties` | `part_number` (default: the part's `name`), `revision`, `definition`, `nomenclature`, `description`, `source` (`made`, `bought`, `unknown`), `user` (name to text, number or boolean) |
| `parameters` | Designer parameters: `name` (letters, digits, `_`), `type` (`length` mm, `angle` deg, `real`, `integer`, `boolean`, `string`), `value`, optional `comment` |
| `configurations` | Variants: a `name` and `params`, each naming a tree parameter or a script parameter as the builder names it (`<feature name>_<script argument>`, for example `Extrude1_height`; see feature-tree.md, Script parameters) |
| `checks` | Bounds on a numeric parameter: `name`, `parameter`, `min` and/or `max` (in the parameter's unit), optional `comment`. The bridge writes the check's expression: the tree never carries rule text |

## What the bridge does

**Appearance** (`appearance.py`). The colour goes on each final body (the
PartBody in a one-body part) through the selection:
`VisProperties.SetRealColor r, g, b, 1`, then `GetRealColor` reads it back
(the out-parameter detour). If a body refuses, the part object is coloured.
A feature's colour goes on the CATIA features the builder made for it, found
by name (`Part.FindObjectByName`). Same calls on V5 and 3DEXPERIENCE.

**Material and mass** (`materials.py`).
- V5, first the catalog: the material named `catalog_name` (else `name`) is
  looked up in each catalog, read without a window (`Documents.Read`). It is
  applied as a copy (link mode 0) by the material manager,
  `GetItem("CATMatManagerVBExt")` on the part document's product, then on
  the part (`ApplyMaterialOnPart`, else `ApplyMaterialOnBody`), and read back
  (`GetMaterialOnPart`). If the catalog's density (`SAMDensity`) differs from
  the tree's, the applied copy gets the tree's, noted.
- V5, else a new material: a `CATMaterial` document, its first material
  renamed and given the tree's density, applied the same way, the document
  closed without saving.
- 3DEXPERIENCE: materials are PLM objects (Core Material). With a
  `catalog_name`, the database is searched for it (`DatabaseSearch`, types
  `CATMatReference`, `MATReference`) and applied with the manager if the part
  has one. Neither the type names nor the apply call are documented for R428:
  `scripts/probe_properties.py` records what exists.
- Fallback, both platforms: parameters in the part's set, `Material`
  (string), `Density` (a DENSITY dimension, kg/m3) and `Mass` (a MASS
  dimension driven by `smartVolume(body) * Density`). The report's
  `material.route` is then `parameters` and its status `fallback`.
- Mass: the sum of each final body's Inertia mass (catia_mcp/measure.py),
  before and after, compared with volume x density within 1e-3. With the
  fallback, CATIA's inertia still uses its default density (1000 kg/m3), so
  the `Mass` parameter is checked instead.

**Product properties** (`attributes.py`).
- V5: on the part document's root product, `PartNumber`, `Revision`,
  `Definition`, `Nomenclature`, `DescriptionRef`, `Source` (unknown 0, made 1,
  bought 2) and user properties (`UserRefProperties.CreateString`,
  `CreateReal`, `CreateBoolean`, or the existing one's `Value`), each read
  back. A part number CATIA renames (already used in the session) is
  reported in `kept_otherwise`.
- 3DEXPERIENCE: on the 3D Shape's PLM object, `V_Name` (the part's name),
  `V_description` and `PLM_ExternalID` (the part number; the platform often
  refuses it). Fields with no attribute, or refused, become string parameters
  in a `Properties` parameter set (`PartNumber`, `Revision`, `Definition`,
  `Nomenclature`, `Source`, the user properties, `TreeId`).

**Knowledgeware** (`knowledge.py`).
- Parameters go in a parameter set named for the part
  (`Parameters.RootParameterSet.ParameterSets.CreateSet`, then
  `DirectParameters.Create*`), with their comments. Length and angle are
  dimensions valued with their unit (`2.5mm`, `1.5deg`), as the script
  parameters are (live on R428). If CATIA refuses the set, they go to the
  part's root, noted.
- Design table, when the tree has `configurations`: the bridge writes a
  tab-separated text sheet (no Excel needed), first an `As built` row with the
  values the part was built with, then one row per configuration.
  `Relations.CreateDesignTable` copies it into the part, each column is
  associated to its parameter, and every configuration is activated in turn
  (update, volume, values read back). Back on `As built` the part must
  measure what it did; otherwise the table is removed and every value put
  back. A configuration that does not build is noted.
- Checks: `Relations.CreateCheck` with `Wall >= 1mm and Wall <= 10mm`,
  written from the bounds. Only with `--checks on`, or `auto` and the
  Knowledge Advisor licence found: a refused licence can open a dialog.

The script parameters (`Extrude1_height`...) stay in the part's root for now:
their formulas name them by their short name, which is validated live there.

## Report

Each part in `parts_detail` gets a `properties` section: `ok`, then one
entry per step that ran (`parameters`, `attributes`, `material`,
`appearance`, `design_table`, `checks`), each with a `status` (`ok`,
`partial`, `fallback`, `skipped`, `failed`) and the `routes` tried
(`{route, ok, error}`). `material.mass` holds `expected_kg`, `measured_kg`
and `volume_mm3`. The command line prints one line per part, for example
`properties ok: colour (204, 26, 26), material Steel (catalog), mass 0.1489
kg (expected 0.1489), 6 attribute(s), 6 parameter(s) in Plate, design table
ok, checks skipped`.

## Live on 3DEXPERIENCE R428 (French, student role)

`p1_properties_plate.json` built exactly (18973.8 mm3), then:

| Step | Result |
| --- | --- |
| Colours | Body and hole feature coloured and read back (`SetRealColor`) |
| Parameters | Set `Plate` created, six typed parameters with comments, read back in the French format (`2,5mm`) |
| PLM attributes | `V_Name`, `V_description`, `PLM_ExternalID` set on the part's PLM object; the V5 product fields went to string parameters |
| Material | The `CATMatReference` search ran but found no "Steel" (`MATReference` is refused as a type); kept as `Material` / `Density` / `Mass` parameters, `Mass = smartVolume(...) * Density` = 0.149 kg |
| Design table | `CreateDesignTable` answers E_NOTIMPL. The bridge now also tries a linked sheet and the horizontal layout, then reports `unavailable` (not failed) and leaves the sheet for a manual import |
| Checks | Skipped: Knowledge Advisor not proven available |

## Coverage

Status: offline-tested against fakes (`tests/test_properties.py`) and run
live on 3DEXPERIENCE R428 (student role) with `p1_properties_plate` and
`scripts/probe_properties.py`; CATIA V5 only against the fakes.

| Area | CATIA V5 | 3DEXPERIENCE | Status |
|---|---|---|---|
| Appearance (colour) | Body, part and feature colours by `SetRealColor`, read back | Same calls on the editor's selection | Live on R428: body and feature colours set and read back (a part-level colour reads back black) |
| Materials | Catalog, else a new material document; density from the tree | Core Material search + manager (undocumented); else parameters | V5 implemented (fake-tested). Live on R428 no material service is reachable and the Core Material search finds none: kept as parameters |
| Mass properties | Inertia mass vs volume x density | Same (InertiaService); with the fallback, a `Mass` parameter | Live on R428: `Mass` = 0.149 kg by formula; the inertia uses CATIA's default density (7770 kg/m3) without a material |
| Properties / attributes | PartNumber, Revision, Definition, Nomenclature, DescriptionRef, Source, user properties | V_Name, V_description, PLM_ExternalID; the rest as string parameters | Live on R428: `V_Name`, `V_description`, `PLM_ExternalID` set; revision and usage refused, kept as string parameters |
| Parameters | Parameter set named for the part, six types, comments | Same | Live on R428: a set named for the part, six types with comments, read back |
| Formulas | Script parameters (feature-tree.md); `Mass = smartVolume * Density` | Same | Live on R428, the mass formula included |
| Design tables | From a sheet the bridge writes, every configuration tried and restored | Same call, if the role allows it | V5 implemented (fake-tested). Live on R428 all three `Relations` routes answer E_NOTIMPL: reported `unavailable`, the sheet left for a manual import |
| Rules | Not from the tree (it carries data, not rule text); the probe tries one with `--kwa` | Same | Probe only; no Knowledge Advisor licence on R428 |
| Checks | From bounds, Knowledge Advisor licence required | Same | Implemented behind `--checks`; skipped live (no Knowledge Advisor licence) |
| Reactions | Not created; the probe reads whether `Relations` exposes a creator | Same | Probe only |
| Knowledge patterns | Not created (Knowledge Advisor, and code-like) | Same | Not done |
| Catalogs | Material catalogs read; no catalog documents written | PLM libraries not used | Read only |
| Publications | The probe tries `Publications.Add` / `SetDirect` on the product | The probe reads `Part.Publications` | Live on R428: none on the Part; a part reference's `Publications` only reads |

## Live probe

`scripts/probe_properties.py` attaches to a running CATIA (it never starts
one), builds a 60 x 40 x 8 plate in a new part with `--create`, and tries
every route above on it, each recorded on its own: licences (KWA, KE1, PKT,
Part Design), colours read back, the inertia mass before and after a
material, material catalogs and managers (V5), candidate material services
and a Core Material search (3DEXPERIENCE, `--material TITLE`), product
fields and PLM attributes (set, read, restored), a parameter set with every
type, DENSITY and MASS dimensions and a `smartVolume` formula, design tables
from a tab-separated `.txt` (with and without units) and from a `.csv`, the
`Relations` creators, and with `--kwa` a check and a rule. It writes a JSON
report and saves nothing.

## Not done / next

- 3DEXPERIENCE Core Material: the apply route, once the probe shows what
  this release exposes.
- The script parameters in the part's parameter set (their formulas would
  then name them by `GetNameToUseInRelation`).
- Part-level properties on the product built by `--assembly product` (each
  instance's part already carries them in its own document).
- Rules, reactions and knowledge patterns from the tree: they need a data
  form (like the checks' bounds) before the bridge can write them.
