"""Put the inferred assembly constraints in CATIA (catia_mcp/tree/mates.py).

The product is already built with every instance in place (V5:
catia_mcp/tree/assembler.py; 3DEXPERIENCE: the imported product,
catia_mcp/tree/adopt.py). This module adds what a designer would have put:

1. **Reference elements**, created on purpose in each part, as a designer
   does: a geometrical set "Assembly references" with an axis line per
   cylindrical face used and a plane per planar face used, in the part's own
   frame, each published under its own name. A part placed twice gets them
   once.
2. **Constraints** in the root product's `Connections("CATIAConstraints")`:
   the grounded component and any component without a full set fixed
   (`AddMonoEltCst`, catCstTypeReference), every mate between the two
   published elements (`AddBiEltCst`): coincidence (catCstTypeOn), contact
   (catCstTypeSurfContact, else On, else an offset of 0), offset
   (catCstTypeDistance, its value and orientation), angle (catCstTypeAngle).
   References are built by name, `Root/Instance.1/!Publication` (else the
   element's own name).
3. **Checks**: after each component, the product is updated and every
   instance's position read back. A constraint that moves anything is
   removed, the position restored, and the component fixed instead. A
   failure never leaves a part where the STEP file does not put it.

The COM details live in a backend (V5 product, or the 3DEXPERIENCE product
imported from the STEP file); each COM call that has variants tries them in
order and records the one that worked.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from catia_mcp.tree.mates import Component, Element, Mate, MatePlan
from catia_mcp.tree.placement import catia_components

# CatConstraintType (V5 automation). 0, 1, 2 and 6 are used by
# catia_mcp/tools/assembly.py; scripts/probe_constraints.py reads the whole
# enum from the running CATIA's type library.
CST_TYPES = {
    "reference": 0, "distance": 1, "on": 2, "concentric": 3, "tangency": 4, "length": 5,
    "angle": 6, "planar_angle": 7, "parallelism": 8, "axis_parallelism": 9,
    "horizontality": 10, "perpendicularity": 11, "axis_perpendicularity": 12,
    "verticality": 13, "radius": 14, "symmetry": 15, "mid_point": 16, "equidistance": 17,
    "major_radius": 18, "minor_radius": 19, "surf_contact": 20, "lin_contact": 21,
    "ponc_contact": 22, "chamfer": 23, "chamfer_perpend": 24, "annul_contact": 25,
    "cylinder_radius": 26, "st_continuity": 27, "st_distance": 28, "sd_continuity": 29,
    "sd_shape": 30,
}
# CatConstraintOrientation
ORIENT_SAME, ORIENT_OPPOSITE = 0, 1

# The CATIA constraint types tried, in order, for each kind of mate.
ROUTES: dict[str, list[str]] = {
    "coincidence": ["on"],
    "contact": ["surf_contact", "on", "distance"],
    "offset": ["distance"],
    "angle": ["angle"],
}
GEOSET = "Assembly references"
POSITION_TOL = 1e-4
AXIS_HALF_LENGTH = 10.0


class ConstraintRefused(Exception):
    pass


def publication_name(element: Element) -> str:
    return f"{'Axis' if element.kind == 'axis' else 'Plane'} {element.face}"


# ── Backends ──

class V5Backend:
    """The V5 product the assembler made. `components`: plan instance id ->
    its product component; `paths`: id -> the component's path from the
    root (`Root/Sub.1/Bolt.2`); `documents`: prototype -> part document."""

    def __init__(self, app: Any, root: Any, components: dict[str, Any],
                 paths: dict[str, str], documents: dict[str, Any],
                 read_position: Callable[[Any], list[float]]) -> None:
        self.app, self.root = app, root
        self.components, self.paths, self.documents = components, paths, documents
        self.read_position = read_position
        self._constraints = None

    def constraints(self) -> Any:
        if self._constraints is None:
            self._constraints = self.root.Connections("CATIAConstraints")
        return self._constraints

    def references(self, instance: str, element: str | None = None
                   ) -> list[tuple[str, Callable[[], Any]]]:
        """Ways to reach a published element of an instance (`element`: its
        name), or the whole instance (None), each (label, make)."""
        path = self.paths[instance]
        name = f"{path}/!{path}/" if element is None else f"{path}/!{element}"
        return [("by path", lambda: self.root.CreateReferenceFromName(name))]

    def part(self, prototype: str) -> Any:
        return self.documents[prototype].Part

    def publish(self, prototype: str, name: str, shape: Any) -> str:
        """Publish a reference element of a part under `name`; the variant
        that worked."""
        product = self.documents[prototype].Product
        part = self.part(prototype)
        publications = product.Publications
        tried = []
        for way, make in (("object", lambda: part.CreateReferenceFromObject(shape)),
                          ("name", lambda: product.CreateReferenceFromName(
                              f"{product.PartNumber}/!{shape.Name}"))):
            try:
                reference = make()
                publications.Add(name)
                publications.SetDirect(name, reference)
                return way
            except Exception as error:  # the next variant
                tried.append(f"{way}: {error}")
        raise ConstraintRefused(f"cannot publish {name}: {'; '.join(tried)}")

    def position(self, instance: str) -> list[float]:
        return list(self.read_position(self.components[instance].Position))

    def set_position(self, instance: str, values: list[float]) -> None:
        self.components[instance].Position.SetComponents(list(values))

    def update(self) -> None:
        self.root.Update()

    def remove(self, constraint: Any) -> None:
        collection = self.constraints()
        name = str(constraint.Name)
        try:
            collection.Remove(name)
            return
        except Exception:  # by selection instead
            pass
        selection = self.app.ActiveDocument.Selection
        selection.Clear()
        selection.Add(constraint)
        selection.Delete()
        selection.Clear()


class X3DBackend(V5Backend):
    """The product imported from the STEP file on 3DEXPERIENCE (adopt). Not
    validated live: every call has variants, the route found is recorded
    (scripts/probe_constraints.py explores them on a live session)."""

    def __init__(self, conn: Any, holders: dict[str, Any], paths: dict[str, str],
                 parts: dict[str, Any], references: dict[str, Any],
                 read_position: Callable[[Any], list[float]]) -> None:
        super().__init__(conn.app, conn.root_occurrence(), holders, paths, {}, read_position)
        self.conn, self.parts, self.references = conn, parts, references
        self.route: str | None = None

    def constraints(self) -> Any:
        if self._constraints is not None:
            return self._constraints
        found, attempts = constraint_collection(self.conn)
        if found is None:
            raise ConstraintRefused("no constraint collection: " + "; ".join(
                f"{a['route']}: {a.get('error') or 'no AddMonoEltCst/AddBiEltCst'}"
                for a in attempts))
        self.route, self._constraints = found
        return self._constraints

    def part(self, prototype: str) -> Any:
        return self.parts[prototype]

    def references(self, instance: str, element: str | None = None
                   ) -> list[tuple[str, Callable[[], Any]]]:
        from catia_mcp.automation import safe_get

        root = self.root
        reference = safe_get(root, "ReferenceRootOccurrenceOf")
        full = self.paths[instance]
        relative = full.split("/", 1)[1] if "/" in full else full
        out = []
        for holder_label, holder in (("root occurrence", root), ("root reference", reference)):
            if holder is None:
                continue
            for path_label, path in (("full path", full), ("relative path", relative)):
                name = f"{path}/!{path}/" if element is None else f"{path}/!{element}"
                out.append((f"{holder_label}, {path_label}",
                            lambda h=holder, n=name: h.CreateReferenceFromName(n)))
        return out

    def publish(self, prototype: str, name: str, shape: Any) -> str:
        part = self.part(prototype)
        reference = self.references.get(prototype)
        tried = []
        holders = [("reference", lambda: reference.Publications),
                   ("part", lambda: part.Publications)]
        for way, publications in holders:
            try:
                pubs = publications()
                pubs.Add(name)
                pubs.SetDirect(name, part.CreateReferenceFromObject(shape))
                return way
            except Exception as error:
                tried.append(f"{way}: {error}")
        raise ConstraintRefused(f"cannot publish {name}: {'; '.join(tried)}")

    def update(self) -> None:
        for update in (lambda: self.root.Update(), lambda: self.conn.app.ActiveEditor.Update()):
            try:
                update()
                return
            except Exception:  # the next one
                continue

    def remove(self, constraint: Any) -> None:
        selection = self.conn.hso
        selection.Clear()
        selection.Add(constraint)
        selection.Delete()
        selection.Clear()


def constraint_collection(conn: Any) -> tuple[tuple[str, Any] | None, list[dict[str, Any]]]:
    """The first object of the 3DEXPERIENCE session that takes constraints
    (has AddMonoEltCst and AddBiEltCst), and every route tried."""
    from catia_mcp.automation import name_exists, safe_get

    attempts: list[dict[str, Any]] = []
    root = conn.root_occurrence()
    instance = safe_get(root, "ReferenceRootOccurrenceOf")
    reference = instance if instance is not None else safe_get(root, "ReferenceInstanceOf")
    routes: list[tuple[str, Callable[[], Any]]] = [
        ("root occurrence .Connections('CATIAConstraints')",
         lambda: root.Connections("CATIAConstraints")),
        ("root reference .GetItem('CATEngConnections')",
         lambda: reference.GetItem("CATEngConnections")),
        ("root reference .GetItem('CATIAConstraints')",
         lambda: reference.GetItem("CATIAConstraints")),
        ("root reference .Connections('CATIAConstraints')",
         lambda: reference.Connections("CATIAConstraints")),
        ("editor active object .Connections('CATIAConstraints')",
         lambda: conn.app.ActiveEditor.ActiveObject.Connections("CATIAConstraints")),
    ]
    for route, get in routes:
        record: dict[str, Any] = {"route": route}
        try:
            found = get()
            record["found"] = found is not None
            usable = found is not None and name_exists(found, "AddMonoEltCst") \
                and name_exists(found, "AddBiEltCst")
            record["usable"] = usable
            attempts.append(record)
            if usable:
                return (route, found), attempts
        except Exception as error:
            record["error"] = str(error)
            attempts.append(record)
    return None, attempts


# ── Applying a mate plan ──

class Constrainer:
    def __init__(self, backend: Any, log: Callable[[str], None] = lambda _: None) -> None:
        self.backend = backend
        self.log = log
        self.datums: dict[tuple[str, str], str] = {}  # (prototype, publication) -> route
        self.notes: list[str] = []

    def apply(self, plan: MatePlan, components: list[Component],
              expected: dict[str, list[float]]) -> dict[str, Any]:
        """Constrain every component of `plan`. `expected`: each instance's
        12 position components. Returns the report's `constraints` entry."""
        by_id = {c.id: c for c in components}
        report: dict[str, Any] = {"ground": plan.ground, "components": []}
        try:
            self.backend.constraints()
        except Exception as error:
            report.update(ok=False, error=f"no constraint collection: {error}",
                          note="The instances stay where the STEP file puts them.")
            return report
        for placement in plan.placements:
            row: dict[str, Any] = {"component": placement.component,
                                   "planned": placement.status, "constraints": []}
            report["components"].append(row)
            if placement.status == "mates":
                self._mates(placement.mates, by_id, row)
                moved = self._moved(expected)
                broken = self._broken(row)
                if row.get("error") or moved or broken:
                    reason = row.get("error") or (f"it moved {', '.join(moved)}" if moved else
                                                  f"not resolved: {', '.join(broken)}")
                    self._undo(row, expected)
                    row["fallback"] = reason
                    self._fix(placement.component, row)
                else:
                    row["status"] = "mates"
            else:
                self._fix(placement.component, row)
        moved = self._moved(expected)
        report["positions_ok"] = not moved
        if moved:
            report["moved"] = moved
        report["ok"] = not moved and all(r.get("status") for r in report["components"])
        report["counts"] = _counts(report["components"])
        if self.datums:
            report["publications"] = sorted({f"{p}: {n} ({way})" for (p, n), way in
                                             self.datums.items()})
        if self.notes:
            report["notes"] = self.notes
        route = getattr(self.backend, "route", None)
        if route:
            report["route"] = route
        return report

    # ── Fix ──

    def _fix(self, instance: str, row: dict[str, Any]) -> None:
        tried = []
        for label, reference in self.backend.references(instance):
            try:
                constraint = self.backend.constraints().AddMonoEltCst(CST_TYPES["reference"],
                                                                      reference())
            except Exception as error:  # the next way
                tried.append(f"{label}: {error}")
                continue
            row["constraints"].append({"kind": "fix", "catia_type": "reference",
                                       "name": _name(constraint), "reference": label})
            row["status"] = "fix" if row["planned"] != "ground" else "ground"
            row["_objects"] = [constraint]
            break
        else:  # the instance stays where it is, unconstrained
            row["status"] = None
            row["error"] = f"fix refused: {'; '.join(tried) or 'no reference'}"
        self.backend.update()

    # ── Mates ──

    def _mates(self, mates: list[Mate], by_id: dict[str, Component],
               row: dict[str, Any]) -> None:
        objects = row.setdefault("_objects", [])
        for mate in mates:
            try:
                first = self._datum(by_id[mate.component], mate.element)
                second = self._datum(by_id[mate.other], mate.other_element)
                constraint, entry = self._constraint(mate, first, second)
            except Exception as error:
                row["error"] = f"{mate.kind} {mate.element.face}/{mate.other_element.face}: {error}"
                return
            objects.append(constraint)
            row["constraints"].append(entry)
        self.backend.update()

    def _datum(self, component: Component, element: Element) -> str:
        """The name a constraint reaches the element by, in the part of
        `component`: a reference element named and published alike, created
        the first time. Unpublished, it is still reached by its name."""
        name = publication_name(element)
        key = (component.prototype, name)
        if key not in self.datums:
            part = self.backend.part(component.prototype)
            shape = _reference_element(part, element, name)
            try:
                self.datums[key] = self.backend.publish(component.prototype, name, shape)
            except Exception as error:
                self.datums[key] = "not published"
                self.notes.append(f"{component.prototype}: {error}")
        return name

    def _constraint(self, mate: Mate, first: str, second: str) -> tuple[Any, dict[str, Any]]:
        backend = self.backend
        collection = backend.constraints()
        tried = []
        for catia_type in ROUTES[mate.kind]:
            for (label, ref1), (_, ref2) in zip(backend.references(mate.component, first),
                                                backend.references(mate.other, second),
                                                strict=True):
                try:
                    constraint = collection.AddBiEltCst(CST_TYPES[catia_type], ref1(), ref2())
                except Exception as error:
                    tried.append(f"{catia_type} ({label}): {error}")
                    continue
                entry = {"kind": mate.kind, "catia_type": catia_type, "name": _name(constraint),
                         "elements": [f"{mate.component}: {first}", f"{mate.other}: {second}"],
                         "reference": label}
                self._settings(constraint, mate, catia_type, entry)
                return constraint, entry
        raise ConstraintRefused("; ".join(tried) or "no route")

    def _settings(self, constraint: Any, mate: Mate, catia_type: str,
                  entry: dict[str, Any]) -> None:
        value = mate.value if mate.kind != "contact" else 0.0
        if catia_type in ("distance", "angle") and value is not None:
            try:
                constraint.Dimension.Value = float(value)
                entry["value"] = value
            except Exception as error:
                self.notes.append(f"{entry['name']}: value {value} not set ({error})")
        orientation = mate.orientation or ("opposite" if mate.kind == "contact" else None)
        if orientation and catia_type in ("distance", "on"):
            try:
                constraint.Orientation = ORIENT_OPPOSITE if orientation == "opposite" \
                    else ORIENT_SAME
                entry["orientation"] = orientation
            except Exception as error:
                self.notes.append(f"{entry['name']}: orientation not set ({error})")

    # ── Checks ──

    def _broken(self, row: dict[str, Any]) -> list[str]:
        """The constraints CATIA does not resolve after the update
        (CatConstraintStatus other than 0); an unreadable status is noted."""
        broken = []
        for constraint, entry in zip(row.get("_objects", []), row["constraints"], strict=False):
            try:
                status = int(constraint.Status)
            except Exception as error:
                self.notes.append(f"{entry['name']}: status not read ({error})")
                continue
            entry["status"] = status
            if status != 0:
                broken.append(f"{entry['name']} (status {status})")
        return broken

    def _moved(self, expected: dict[str, list[float]]) -> list[str]:
        moved = []
        for instance, want in expected.items():
            try:
                got = self.backend.position(instance)
            except Exception as error:
                self.notes.append(f"{instance}: position not read ({error})")
                continue
            if len(got) != 12 or any(abs(a - b) > POSITION_TOL for a, b in
                                     zip(got, want, strict=True)):
                moved.append(instance)
        return moved

    def _undo(self, row: dict[str, Any], expected: dict[str, list[float]]) -> None:
        for constraint in row.pop("_objects", []):
            try:
                self.backend.remove(constraint)
            except Exception as error:
                self.notes.append(f"{_name(constraint)} not removed: {error}")
        row["removed"] = [c["name"] for c in row["constraints"]]
        row["constraints"] = []
        row.pop("error", None)
        self.backend.update()
        for instance in self._moved(expected):
            self.backend.set_position(instance, expected[instance])
        self.backend.update()


def _reference_element(part: Any, element: Element, name: str) -> Any:
    """An axis line or a plane in the part's own frame, in the geometrical
    set of assembly references, named `name`."""
    geoset = None
    sets = part.HybridBodies
    for i in range(1, sets.Count + 1):
        if str(sets.Item(i).Name) == GEOSET:
            geoset = sets.Item(i)
    if geoset is None:
        geoset = sets.Add()
        geoset.Name = GEOSET
    factory = part.HybridShapeFactory
    x, y, z = element.own_point
    a, b, c = element.own_direction
    if element.kind == "axis":
        point = factory.AddNewPointCoord(x, y, z)
        geoset.AppendHybridShape(point)
        point.Name = f"{name} point"
        direction = factory.AddNewDirectionByCoord(a, b, c)
        shape = factory.AddNewLinePtDir(part.CreateReferenceFromObject(point), direction,
                                        -AXIS_HALF_LENGTH, AXIS_HALF_LENGTH, False)
    else:
        shape = factory.AddNewPlaneEquation(a, b, c, a * x + b * y + c * z)
    geoset.AppendHybridShape(shape)
    shape.Name = name
    part.UpdateObject(shape)
    return shape


def _name(constraint: Any) -> str:
    try:
        return str(constraint.Name)
    except Exception:
        return "<unnamed>"


def _counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        for constraint in row["constraints"]:
            counts[constraint["kind"]] = counts.get(constraint["kind"], 0) + 1
        row.pop("_objects", None)
    return counts


def expected_positions(plan: Any) -> dict[str, list[float]]:
    """Each plan instance's position components relative to its parent."""
    return {placed.id: catia_components(placed.local) for placed in plan.instances}


def v5_paths(plan: Any, root_name: str, assembly_report: dict[str, Any]) -> dict[str, str]:
    """Each instance's path from the root product, from the assembler's
    report (the names CATIA kept)."""
    nodes = {n["id"]: n["catia_name"] for n in assembly_report.get("nodes") or []}
    parents = {n.id: n.parent for n in plan.nodes}

    def path(node: str) -> str:
        if node not in parents:
            return root_name
        return f"{path(parents[node])}/{nodes[node]}"

    catia = {i["id"]: i["catia_name"] for i in assembly_report["instances"]}
    return {placed.id: f"{path(placed.parent)}/{catia[placed.id]}" for placed in plan.instances}


# ── Entry points (catia_mcp/tree/__main__.py) ──

def plan_mates(plan: Any, mode: str, step: str | None = None,
               ground: str | None = None) -> tuple[MatePlan, list[Component]]:
    """The constraints to put (`mode` fix or infer) and the components they
    hold, offline: faces from the STEP file's shapes, else from the tree's
    build123d reference. Needs OCP."""
    from catia_mcp.tree import mates

    if step:
        from catia_mcp.tree.step_structure import read

        components = mates.components_from_structure(read(step), plan)
    else:
        components = mates.components_from_tree(plan.tree)
    if mode == "fix":
        return mates.fix_all(components), components
    return mates.infer(components, ground), components


def apply_v5(app: Any, assembler: Any, plan: Any, mate_plan: MatePlan,
             components: list[Component], documents: dict[str, Any],
             assembly_report: dict[str, Any], log: Callable[[str], None] = lambda _: None
             ) -> dict[str, Any]:
    """Constrain the V5 product the assembler just made (`documents`: the
    part document of each prototype)."""
    root = assembler.root
    backend = V5Backend(app, root, assembler.components,
                        v5_paths(plan, str(root.Name), assembly_report), documents,
                        assembler.read_position)
    return Constrainer(backend, log).apply(mate_plan, components, expected_positions(plan))


def apply_3dx(conn: Any, plan: Any, mate_plan: MatePlan, components: list[Component],
              leaves: list[dict[str, Any]], parts: dict[str, Any],
              references: dict[str, Any], log: Callable[[str], None] = lambda _: None
              ) -> dict[str, Any]:
    """Constrain the product imported from the STEP file (adopt), its leaves
    matched in order to the plan's instances (`parts`, `references`: the
    Part and the reference of each prototype)."""
    from catia_mcp.automation import call_fill_array, safe_get

    root_name = str(safe_get(conn.root_occurrence(), "Name", "") or "")
    pairs = list(zip(plan.instances, leaves, strict=True))
    holders = {placed.id: leaf["holder"] for placed, leaf in pairs}
    paths = {placed.id: f"{root_name}{leaf['path']}" for placed, leaf in pairs}
    backend = X3DBackend(conn, holders, paths, parts, references,
                         lambda position: call_fill_array(conn.app, position, "GetComponents", 12))
    return Constrainer(backend, log).apply(mate_plan, components, expected_positions(plan))
