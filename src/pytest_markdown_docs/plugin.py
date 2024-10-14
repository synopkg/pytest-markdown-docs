import ast
import inspect
import traceback
import types
import pathlib
import pytest
import typing

from _pytest._code import ExceptionInfo
from _pytest.config.argparsing import Parser
from _pytest.pathlib import import_path
from pytest_markdown_docs import hooks


if pytest.version_tuple >= (8, 0, 0):
    from _pytest.fixtures import TopRequest
else:
    # pytest 7 compatible
    from _pytest.fixtures import FixtureRequest as TopRequest  # type: ignore


if typing.TYPE_CHECKING:
    from markdown_it.token import Token

MARKER_NAME = "markdown-docs"


class MarkdownInlinePythonItem(pytest.Item):
    def __init__(
        self,
        name: str,
        parent: typing.Union["MarkdownDocstringCodeModule", "MarkdownTextFile"],
        code: str,
        fixture_names: typing.List[str],
        start_line: int,
        fake_line_numbers: bool,
    ) -> None:
        super().__init__(name, parent)
        self.add_marker(MARKER_NAME)
        self.code = code
        self.obj = None
        self.user_properties.append(("code", code))
        self.start_line = start_line
        self.fake_line_numbers = fake_line_numbers
        self.fixturenames = fixture_names
        self.nofuncargs = True

    def setup(self):
        def func() -> None:
            pass

        self.funcargs = {}
        self._fixtureinfo = self.session._fixturemanager.getfixtureinfo(
            node=self, func=func, cls=None
        )
        self.fixture_request = TopRequest(self, _ispytest=True)
        self.fixture_request._fillfixtures()

    def runtest(self):
        global_sets = self.parent.config.hook.pytest_markdown_docs_globals()

        mod = types.ModuleType("fence")  # dummy module
        all_globals = mod.__dict__
        for global_set in global_sets:
            all_globals.update(global_set)

        # make sure to evaluate fixtures
        # this will insert named fixtures into self.funcargs
        for fixture_name in self._fixtureinfo.names_closure:
            self.fixture_request.getfixturevalue(fixture_name)

        # Since these are not actual functions with arguments, the only
        # arguments that should appear in self.funcargs are the filled fixtures
        for argname, value in self.funcargs.items():
            if argname not in all_globals:
                all_globals[argname] = value

        try:
            tree = ast.parse(self.code)
        except SyntaxError:
            raise

        try:
            # if we don't compile the code, it seems we get name lookup errors
            # for functions etc. when doing cross-calls across inline functions
            compiled = compile(tree, self.name, "exec", dont_inherit=True)
        except SyntaxError:
            raise

        exec(compiled, all_globals)

    def repr_failure(
        self,
        excinfo: ExceptionInfo[BaseException],
        style=None,
    ):
        rawlines = self.code.split("\n")

        # custom formatted traceback to translate line numbers and markdown files
        traceback_lines = []
        stack_summary = traceback.StackSummary.extract(traceback.walk_tb(excinfo.tb))
        start_capture = False

        start_line = 0 if self.fake_line_numbers else self.start_line

        for frame_summary in stack_summary:
            if frame_summary.filename == self.name:
                lineno = (frame_summary.lineno or 0) + start_line
                start_capture = (
                    True  # start capturing frames the first time we enter user code
                )
                line = (
                    rawlines[frame_summary.lineno - 1] if frame_summary.lineno else ""
                )
            else:
                lineno = frame_summary.lineno or 0
                line = frame_summary.line or ""

            if start_capture:
                linespec = f"line {lineno}"
                if self.fake_line_numbers:
                    linespec = f"code block line {lineno}*"

                traceback_lines.append(
                    f"""  File "{frame_summary.filename}", {linespec}, in {frame_summary.name}"""
                )
                traceback_lines.append(f"    {line.lstrip()}")

        maxnum = len(str(len(rawlines) + start_line + 1))
        numbered_code = "\n".join(
            [
                f"{i:>{maxnum}}   {line}"
                for i, line in enumerate(rawlines, start_line + 1)
            ]
        )

        pretty_traceback = "\n".join(traceback_lines)
        note = ""
        if self.fake_line_numbers:
            note = ", *-denoted line numbers refer to code block"
        pt = f"""Traceback (most recent call last{note}):
{pretty_traceback}
{excinfo.exconly()}"""

        return f"""Error in code block:
```
{numbered_code}
```
{pt}
"""

    def reportinfo(self):
        return self.name, 0, f"docstring for {self.name}"


def extract_code_blocks(
    markdown_string: str,
) -> typing.Generator[typing.Tuple[str, typing.List[str], int], None, None]:
    import markdown_it

    mi = markdown_it.MarkdownIt(config="commonmark")
    tokens = mi.parse(markdown_string)

    prev = ""
    for i, block in enumerate(tokens):
        if block.type != "fence" or not block.map:
            continue

        startline = block.map[0] + 1  # skip the info line when counting
        code_info = block.info.split()

        lang = code_info[0] if code_info else None
        code_options = set(code_info) - {lang}

        # MDX comments are put inside of a paragraph block, so we check the
        # block two back to see if it's a comment as we the comment needs to
        # be directly above the code block.
        if i >= 2 and is_mdx_comment(tokens[i - 2]):
            code_options |= extract_options_from_mdx_comment(tokens[i - 2].content)

        if lang in ("py", "python", "python3") and "notest" not in code_options:
            code_block = block.content

            if "continuation" in code_options:
                code_block = prev + code_block
                startline = -1  # this disables proper line numbers, TODO: adjust line numbers *per snippet*

            fixture_names = [
                f[len("fixture:") :] for f in code_options if f.startswith("fixture:")
            ]
            yield code_block, fixture_names, startline
            prev = code_block


def is_mdx_comment(block: "Token") -> bool:
    return (
        block.type == "inline"
        and block.content.strip().startswith("{/*")
        and block.content.strip().endswith("*/}")
        and "pmd-metadata:" in block.content
    )


def extract_options_from_mdx_comment(comment: str) -> typing.Set[str]:
    comment = (
        comment.strip()
        .replace("{/*", "")
        .replace("*/}", "")
        .replace("pmd-metadata:", "")
    )
    return set(option.strip() for option in comment.split(" ") if option)


def find_object_tests_recursive(
    module_name: str, object_name: str, object: typing.Any
) -> typing.Generator[typing.Tuple[str, typing.List[str], int], None, None]:
    docstr = inspect.getdoc(object)

    if docstr:
        yield from extract_code_blocks(docstr)

    for member_name, member in inspect.getmembers(object):
        if member_name.startswith("_"):
            continue

        if (
            inspect.isclass(member)
            or inspect.isfunction(member)
            or inspect.ismethod(member)
        ) and member.__module__ == module_name:
            yield from find_object_tests_recursive(module_name, member_name, member)


class MarkdownDocstringCodeModule(pytest.Module):
    def collect(self):
        if pytest.version_tuple >= (8, 1, 0):
            # consider_namespace_packages is a required keyword argument in pytest 8.1.0
            module = import_path(
                self.path, root=self.config.rootpath, consider_namespace_packages=True
            )
        else:
            # but unsupported before 8.1...
            module = import_path(self.path, root=self.config.rootpath)

        for i, (test_code, fixture_names, start_line) in enumerate(
            find_object_tests_recursive(module.__name__, module.__name__, module)
        ):
            yield MarkdownInlinePythonItem.from_parent(
                self,
                name=f"{self.path}#{i+1}",
                code=test_code,
                fixture_names=fixture_names,
                start_line=start_line,
                fake_line_numbers=True,  # TODO: figure out where docstrings are in file to offset line numbers properly
            )


class MarkdownTextFile(pytest.File):
    def collect(self):
        markdown_content = self.path.read_text("utf8")

        for code_block, fixture_names, start_line in extract_code_blocks(
            markdown_content
        ):
            yield MarkdownInlinePythonItem.from_parent(
                self,
                name=str(self.path),
                code=code_block,
                fixture_names=fixture_names,
                start_line=start_line,
                fake_line_numbers=start_line == -1,
            )


def pytest_collect_file(
    file_path,
    parent,
):
    if parent.config.option.markdowndocs:
        pathlib_path = pathlib.Path(str(file_path))  # pytest 7/8 compat
        if pathlib_path.suffix == ".py":
            return MarkdownDocstringCodeModule.from_parent(parent, path=pathlib_path)
        elif pathlib_path.suffix in (".md", ".mdx", ".svx"):
            return MarkdownTextFile.from_parent(parent, path=pathlib_path)

    return None


def pytest_configure(config):
    config.addinivalue_line(
        "markers", f"{MARKER_NAME}: filter for pytest-markdown-docs generated tests"
    )


def pytest_addoption(parser: Parser) -> None:
    group = parser.getgroup("collect")
    group.addoption(
        "--markdown-docs",
        action="store_true",
        default=False,
        help="run ",
        dest="markdowndocs",
    )


def pytest_addhooks(pluginmanager):
    pluginmanager.add_hookspecs(hooks)
