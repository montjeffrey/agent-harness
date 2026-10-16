"""Family G: code relations at scale (Hugo v0.128.0, pinned e6d2712).

Four read-only, single-session questions about a ~104k-line Go codebase. The
agent changes nothing; each check scores only its final ``ANSWER:`` line.

How the ground truth below was derived (once, by hand, against the pristine
clone at e6d2712ee062321dc2fc49e963597dd5a6157660):

- G1/G2 (callers): ``benchmarks/token-efficiency/gocallers`` (go/ast walk over
  every non-_test.go file, enclosing FuncDecl of each ``pkg.Name(...)`` call or
  bare ``Name(...)`` inside pkg). Cross-checked with a second, import-resolving
  go/ast indexer (resolves import aliases, also records package-level var
  initialisers and non-call references) and with ``grep -rnw`` plus a manual
  look at the enclosing ``func`` of every hit. Both tools agree; neither
  function is referenced other than by direct calls, is recursive, or is
  called from a package-level initialiser. All function names are unique
  across the repo (no other func/method declares the same name).
  Note for G1: js/build.go splits the call across lines (``herrors.`` /
  ``NewFileErrorFromName(``), so a literal ``herrors.NewFileErrorFromName``
  grep misses ``buildTransformation.Transform``.
- G3 (change impact): ``go list -f '{{.ImportPath}}|{{.Imports}}|{{.GoFiles}}'
  ./...`` -> every package whose non-test GoFiles mention the identifier as a
  whole word (excluding its declaration), intersected with packages importing
  common/types; confirmed with the import-resolving indexer. gocallers alone
  misses config/allconfig, whose call sits in a closure inside a package-level
  ``var`` (alldecoders.go). Distractor: modules only calls
  ``config.GetStringSlicePreserveString`` (a wrapper) and is NOT impacted.
- G4 (data flow): read by hand across hugolib/site.go (``Site.shouldBuild`` ->
  ``shouldBuild``), resources/page/pagemeta/page_frontmatter.go
  (``newDefaultFrontmatterConfig``: PublishDate = [publishdate, date], aliases
  pubdate/published added by ``addDateFieldAliases``) and
  config/allconfig/load.go / commands/commandeer.go (``buildFuture`` defaults
  to false). A page with only ``date`` in the future gets PublishDate = date,
  and ``shouldBuild`` returns false because ``!buildFuture &&
  publishDate.After(now)``.
"""

from __future__ import annotations

import re
from pathlib import Path

from tasks_common import answer_line, read

MOD = "github.com/gohugoio/hugo"


# --------------------------------------------------------------------------- helpers

def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[,;\s]+", text or "") if t]


def _norm_ident(tok: str) -> str:
    """`(*hugolib.pageState).parseError()` -> `hugolib.pagestate.parseerror`."""
    t = tok.strip("`'\"").replace("(", "").replace(")", "").replace("*", "")
    t = t.rstrip(".:")
    t = t.rsplit("/", 1)[-1]          # drop an import-path prefix if present
    return t.lower()


def _caller_forms(expected: list[str]) -> dict[str, str]:
    """Accepted spellings -> canonical entry. Each expected entry is
    ``pkg.Func`` or ``pkg.Recv.Method``; it may be given with or without the
    package name."""
    forms: dict[str, str] = {}
    for e in expected:
        for f in (e.lower(), e.split(".", 1)[1].lower()):
            if forms.get(f, e) != e:
                raise RuntimeError(f"ambiguous caller spelling {f!r}")
            forms[f] = e
    return forms


def _caller_check(expected: list[str]):
    forms = _caller_forms(expected)
    want = set(expected)

    def check(clone: Path, result: str) -> tuple[bool, str]:
        toks = _tokens(answer_line(result))
        if not toks:
            return False, "no ANSWER line / empty answer"
        got, unknown = set(), []
        for t in toks:
            n = _norm_ident(t)
            if n in forms:
                got.add(forms[n])
            elif n:
                unknown.append(t)
        missing = sorted(want - got)
        if unknown or missing:
            return False, f"missing={missing} unexpected={unknown}"
        return True, f"all {len(want)} callers, nothing extra"
    return check


def _norm_pkg(tok: str) -> str:
    t = tok.strip("`'\"").rstrip("/.:").removeprefix("./")
    t = t.removeprefix(MOD).lstrip("/")
    return t.lower()


def _anchor(clone: Path, rel: str, needle: str) -> None:
    if needle not in read(clone, rel):
        raise RuntimeError(f"ground truth stale: {needle!r} not in {rel}")


def _answer(items) -> str:
    return "ANSWER: " + ", ".join(items)


CALLER_FORMAT = (
    "Count calls made inside closures/func literals as calls from the enclosing "
    "named function or method; count calls from the function's own package too. "
    "Ignore _test.go files. Do not modify any files.\n\n"
    "End your reply with exactly one line of the form\n"
    "ANSWER: name1, name2, ...\n"
    "where each name is `Func` for a plain function or `Receiver.Method` for a "
    "method (receiver type name only: no `*`, no parentheses). A package-name "
    "prefix is optional (`hugolib.Site.Build` and `Site.Build` are both fine). "
    "Order does not matter. The answer is scored as an exact set: every caller, "
    "and nothing else."
)


# --------------------------------------------------------------------------- G1

G1_CALLERS = [
    "cssjs.importResolver.toFileError",
    "herrors.NewFileErrorFromFile",
    "hugolib.doRenderShortcode",
    "hugolib.pageState.parseError",
    "i18n.errWithFileContext",
    "js.buildTransformation.Transform",
    "metadecoders.toFileError",
    "tplimpl.templateHandler.addFileContext",
    "tplimpl.templateInfo.errWithFileContext",
    "transform.Chain.Apply",
]


def _g1_reference(clone: Path) -> str:
    _anchor(clone, "common/herrors/file_error.go", "func NewFileErrorFromName(err error, name string) FileError")
    _anchor(clone, "resources/resource_transformers/js/build.go", "NewFileErrorFromName(errors.New(errorMessage), path)")
    return _answer(G1_CALLERS)


# --------------------------------------------------------------------------- G2

G2_CALLERS = [
    "create.Client.FromString",
    "data.Namespace.getRemote",
    "resources.imageResource.relTargetPathFromConfig",
    "resources.resourceAdapter.TransformationKey",
    "source.File.init",
    "templates.Namespace.DoDefer",
    "tplimpl.templateContext.handleDefer",
    "transform.Namespace.Unmarshal",
]


def _g2_reference(clone: Path) -> str:
    _anchor(clone, "helpers/general.go", "func MD5String(f string) string")
    return _answer(G2_CALLERS)


# --------------------------------------------------------------------------- G3

G3_PACKAGES = [
    "config",
    "config/allconfig",
    "config/security",
    "hugolib",
    "hugolib/filesystems",
    "resources/resource_factories/create",
    "tpl/data",
]


def _g3_check(clone: Path, result: str) -> tuple[bool, str]:
    got = {_norm_pkg(t) for t in _tokens(answer_line(result))} - {""}
    if not got:
        return False, "no ANSWER line / empty answer"
    want = {p.lower() for p in G3_PACKAGES}
    missing, extra = sorted(want - got), sorted(got - want)
    if missing or extra:
        return False, f"missing={missing} unexpected={extra}"
    return True, f"all {len(want)} packages, nothing extra"


def _g3_reference(clone: Path) -> str:
    _anchor(clone, "common/types/convert.go", "func ToStringSlicePreserveString(v any) []string")
    _anchor(clone, "config/allconfig/alldecoders.go", "types.ToStringSlicePreserveString(v)")
    return _answer(f"{MOD}/{p}" for p in G3_PACKAGES)


# --------------------------------------------------------------------------- G4

def _g4_check(clone: Path, result: str) -> tuple[bool, str]:
    toks = [_norm_ident(t).rsplit(".", 1)[-1] for t in _tokens(answer_line(result))]
    toks = [t for t in toks if t]
    if not toks:
        return False, "no ANSWER line / empty answer"
    problems = []
    verdicts = {"built", "skipped"} & set(toks)
    if verdicts != {"skipped"}:
        problems.append(f"verdict must be exactly 'skipped', got {sorted(verdicts) or 'none'}")
    for need in ("shouldbuild", "date", "newdefaultfrontmatterconfig"):
        if need not in toks:
            problems.append(f"missing {need!r}")
    if problems:
        return False, "; ".join(problems)
    return True, "skipped / shouldBuild / date / newDefaultFrontmatterConfig"


def _g4_reference(clone: Path) -> str:
    _anchor(clone, "hugolib/site.go", "if !buildFuture && !publishDate.IsZero() && publishDate.After(hnow) {")
    _anchor(clone, "resources/page/pagemeta/page_frontmatter.go", "PublishDate: []string{fmPubDate, fmDate},")
    return "ANSWER: skipped, shouldBuild, date, newDefaultFrontmatterConfig"


# --------------------------------------------------------------------------- tasks

TASKS = {
    "G1": dict(
        family="G", repo="hugo", setup=None,
        prompt=(
            "In this Go repository (Hugo), list every non-test function or method "
            "that calls `herrors.NewFileErrorFromName` (the function declared in "
            "package github.com/gohugoio/hugo/common/herrors).\n\n" + CALLER_FORMAT
        ),
        check=_caller_check(G1_CALLERS),
        reference=_g1_reference,
    ),
    "G2": dict(
        family="G", repo="hugo", setup=None,
        prompt=(
            "In this Go repository (Hugo), list every non-test function or method "
            "that calls `helpers.MD5String` (the function declared in package "
            "github.com/gohugoio/hugo/helpers).\n\n" + CALLER_FORMAT
        ),
        check=_caller_check(G2_CALLERS),
        reference=_g2_reference,
    ),
    "G3": dict(
        family="G", repo="hugo", setup=None,
        prompt=(
            "In this Go repository (Hugo): if the signature of the function "
            "`ToStringSlicePreserveString` in package "
            "github.com/gohugoio/hugo/common/types changed (say it gained a second "
            "parameter), which Go packages contain non-test code that would fail to "
            "compile? Count only packages with a direct call or reference to that "
            "exact function in non-_test.go files (calls inside closures and "
            "package-level variable initialisers count; callers of other functions "
            "that merely wrap it do not). Exclude common/types itself. Do not modify "
            "any files.\n\n"
            "End your reply with exactly one line of the form\n"
            "ANSWER: importpath1, importpath2, ...\n"
            "using full import paths (e.g. github.com/gohugoio/hugo/hugolib). Order "
            "does not matter. The answer is scored as an exact set."
        ),
        check=_g3_check,
        reference=_g3_reference,
    ),
    "G4": dict(
        family="G", repo="hugo", setup=None,
        prompt=(
            "In this Go repository (Hugo), trace the following without running Hugo "
            "and without modifying any files.\n\n"
            "A regular content page's front matter sets only `date: 2999-01-01` "
            "(no publishDate/pubdate/published, no expiryDate, not a draft). The "
            "site uses the default configuration (no `frontmatter` settings) and is "
            "built without --buildFuture/--buildDrafts/--buildExpired.\n\n"
            "1. Is the page built or skipped?\n"
            "2. Name the plain (non-method) Go function that makes that decision by "
            "comparing the page's publish date with the current time.\n"
            "3. From which front matter key does that publish date come?\n"
            "4. Name the Go function whose body holds Hugo's built-in default list "
            "of front matter keys consulted for PublishDate.\n\n"
            "End your reply with exactly one line of the form\n"
            "ANSWER: <built|skipped>, <function for 2>, <front matter key for 3>, "
            "<function for 4>\n"
            "using the single word `built` or `skipped` for 1 and bare Go "
            "identifiers for 2 and 4."
        ),
        check=_g4_check,
        reference=_g4_reference,
    ),
}
