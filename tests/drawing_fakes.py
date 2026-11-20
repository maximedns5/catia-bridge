"""In-memory doubles of the CATIA drafting object model, for the drawing
builder (catia_mcp/tree/drawing_builder.py).

They record what the builder asks for. A V5 sheet has the Main and
Background views; GenerateDimensions adds dimensions to the active
generative view only, as CATIA does (a silent no-op otherwise). Options:
`fail` makes a Define* call raise; `ascii_font` makes texts lose what a
Latin-1 font lacks (as '?'); `generation` gives generative views the named
generation settings (AxisMode, CenterLineMode...).
"""

from __future__ import annotations

import os
from types import SimpleNamespace
from typing import Any

# CatPaperSize width x height in portrait, mm.
PAPER = {0: (841.0, 1189.0), 1: (594.0, 841.0), 2: (420.0, 594.0), 3: (297.0, 420.0),
         4: (210.0, 297.0)}


class Leaders:
    def __init__(self) -> None:
        self.heads: list[tuple[float, float]] = []

    def Add(self, x: float, y: float) -> None:  # noqa: N802
        self.heads.append((x, y))


class Text:
    def __init__(self, text: str, x: float, y: float, ascii_font: bool = False) -> None:
        if ascii_font:
            text = text.encode("latin-1", "replace").decode("latin-1")
        self.Text, self.x, self.y = text, x, y
        self.Leaders = Leaders()
        self.FrameType = 0
        self.size = None

    def SetFontSize(self, first: int, count: int, size: float) -> None:  # noqa: N802
        self.size = size


class Texts:
    def __init__(self, ascii_font: bool = False) -> None:
        self.items: list[Text] = []
        self.ascii_font = ascii_font

    def Add(self, text: str, x: float, y: float) -> Text:  # noqa: N802
        item = Text(text, x, y, self.ascii_font)
        self.items.append(item)
        return item


class Table:
    def __init__(self, x, y, rows, columns, row_height, column_width) -> None:
        self.Name = "Table.1"
        self.anchor, self.shape = (x, y), (rows, columns)
        self.cells: dict[tuple[int, int], str] = {}
        self.widths: dict[int, float] = {}

    def SetColumnSize(self, column: int, width: float) -> None:  # noqa: N802
        self.widths[column] = width

    def SetCellString(self, row: int, column: int, text: str) -> None:  # noqa: N802
        self.cells[(row, column)] = text


class Tables:
    def __init__(self) -> None:
        self.items: list[Table] = []

    def Add(self, *args: Any) -> Table:  # noqa: N802
        table = Table(*args)
        self.items.append(table)
        return table


class Line2D:
    def __init__(self, args: tuple[float, ...]) -> None:
        self.ends = args
        self.line_type = 1  # solid
        self.width = None


class Factory2D:
    def __init__(self) -> None:
        self.lines: list[tuple[float, ...]] = []
        self.circles: list[tuple[float, ...]] = []
        self.items: list[Line2D] = []

    def CreateLine(self, *args: float) -> Line2D:  # noqa: N802
        self.lines.append(args)
        item = Line2D(args)
        self.items.append(item)
        return item

    def CreateClosedCircle(self, *args: float) -> Line2D:  # noqa: N802
        self.circles.append(args)
        item = Line2D(args)
        self.items.append(item)
        return item


class Behavior:
    """DrawingViewGenerativeBehavior: records the Define* calls. A failure
    given as 'DefineAuxiliaryView/5' refuses only calls with 5 arguments."""

    def __init__(self, fail: dict[str, str], generation: tuple[str, ...] = ()) -> None:
        self.Document = None
        self.calls: list[tuple[str, tuple]] = []
        self.fail = fail
        self.updates = 0
        for name in generation:
            setattr(self, name, False)

    def __getattr__(self, name: str):
        if not name.startswith("Define"):
            raise AttributeError(name)

        def define(*args: Any) -> None:
            for key in (name, f"{name}/{len(args)}"):
                if key in self.fail:
                    raise RuntimeError(self.fail[key])
            self.calls.append((name, args))
        return define

    def Update(self) -> None:  # noqa: N802
        self.updates += 1


class View:
    def __init__(self, sheet: Sheet, name: str, generative: bool = True) -> None:
        self.sheet = sheet
        self.Name = name
        self.x = self.y = 0.0
        self.Scale = 1.0
        if generative:
            self.GenerativeBehavior = Behavior(sheet.fail, sheet.generation)
        self.Texts = Texts(sheet.ascii_font)
        self.Tables = Tables()
        self.Factory2D = Factory2D()
        self.Dimensions = SimpleNamespace(Count=0)
        self.label = None

    def Activate(self) -> None:  # noqa: N802
        self.sheet.Views.active = self

    def SetViewName(self, prefix: str, ident: str, suffix: str) -> None:  # noqa: N802
        self.label = f"{prefix}{ident}-{ident}{suffix}"


class Views:
    def __init__(self, sheet: Sheet) -> None:
        self.sheet = sheet
        self.items = [View(sheet, "Main View", False), View(sheet, "Background View", False)]
        self.active = self.items[0]
        self.removed: list[str] = []

    @property
    def Count(self) -> int:  # noqa: N802
        return len(self.items)

    @property
    def ActiveView(self) -> View:  # noqa: N802
        return self.active

    def Item(self, key: Any) -> View:  # noqa: N802
        if isinstance(key, int):
            return self.items[key - 1]
        for view in self.items:
            if view.Name == key:
                return view
        raise KeyError(key)

    def Add(self, name: str) -> View:  # noqa: N802
        view = View(self.sheet, name)
        self.items.append(view)
        return view

    def Remove(self, name: str) -> None:  # noqa: N802
        self.items = [v for v in self.items if v.Name != name]
        self.removed.append(name)


class Sheet:
    def __init__(self, fail: dict[str, str] | None = None, name: str = "Sheet.1",
                 ascii_font: bool = False, generation: tuple[str, ...] = (),
                 owner: Any = None) -> None:
        self.fail = fail or {}
        self.ascii_font = ascii_font
        self.generation = generation
        self.owner = owner
        self.Name = name
        self.PaperSize = 4
        self.Orientation = 0
        self.Scale = 1.0
        self.ProjectionMethod = 1
        self.Views = Views(self)
        self.generated = 0

    def GetPaperWidth(self) -> float:  # noqa: N802
        w, h = PAPER[self.PaperSize]
        return h if self.Orientation == 1 else w

    def GetPaperHeight(self) -> float:  # noqa: N802
        w, h = PAPER[self.PaperSize]
        return w if self.Orientation == 1 else h

    def GenerateDimensions(self) -> None:  # noqa: N802
        view = self.Views.active
        if hasattr(view, "GenerativeBehavior"):
            view.Dimensions.Count += 2
            self.generated += 1

    def Activate(self) -> None:  # noqa: N802
        if self.owner is not None:
            self.owner.ActiveSheet = self


class Sheets:
    def __init__(self, first: Sheet) -> None:
        self.items = [first]
        self.ActiveSheet = first
        first.owner = self

    @property
    def Count(self) -> int:  # noqa: N802
        return len(self.items)

    def Item(self, index: int) -> Sheet:  # noqa: N802
        return self.items[index - 1]

    def Add(self, name: str) -> Sheet:  # noqa: N802
        first = self.items[0]
        sheet = Sheet(first.fail, name, first.ascii_font, first.generation, self)
        self.items.append(sheet)
        return sheet


class VisProperties:
    def __init__(self, selection: Selection) -> None:
        self.selection = selection

    def SetRealLineType(self, line_type: int, inheritance: int) -> None:  # noqa: N802
        for item in self.selection.items:
            item.line_type = line_type

    def SetRealWidth(self, width: int, inheritance: int) -> None:  # noqa: N802
        for item in self.selection.items:
            item.width = width


class Selection:
    def __init__(self) -> None:
        self.items: list[Any] = []
        self.VisProperties = VisProperties(self)

    def Clear(self) -> None:  # noqa: N802
        self.items = []

    def Add(self, item: Any) -> None:  # noqa: N802
        self.items.append(item)


class Drawing:
    def __init__(self, name: str = "Drawing1.CATDrawing", fail: dict[str, str] | None = None,
                 ascii_font: bool = False, generation: tuple[str, ...] = (),
                 refuse_export: str | None = None) -> None:
        self.Name = name
        self.Sheets = Sheets(Sheet(fail, ascii_font=ascii_font, generation=generation))
        self.Selection = Selection()
        self.updates = 0
        self.exported: list[tuple[str, str]] = []
        self.refuse_export = refuse_export

    def Activate(self) -> None:  # noqa: N802
        pass

    def Update(self) -> None:  # noqa: N802
        self.updates += 1

    def ExportData(self, path: str, fmt: str) -> None:  # noqa: N802
        if self.refuse_export:
            raise RuntimeError(self.refuse_export)
        self.exported.append((path, fmt))
        with open(path, "wb") as handle:
            handle.write(b"%PDF-1.4\n")


class Documents:
    def __init__(self, existing: list[Any] | None = None,
                 fail: dict[str, str] | None = None, **drawing: Any) -> None:
        self.items = list(existing or [])
        self.drawings: list[Drawing] = []
        self.fail = fail
        self.drawing = drawing  # options for every new drawing

    @property
    def Count(self) -> int:  # noqa: N802
        return len(self.items)

    def Item(self, index: int) -> Any:  # noqa: N802
        return self.items[index - 1]

    def Add(self, kind: str) -> Drawing:  # noqa: N802
        assert kind == "Drawing"
        drawing = Drawing(f"Drawing{len(self.drawings) + 1}.CATDrawing", self.fail,
                          **self.drawing)
        self.drawings.append(drawing)
        self.items.append(drawing)
        return drawing


class ConnV5:
    is_3dx = False

    def __init__(self, documents: Documents) -> None:
        self.app = SimpleNamespace(Documents=documents)

    def refresh_display(self) -> None:
        pass


class Worker:
    """ComWorker.run_guarded, inline."""

    def __init__(self) -> None:
        self.labels: list[str] = []

    def run_guarded(self, fn, *args, label=None, **_):
        self.labels.append(label)
        return fn(*args)


def part_document(name: str) -> Any:
    return SimpleNamespace(Name=f"{name}.CATPart")


def product_document(part_number: str) -> Any:
    return SimpleNamespace(Name=f"{part_number}.CATProduct",
                           Product=SimpleNamespace(PartNumber=part_number))


def exported_files(drawings: list[Drawing]) -> list[str]:
    return [path for d in drawings for path, _ in d.exported if os.path.exists(path)]
