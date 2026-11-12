# The STEP fallback: solids without history

Some features have no native rule in the tree replay. A `base_feature` is a
solid that build123d only keeps as a B-rep (an imported STEP, raw OCP, a
shear). Other features have no native rule yet (a draft, a thicken, text). The STEP fallback
brings such geometry into CATIA anyway. A STEP file holds the solid, CATIA
imports it, and the solid is pasted into the part as a **dead body**: an
isolated solid with no history, added to or removed from a native body. The
native features before and after it stay editable. The dead body itself does
not follow their edits.

The fallback is opt-in and reported. The tree builder uses it only with
`--step-dir`, and the build report lists the dead bodies apart from the
native features.

## What runs where

| Step | CATIA V5 | 3DEXPERIENCE |
|------|----------|--------------|
| Import a STEP file | Scripted: `Documents.Open("x.stp")` opens it as a new Part document. Needs the STEP interface licence (ST1) | **Not scriptable**: no import service is documented, and macro recording does not capture the Import command. The user imports by hand |
| Find the imported shape | The document the import opened | `adopt`: the window the user names (title or part of it) |
| Paste as a body without history | `Selection.PasteSpecial "CATPrtResultWithOutLink"` | Same; live-checked on R428 SP6 |
| Add / Remove into a body | `AddNewAdd` / `AddNewRemove` / `AddNewIntersect` | Same |
| Tree builder with `--step-dir` | Yes | Refused by preflight |

## Import (CATIA V5)

`catia_mcp/step.py` `StepExchange.import_file`:

1. It refuses anything but an existing `.stp` / `.step`.
2. It copies the file to a fresh temporary folder. The V5 guide warns that a read-only input stops the import on a dialog.
3. It sets `DisplayFileAlerts` to False, calls `Documents.Open`, and restores the previous setting. It notes whether the document count grew, that is, whether this import opened the document.
4. A file holding an assembly opens as a CATProduct. That is refused, and the document is closed.
5. It checks for the **failed-import signature**. A failed STEP import still opens. Per the V5 guide it leaves an empty PartBody, and geometrical sets named `FaceKO...` hold the faces that could not be transferred. The import is refused when:
   - PartBody has no feature;
   - a `FaceKO` set exists;
   - the body cannot be measured or has no volume (surfaces only);
   - the volume misses `expected_volume_mm3`, relative tolerance 1e-4.

   On refusal the document is closed. CATIA writes the details to `<file>.rpt` and `<file>.err` in its CATReport folder.
6. It measures the volume, area and centre of gravity.

The bridge never saves the imported document. It closes it only if it opened
it.

## 3DEXPERIENCE: import by hand, then adopt

`import_file` fails with `UNSUPPORTED_CAPABILITY` and these steps:

1. In the top bar, click **+** (Add, "Ajouter") > **Import** ("Importer").
2. Set Format to STEP and Source to File on disk, then pick the file.
3. Click **Validate Import**. CATIA saves the imported 3D Part to the database, so the role needs the Converter for STEP app and write access to the collaborative space. It titles the part with the STEP product name plus a date suffix.
4. Open it in its own window (`catia_search_database` / `catia_open_from_database` find it by title), check that it shows a solid, and call `catia_paste_dead_body` with that window's title as `source`.

`StepExchange.adopt(title)` activates the matching window and checks the
shape like an import. It never closes that window. The French menu labels
in step 1 are unverified.

## Paste as result

`StepExchange.paste_as_result` copies the source's main body, selects the
target part, and calls `PasteSpecial "CATPrtResultWithOutLink"`. CATIA
creates a new body holding an isolated solid. The source is not linked, so
closing it changes nothing.

The new body is found by comparing the part's bodies before and after. Any
count other than one fails with `PASTE_FAILED`. The body is renamed
`STEP_<name>`. Its volume must match the source's, or the paste fails with
`WRONG_GEOMETRY_EFFECT`.

`combine` then adds, removes or intersects it into a target body. The result
is checked against the volumes that operation can give. An empty target
body, such as a new part's PartBody, counts as 0 mm3.

## MCP tools

| Tool | What it does |
|------|--------------|
| `catia_import_step` | V5: imports and checks a STEP file as a new document, and returns its name, volume, area and centre of gravity. The document stays open and unsaved. 3DEXPERIENCE: `UNSUPPORTED_CAPABILITY` with the manual steps |
| `catia_paste_dead_body` | Pastes a source's solid into a target part as a body named `STEP_*`, then adds, removes or intersects it into a body (`operation`, `into_body`; `none` keeps it apart). V5: names are document names, and the target defaults to the document active before the last import. 3DEXPERIENCE: names are window titles, and the target is required. `close_source` closes only a document `catia_import_step` opened |
| `catia_list_dead_bodies` | Lists the active part's bodies and which are dead. A dead body is either named `STEP_*`, or all its features are isolated solids (COM type `Solid`, or named `Solid.N` / `Solide.N` when the type is unreadable). A body with native features and an isolated solid is reported as partly dead |

Nothing is saved.

## In the tree replay: `--step-dir`

```bat
python -m catia_mcp.tree check TREE.json --step-dir STEPS
python -m catia_mcp.tree build TREE.json --step-dir STEPS --platform v5
```

`STEPS` holds one folder per part. The folder name is the part id with
anything other than letters, digits, `.`, `_` and `-` replaced by `_`, so
`/Demo/Plate` becomes `Demo_Plate`. The files are:

- **`<feature id>.step`** (or `.stp`): the solid of a `base_feature`, applied with the feature's own `operation`. `new_body` makes the pasted body the feature's body. `join` / `cut` / `intersect` combine it into its one target body.
- **`<feature id>.add.step`** and/or **`<feature id>.remove.step`**: a **delta** for any feature that modifies exactly one known body. This covers a `base_feature` that joins or cuts, and unsupported features such as `draft` or `thicken`.
  - `add` is the material the feature adds (after - before).
  - `remove` is the material it removes (before - after).
  - The builder adds, then removes. Adding then removing gives exactly the body after the feature.
  - Each delta must move the body's volume by exactly its own volume. This catches CATIA booleans that go wrong on the delta's coincident faces.

### Preflight

`check` accepts an unsupported feature only when its files exist and the
platform can import them. Otherwise the refusal says why: no file (with the
folder looked in), 3DEXPERIENCE, or a feature that creates bodies, which a
delta cannot express.

`check` without a connection does not know the platform with `--platform
auto`. It accepts the files, and `build` checks again once connected.
Without `--step-dir` nothing changes: such features are refused as before.

### Build report

- Each STEP-built feature has `"dead": true` and the dead body names in `catia`.
- `dead_bodies` lists, per part and feature, the role, the file, the CATIA body and its volume.
- `native_history` is `"partial"` when there are dead bodies.
- The per-part volume check compares with the reference plus the STEP files (see below).

## Writing the files (development side, build123d)

`catia_mcp/tree/step_fallback.py` writes the files for a tree:

```bat
python -m catia_mcp.tree.step_fallback TREE.json MODEL.step --step-dir STEPS
```

The reference builds the bodies as they are just before each unsupported
feature. The model's final solid gives the body just after it. This is exact
only when nothing later in the part touches that body and it is the part's
only body; the tool refuses anything else. From Python, `write_fallback(tree,
dir, after_solids({(part id, feature id): solid}))` takes the solid after
each feature directly.

Files are AP214 in mm, the schema V5's ST1 reads. The header timestamp is
fixed, so the same model gives the same bytes. Each solid must be valid,
with edge tolerances under 1e-3 mm. `expected_with_steps(tree, dir)` (also
`reference --step-dir`, and `build --step-dir` with build123d installed) is
the reference with the files standing in. That is the expected volume of
the build.

`tests/fixtures/step/` holds a probe set made this way:
- a 100 x 60 x 40 mm block with R5 vertical edges, 239141.5927 mm3 (`fillet_block.step`);
- the same block as a tree, a pad plus a `draft` feature standing in for the rounds (`fillet_block.tree.json`; a feature with no native rule, so its result comes from STEP);
- that feature's remove delta, 858.4073 mm3 (`steps/Probe_FilletBlock/draft1.remove.step`).

## Live checklist

`scripts/step_probe.py` runs the scripted parts and writes
`step_probe.json`. Nothing is saved. Send the JSON back with the answers to
the manual items.

**3DEXPERIENCE (R2026x, French UI, student role):**

1. Run `python scripts/step_probe.py`. Check that it records whether `Documents` resolves and that the import is refused with the manual steps.
2. In the top bar, click + (Ajouter) > Importer. Is STEP offered in Format? If not, the role lacks Converter for STEP: note it and stop here.
3. Import `tests/fixtures/step/fillet_block.step` from File on disk. Note the object type created (3D Part, or Physical Product with a 3D Shape), its title and suffix, and the tree content: is it one isolated solid, and what is it called (`Solide.1`?). Was it saved to the database without asking? Note the exact French labels of each menu and button.
4. Open the imported part in its own window, create a 3D Shape (`catia_new_part`) in another, then run `python scripts/step_probe.py --adopt "<imported title>" --target "<new shape title>"`. Expect a pasted volume of 239141.59 mm3, a body `STEP_probe`, and `catia_list_dead_bodies` reporting it.
5. In CATIA, pocket a sketch through the dead body on an origin plane, and check that the volume changes as expected (native on top of dead).
6. Try Exporter on the 3D Shape to STEP, and note whether the role allows it.
7. Search `win_b64\code\bin\DSYAutomation.chm` for "STEP", "Import", "Exchange" and "XCAD". Any import service would make step 2 scriptable.

**CATIA V5 (when a V5 seat with ST1 is available):**

1. Run `python scripts/step_probe.py`. It covers:
   - the import of the fixture block: the document type, the main body's features (`Solid.1` / `Solide.1`?), the geometrical sets (one empty?), and the volume;
   - `DisplayFileAlerts` restored;
   - the paste as result into a new part and the Add into its empty PartBody;
   - closing the STEP document, then an update with the volume unchanged (no link);
   - the dead bodies;
   - the STEP settings controller name, which is only guessed (`CATSdeStepSettingCtrl`);
   - the fixture tree replayed with its remove delta. Its volume must be 239141.59 mm3, and a boolean failure on coincident faces shows up here.
2. Import a deliberately broken STEP (for example, edit a face out of the file) and check that it fails with `STEP_IMPORT_FAILED` and its `FaceKO` sets. Keep the `.rpt` / `.err` files from CATReport.
3. If the settings controller was not found: Tools > Macro > Start Recording, change one STEP option, stop, and send the `SettingControllers.Item("...")` string.

## Not validated live

- **V5:** all of it. In particular: `Documents.Open` of a .stp from automation with `DisplayFileAlerts` off, the exact failed-import signature, the isolated solid's COM type name (`Solid`), `PasteSpecial "CATPrtResultWithOutLink"` adding exactly one body, and the Add into an empty PartBody.
- **3DEXPERIENCE:** the paste is live-checked on R428 SP6 between two 3D Shapes. Not checked: adopting a hand-imported STEP part's window, whether the student role can import STEP, and the French labels.
- **Delta bodies:** CATIA booleans of solids sharing faces with the body (fillet and chamfer deltas are exactly that). The builder fails if the volume does not move by the delta's volume.
