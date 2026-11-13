"""The accessibility tree: one cached UIA round trip per look, indexed for acting.

Every element keeps its live COM pointer (AutomationElementMode_Full), so an index
from the latest look can be acted on without searching again.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import uiautomation as auto
from uiautomation import uiautomation as _U

P = auto.PropertyId
PROPS = {
    "name": P.NameProperty, "ctype": P.ControlTypeProperty, "aid": P.AutomationIdProperty,
    "cls": P.ClassNameProperty, "rect": P.BoundingRectangleProperty, "enabled": P.IsEnabledProperty,
    "offscreen": P.IsOffscreenProperty, "focus": P.HasKeyboardFocusProperty,
    "invoke": P.IsInvokePatternAvailableProperty, "value_ok": P.IsValuePatternAvailableProperty,
    "toggle": P.IsTogglePatternAvailableProperty, "expand": P.IsExpandCollapsePatternAvailableProperty,
    "select": P.IsSelectionItemPatternAvailableProperty, "scroll": P.IsScrollPatternAvailableProperty,
    "legacy": P.IsLegacyIAccessiblePatternAvailableProperty, "value": P.ValueValueProperty,
    "readonly": P.ValueIsReadOnlyProperty, "tstate": P.ToggleToggleStateProperty,
    "xstate": P.ExpandCollapseExpandCollapseStateProperty, "selected": P.SelectionItemIsSelectedProperty,
    "text_ok": P.IsTextPatternAvailableProperty, "range_ok": P.IsRangeValuePatternAvailableProperty,
    "range": P.RangeValueValueProperty, "hwnd": P.NativeWindowHandleProperty,
    "default_action": P.LegacyIAccessibleDefaultActionProperty, "help": P.HelpTextProperty,
}
STRUCTURAL = {"Pane", "Group", "Custom", "Document", "Window", "TitleBar", "Thumb", "Separator", "ScrollBar"}
SHORT = {"Button": "button", "Edit": "edit", "Text": "text", "MenuItem": "menuitem", "ListItem": "listitem",
         "TreeItem": "treeitem", "CheckBox": "checkbox", "RadioButton": "radio", "ComboBox": "combo",
         "Hyperlink": "link", "TabItem": "tab", "Tab": "tabs", "List": "list", "Tree": "tree", "Menu": "menu",
         "MenuBar": "menubar", "ToolBar": "toolbar", "Slider": "slider", "Spinner": "spinner",
         "DataItem": "row", "DataGrid": "grid", "Table": "table", "Header": "header", "HeaderItem": "column",
         "Image": "image", "Document": "document", "SplitButton": "splitbutton", "StatusBar": "statusbar",
         "Pane": "pane", "Group": "group", "Window": "window", "ProgressBar": "progress", "AppBar": "appbar",
         "Custom": "custom", "TitleBar": "titlebar", "ScrollBar": "scrollbar", "Calendar": "calendar"}
TREE_SCOPE_SUBTREE = 7
MODE_FULL = 1
TEXT_CAP = 160


@dataclass
class Node:
    index: int
    el: object  # IUIAutomationElement (live)
    ctype: str
    name: str
    aid: str
    rect: tuple[int, int, int, int]
    depth: int
    props: dict = field(default_factory=dict)

    @property
    def center(self) -> tuple[int, int]:
        l, t, r, b = self.rect
        return (l + r) // 2, (t + b) // 2

    def actions(self) -> list[str]:
        a = []
        p = self.props
        if p.get("invoke"): a.append("click")
        if p.get("toggle"): a.append("toggle")
        if p.get("select"): a.append("select")
        if p.get("expand"): a.append("expand/collapse")
        if p.get("value_ok") and not p.get("readonly"): a.append("set")
        if p.get("range_ok"): a.append("set-range")
        if p.get("scroll"): a.append("scroll")
        if not a and p.get("legacy") and p.get("default_action"): a.append("click")
        return a


class Tree:
    def __init__(self):
        self.uia = _U._AutomationClient.instance().IUIAutomation
        cr = self.uia.CreateCacheRequest()
        for pid in PROPS.values():
            cr.AddProperty(pid)
        cr.TreeScope = TREE_SCOPE_SUBTREE
        cr.TreeFilter = self.uia.ControlViewCondition
        cr.AutomationElementMode = MODE_FULL
        self.cr = cr

    def snapshot(self, hwnd: int, origin: tuple[int, int], max_nodes: int = 1500) -> list[Node]:
        root = self.uia.ElementFromHandleBuildCache(hwnd, self.cr)
        nodes: list[Node] = []
        self._walk(root, 0, nodes, origin, max_nodes)
        return nodes

    def _get(self, el, key):
        try:
            v = el.GetCachedPropertyValue(PROPS[key])
        except Exception:
            return None
        if isinstance(v, str):  # broken UTF-16 from an app must never break encoding downstream
            v = v.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
        return v

    def _walk(self, el, depth, nodes, origin, max_nodes):
        if len(nodes) >= max_nodes:
            return
        props = {k: self._get(el, k) for k in PROPS}
        ctype = auto.ControlTypeNames.get(props["ctype"], "Custom").replace("Control", "")
        r = props["rect"]
        rect = (int(r[0]), int(r[1]), int(r[2]), int(r[3])) if r is not None and len(r) == 4 else (0, 0, 0, 0)
        # UIA rect is (left, top, width, height)
        rect = (rect[0], rect[1], rect[0] + rect[2], rect[1] + rect[3])
        nodes.append(Node(index=len(nodes), el=el, ctype=ctype, name=(props["name"] or "").strip(),
                          aid=props["aid"] or "", rect=rect, depth=depth, props=props))
        try:
            kids = el.GetCachedChildren()
        except Exception:
            kids = None
        if not kids:  # leaves come back as a NULL COM pointer
            return
        for i in range(kids.Length):
            self._walk(kids.GetElement(i), depth + 1, nodes, origin, max_nodes)

    def focused(self):
        try:
            return self.uia.GetFocusedElement()
        except Exception:
            return None


def _clip(s: str, n: int = TEXT_CAP) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def render(nodes: list[Node], origin: tuple[int, int], scale: float, cap: int = 12000) -> tuple[str, int]:
    """Indented text tree. Structural nameless nodes collapse; coordinates are screenshot pixels."""
    lines: list[str] = []
    kept_depth: dict[int, int] = {}
    shown = 0
    ox, oy = origin
    for n in nodes:
        p = n.props
        acts = n.actions()
        val = p.get("value")
        interesting = bool(n.name or acts or val or p.get("focus") or n.ctype not in STRUCTURAL)
        if n.index == 0:
            interesting = True
        if not interesting:
            continue
        if p.get("offscreen") and not p.get("focus") and n.index != 0:
            continue
        indent = min(n.depth, 12)
        bits = [f"[{n.index}]", SHORT.get(n.ctype, n.ctype.lower())]
        if n.name:
            bits.append(f'"{_clip(n.name)}"')
        if n.aid and not n.aid.isdigit() and len(n.aid) < 48:
            bits.append(f"#{n.aid}")
        if val not in (None, "") and val != n.name:
            bits.append(f'value="{_clip(val, 240)}"')
        if p.get("range_ok") and p.get("range") is not None:
            bits.append(f"range={p.get('range')}")
        if p.get("toggle") and p.get("tstate") is not None:
            bits.append(["off", "on", "mixed"][int(p["tstate"]) % 3])
        if p.get("expand") and p.get("xstate") is not None:
            bits.append(["collapsed", "expanded", "partial", "leaf"][int(p["xstate"]) % 4])
        if p.get("selected"):
            bits.append("selected")
        if p.get("focus"):
            bits.append("FOCUSED")
        if p.get("enabled") is False:
            bits.append("disabled")
        if acts:
            bits.append("(" + ",".join(acts) + ")")
        l, t, r, b = n.rect
        if r > l and b > t:
            cx, cy = (l + r) // 2 - ox, (t + b) // 2 - oy
            bits.append(f"@{round(cx * scale)},{round(cy * scale)}")
        lines.append("  " * indent + " ".join(bits))
        shown += 1
    text = "\n".join(lines)
    if len(text) > cap:
        cut = text[:cap].rsplit("\n", 1)[0]
        text = cut + f"\n… tree cut at {cap} chars ({shown} elements; ask hands_look with tree_cap higher or find=…)"
    return text, shown


def find(nodes: list[Node], query: str) -> list[Node]:
    q = query.casefold()
    return [n for n in nodes if q in n.name.casefold() or q in n.aid.casefold()
            or q in str(n.props.get("value") or "").casefold()]
