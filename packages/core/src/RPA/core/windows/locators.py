import functools
import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple, Union

from RPA.core.locators import LocatorsDatabase, WindowsLocator
from RPA.core.vendor.deco import keyword as method
from RPA.core.windows.context import (
    ElementNotFound,
    WindowsContext,
    WindowControlError,
    with_timeout,
)
from RPA.core.windows.helpers import IS_WINDOWS

if IS_WINDOWS:
    import uiautomation as auto
    from uiautomation.uiautomation import Control


Locator = Union["WindowsElement", str]


@dataclass
class WindowsElement:
    """Represent Control as dataclass"""

    # pylint: disable=no-self-argument
    def _cmp_subname(win_elem: "WindowsElement", *, locator: str) -> bool:
        subname = None
        # pylint: disable=not-an-iterable
        for loc in MatchObject.parse_locator(locator).locators:
            if loc[0] == "SubName":
                subname = loc[1]
                break

        assert subname, f"couldn't find 'SubName' in parsed sub-locator {locator!r}"
        return subname in win_elem.name

    _WINDOW_SIBLING_COMPARE = {
        # <locator_strategy>: <element_attribute>
        "id": "automation_id",
        re.compile(r"(?<!sub)name:"): "name",
        "subname": _cmp_subname,
        "class": "class_name",
        "control": "control_type",
        "type": "control_type",
    }

    item: "Control"
    locator: Optional[Locator] = None
    name: str = ""
    automation_id: str = ""
    control_type: str = ""
    class_name: str = ""
    left: int = -1
    right: int = -1
    top: int = -1
    bottom: int = -1
    width: int = -1
    height: int = -1
    xcenter: int = -1
    ycenter: int = -1

    def __init__(self, item: "Control", locator: Optional[Locator]):
        self.item: "Control" = item
        self.locator: Optional[Locator] = locator
        self.name = item.Name
        self.automation_id = item.AutomationId
        self.control_type = item.ControlTypeName
        self.class_name = item.ClassName
        # If there's no rectangle, then all coords are defaulting to -1.
        rect = item.BoundingRectangle
        if rect:
            self.left = rect.left
            self.right = rect.right
            self.top = rect.top
            self.bottom = rect.bottom
            self.width = rect.width()
            self.height = rect.height()
            self.xcenter = rect.xcenter()
            self.ycenter = rect.ycenter()

    def is_sibling(self, win_elem: "WindowsElement") -> bool:
        """Returns `True` if the provided window element is a sibling."""
        locator: Optional[Locator] = win_elem.locator
        while locator:
            if isinstance(locator, WindowsElement):
                locator = locator.locator
            else:  # finally, reached a string locator
                break
        else:
            return True  # nothing to check here, can be considered sibling

        last_locator_part = locator.split(MatchObject.TREE_SEP)[-1]
        cmp_attrs = []
        for strategy, attr_or_func in self._WINDOW_SIBLING_COMPARE.items():
            if isinstance(strategy, str):
                strategy_regex = re.compile(rf"{strategy}:")
            else:
                strategy_regex = strategy
            if strategy_regex.search(last_locator_part):
                cmp_attrs.append(attr_or_func)
        # Name is assumed by default if no strategies are found at all.
        cmp_attrs = cmp_attrs or [self._WINDOW_SIBLING_COMPARE["name"]]
        for attr_or_func in cmp_attrs:
            if isinstance(attr_or_func, str):
                status = getattr(self, attr_or_func) == getattr(win_elem, attr_or_func)
            else:
                status = attr_or_func(win_elem, locator=last_locator_part)
            if not status:
                return False

        return True


@dataclass
class MatchObject:
    """Represents all locator parts as object properties"""

    _WINDOWS_LOCATOR_STRATEGIES = {
        # RPA-strategy: UIA-strategy
        "automationid": "AutomationId",
        "id": "AutomationId",
        "class": "ClassName",
        "control": "ControlType",
        "depth": "searchDepth",
        "name": "Name",
        "regex": "RegexName",
        "subname": "SubName",
        "type": "ControlType",
        "index": "foundIndex",
        "offset": "offset",
        "desktop": "desktop",
        "process": "process",
        "handle": "handle",
        "executable": "executable",
    }
    TREE_SEP = " > "
    QUOTE = '"'  # enclosing quote character
    _LOCATOR_REGEX = re.compile(rf"\S*{QUOTE}[^{QUOTE}]+{QUOTE}|\S+", re.IGNORECASE)
    _LOGGER = logging.getLogger(__name__)

    locators: List[Tuple] = field(default_factory=list)
    _classes: Set[str] = field(default_factory=set)
    max_level: int = 0

    @classmethod
    def parse_locator(cls, locator: str) -> "MatchObject":
        match_object = MatchObject()
        locator_tree = [loc.strip() for loc in locator.split(cls.TREE_SEP)]
        for level, branch in enumerate(locator_tree):
            default_values = []
            for part in cls._LOCATOR_REGEX.finditer(branch):
                match_object.handle_locator_part(
                    level, part.group().strip(), default_values
                )
            if default_values:
                match_object.add_locator("Name", " ".join(default_values), level=level)
        if not match_object.locators:
            match_object.add_locator("Name", locator)
        return match_object

    def handle_locator_part(
        self, level: int, part_text: str, default_values: List[str]
    ) -> None:
        if not part_text:
            return

        add_locator = functools.partial(self.add_locator, level=level)

        if part_text in ("and", "or", "desktop"):
            # NOTE(cmin764): Only "and" is supported at the moment. (match type is
            #  ignored and spaces are treated as "and"s by default)
            if part_text == "desktop":
                add_locator("desktop", "desktop")
            return

        try:
            strategy, value = part_text.split(":", 1)
        except ValueError:
            self._LOGGER.debug("No locator strategy found. (assuming 'name')")
            default_values.append(part_text)
            return

        control_strategy = self._WINDOWS_LOCATOR_STRATEGIES.get(strategy)
        if control_strategy:
            if default_values:
                add_locator("Name", " ".join(default_values))
                default_values.clear()
            add_locator(control_strategy, value)
        else:
            self._LOGGER.warning(
                "Invalid locator strategy %r! (assuming 'name')", strategy
            )
            default_values.append(part_text)

    def add_locator(self, control_strategy: str, value: str, level: int = 0) -> None:
        value = value.strip(f"{self.QUOTE} ")
        if not value:
            return

        self.max_level = max(self.max_level, level)

        if control_strategy in ("foundIndex", "searchDepth", "handle"):
            value = int(value)
        elif control_strategy == "ControlType":
            value = value if value.endswith("Control") else f"{value}Control"
        elif control_strategy == "ClassName":
            self._classes.add(value.lower())  # pylint: disable=no-member
        self.locators.append(  # pylint: disable=no-member
            (control_strategy, value, level)
        )

    @property
    def classes(self) -> List[str]:
        return list(self._classes)


class LocatorMethods(WindowsContext):
    """Keywords for finding Windows GUI elements"""

    def __init__(self, ctx, locators_path: Optional[str] = None):
        self._locators_path = locators_path
        super().__init__(ctx)

    @staticmethod
    def _get_control_from_params(
        search_params: Dict[str, str], root_control: Optional["Control"] = None
    ) -> "Control":
        offset = search_params.pop("offset", None)
        control_type = search_params.pop("ControlType", "Control")
        ElementControl = getattr(root_control, control_type, Control)
        element = ElementControl(**search_params)
        new_element = Control.CreateControlFromControl(element)
        new_element.robocorp_click_offset = offset
        return new_element

    def _get_control_from_listed_windows(
        self, search_params: Dict[str, str], *, param_type: str, win_type: str
    ) -> "Control":
        win_value = search_params.pop(param_type)
        window_list = self.ctx.list_windows()
        matches = [win for win in window_list if win[win_type] == win_value]
        if not matches:
            raise WindowControlError(
                f"Could not locate window with {param_type} {win_value!r}"
            )
        elif len(matches) > 1:
            raise WindowControlError(
                f"Found more than one window with {param_type} {win_value!r}"
            )
        self.logger.info("Found process with window title: %r", matches[0]["title"])
        search_params["Name"] = matches[0]["title"]
        return self._get_control_from_params(search_params)

    def _get_control_with_locator_part(
        self, locator: str, search_depth: int, root_control: "Control"
    ) -> "Control":
        # Prepare control search parameters.
        match_object = MatchObject.parse_locator(locator)
        self.logger.info("Locator %r produced matcher: %s", locator, match_object)
        search_params = {}
        for loc in match_object.locators:  # pylint: disable=not-an-iterable
            search_params[loc[0]] = loc[1]
        if "searchDepth" not in search_params:
            search_params["searchDepth"] = search_depth

        # Obtain an element with the search parameters.
        if "desktop" in search_params:
            root_control = auto.GetRootControl()
            return Control.CreateControlFromControl(root_control)

        if "executable" in search_params:
            return self._get_control_from_listed_windows(
                search_params, param_type="executable", win_type="name"
            )

        if "handle" in search_params:
            return self._get_control_from_listed_windows(
                search_params, param_type="handle", win_type="handle"
            )

        return self._get_control_from_params(search_params, root_control=root_control)

    def _load_by_alias(self, criteria: str) -> str:
        try:
            locator = LocatorsDatabase.load_by_name(criteria, self._locators_path)
            if isinstance(locator, WindowsLocator):
                return locator.value
        except ValueError:
            pass

        return criteria

    def _get_element_by_locator_string(
        self, locator: str, search_depth: int, root_element: Optional[WindowsElement]
    ) -> WindowsElement:
        root = root_element.item if self._window_or_none(root_element) else None
        anchor = self.anchor.item if self.anchor else None
        window = self.window.item if self.window else None
        self.logger.debug("argument root = %s", root)
        self.logger.debug("active anchor = %s", anchor)
        self.logger.debug("active window = %s", window)
        root_result = root or anchor or window or auto.GetRootControl()
        self.logger.debug("resulting root = %s", root_result)

        locators = locator.split(MatchObject.TREE_SEP)
        try:
            for loc in locators:
                self.logger.info("Root element: %r", root_result)
                control = self._get_control_with_locator_part(
                    loc, search_depth, root_result
                )
                root_result = control
        except LookupError as err:
            raise ElementNotFound(
                f"Element not found with locator {locator!r}"
            ) from err

        # If we get here, then we have a `control` no matter what.
        return WindowsElement(control, locator)

    @method
    @with_timeout
    def get_element(
        self,
        locator: Optional[Locator] = None,
        search_depth: int = 8,
        root_element: Optional[WindowsElement] = None,
        timeout: Optional[float] = None,  # pylint: disable=unused-argument
    ) -> WindowsElement:
        if isinstance(locator, str):
            locator = self._load_by_alias(locator)
        self.logger.info("Getting element with locator: %s", locator)
        if not locator:
            element = (
                self.ctx.anchor_element
                or self.ctx.window_element
                or WindowsElement(auto.GetRootControl(), None)
            )
        elif isinstance(locator, str):
            element = self._get_element_by_locator_string(
                locator, search_depth, root_element
            )
        else:
            element = locator
        if self._window_or_none(element) is None:
            raise ElementNotFound(f"Unable to get element with {locator!r}")
        self.logger.info("Returning element: %s", element)
        return element
