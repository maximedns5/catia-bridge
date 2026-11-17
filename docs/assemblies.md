# Assemblies

A build123d model is often a `Compound` of parts, some placed several times.
The feature tree gives each part's features in world coordinates: a part
moved in build123d arrives with the move folded into its sketches, and a part
used twice arrives twice. The model's STEP file keeps what the tree drops:
each part once, in its own frame, and each instance's location.

```bash
python -m catia_mcp.tree build MODEL.tree.json --assembly product --step MODEL.step
```

## What happens

1. **Structure** (`catia_mcp/tree/step_structure.py`, OpenCascade's XCAF
   reader, installed with build123d): the root, its sub-assembly nodes, and
   every part instance with its name, its prototype (instances of one shape
   share it), its location relative to its parent and to the root, its
   colour (sRGB) and its solid's volume and centre of gravity. Ids are name
   paths like the tree's (`/Bracket/Bolt`); a name repeated among siblings
   gets `#2`, `#3`, in sibling order.
2. **Matching** (`match`): every tree part must have the STEP instance of the
   same id, of the same volume and with its centre of gravity in the same
   place (the tree's world properties from the build123d reference). Any
   difference, or an instance missing from the tree, stops the build before
   CATIA is touched.
3. **Plan** (`catia_mcp/tree/assembly.py`): one part per prototype, moved
   back into its own frame (`placement.unplace_part`: the inverse of the
   instance's world location), so a bolt is modelled centred on its own
   origin, as a designer would, then placed once per instance.
4. **Parts**: built as usual (human style, checks, parameters,
   constraints), each in its own part document.
5. **Product** (`catia_mcp/tree/assembler.py`, CATIA V5):
   - a Product document named after the root, a product component per
     sub-assembly node (`AddNewProduct`);
   - the first instance of a part from its document
     (`AddExternalComponent`), every other one reusing it (`AddComponent` of
     its reference product): one CATPart placed several times, its part
     number the part's name;
   - instances named `Bolt.1`, `Bolt.2`, and placed by
     `Position.SetComponents` (the X, Y and Z axes, then the origin, relative
     to the parent), each read back and compared to 1e-6;
   - the STEP colours on the instances.

   A name CATIA changes or refuses is noted; a position it does not keep
   fails the assembly (the parts stay built). The report's `assembly` lists
   every node and instance. Nothing is saved.

Without `--step`, every tree part is its own part, placed at the identity
where the tree puts it: a product grouping the parts, without reuse.

## 3DEXPERIENCE

A Physical Product created by script needs licences (E70, VMX) that student
and many design roles lack: on R428 the student role refused a scripted 3D
Part. So on 3DEXPERIENCE `--assembly product` builds every part in its own
frame and stops there, saying so. `scripts/probe_assembly.py` records what a
session allows:

- the licences, and with `--create` whether `PLMCreate "VPMReference"` works;
- the product open in the active editor (for example the model's STEP
  imported by hand): every occurrence, its reference, its position;
- with `--step`, those positions next to the STEP file's;
- with `--move`, whether `Position.SetComponents` moves an instance (10 mm,
  then back);
- whether an occurrence leads to its Part (to build native features inside
  an imported part later).

## What the app could send

The STEP of the same compile is enough today. A tree that said it itself
would be simpler (contract requests):

- `frame: "part"` with each part's own `location` (relative to its parent)
  and `world`;
- the sub-assembly nodes;
- a `prototype` key on parts placing the same build123d shape, the others
  carrying `features_from`;
- the colour (sRGB) and the rule that makes repeated names unique.

## Not yet validated live

The V5 assembler has only run against a fake CATIA:
`AddExternalComponent` with an unsaved part document, `SetComponents` with a
Python list, instance names sticking, positions relative to the parent at
two levels.
