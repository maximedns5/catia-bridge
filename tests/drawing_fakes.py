"""In-memory doubles of the CATIA drafting object model, for the drawing
builder (catia_mcp/tree/drawing_builder.py).

They record what the builder asks for. A V5 sheet has the Main and
Background views; GenerateDimensions adds dimensions to the active
generative view only, as CATIA does (a silent no-op otherwise).
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
    def __init__(self, text: str, x: float, y: float) -> None:
        self.Text, self.x, self.y = text, x, y
        self.Leaders = Leaders()
        self.FrameType = 0
        self.size = None

    def SetFontSize(self, first: int, count: int, size: float) -> None:  # noqa: N802
        self.size = size


class Texts:
    def __init__(self) -> None:
        self.items: list[Text] = []

    def Add(self, text: str, x: float, y: float) -> Text:  # noqa: N802
        item = Text(text, x, y)
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


class Factory2D:
    def __init__(self) -> None:
        self.lines: list[tuple[float, ...]] = []
        self.circles: list[tuple[float, ...]] = []

    def CreateLine(self, *args: float) -> None:  # noqa: N802
        self.lines.append(args)

    def CreateClosedCircle(self, *args: float) -> None:  # noqa: N802
        self.circles.append(args)


class Behavior:
    """DrawingViewGenerativeBehavior: records the Define* calls."""

    def __init__(self, fail: dict[str, str]) -> None:
        self.Document = None
        self.calls: list[tuple[str, tuple]] = []
        self.fail = fail
        self.updates = 0

    def __getattr__(self, name: str):
        if not name.startswith("Define"):
            raise AttributeError(name)

        def define(*args: Any) -> None:
            if name in self.fail:
                raise RuntimeError(self.fail[name])
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
            self.GenerativeBehavior = Behavior(sheet.fail)
        self.Texts = Texts()
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
    def __init__(self, fail: dict[str, str] | None = None) -> None:
        self.fail = fail or {}
        self.Name = "Sheet.1"
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


class Drawing:
    def __init__(self, name: str = "Drawing1.CATDrawing", fail: dict[str, str] | None = None
                 ) -> None:
        self.Name = name
        self.Sheets = SimpleNamespace(ActiveSheet=Sheet(fail))
        self.updates = 0
        self.exported: list[tuple[str, str]] = []

    def Activate(self) -> None:  # noqa: N802
        pass

    def Update(self) -> None:  # noqa: N802
        self.updates += 1

    def ExportData(self, path: str, fmt: str) -> None:  # noqa: N802
        self.exported.append((path, fmt))
        with open(path, "wb") as handle:
            handle.write(b"%PDF-1.4\n")


class Documents:
    def __init__(self, existing: list[Any] | None = None,
                 fail: dict[str, str] | None = None) -> None:
        self.items = list(existing or [])
        self.drawings: list[Drawing] = []
        self.fail = fail

    @property
    def Count(self) -> int:  # noqa: N802
        return len(self.items)

    def Item(self, index: int) -> Any:  # noqa: N802
        return self.items[index - 1]

    def Add(self, kind: str) -> Drawing:  # noqa: N802
        assert kind == "Drawing"
        drawing = Drawing(f"Drawing{len(self.drawings) + 1}.CATDrawing", self.fail)
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
