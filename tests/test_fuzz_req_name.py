"""Oracle-diff fuzzing of `_REQ_LINE_RE` against `packaging.requirements`.

Same standing technique used across the Go tools in this org (diff a
hand-rolled parser against the ecosystem's own canonical implementation as
an oracle): `_REQ_LINE_RE` re-implements enough of PEP 508 to pull a bare
package name out of a `pyproject.toml`/`requirements.txt` dependency spec,
and `packaging` (pip's own requirement parser) is the real spec. Anywhere
they disagree on the extracted name for a spec `packaging` accepts is
either a silently dropped dependency (false negative) or a wrong one
checked against the registry.

Two complementary strategies, like the Go fuzz targets: a structured one
that assembles syntactically-plausible specs (name/extras/specifier/marker)
so most examples are accepted by the oracle, and a charset-restricted raw
one that lets Hypothesis freely recombine the grammar's own punctuation to
turn up combinations a human wouldn't think to assemble.
"""
from __future__ import annotations

import string

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st
from packaging.requirements import InvalidRequirement, Requirement

from slopcheck.parsers import _REQ_LINE_RE

_SETTINGS = settings(
    max_examples=3000,
    suppress_health_check=[HealthCheck.filter_too_much, HealthCheck.too_slow],
)

_NAME_CHARS = string.ascii_letters + string.digits + "._-"
# PEP 508's formal grammar only allows space/tab as inter-token whitespace
# (`packaging`'s own tokenizer encodes the same rule: `WS = re.compile(r"[
# \t]+")`). `\n`/`\r`/`\f`/`\v` were in this alphabet until this fuzz run
# turned up a divergence on a spec with an embedded newline
# (`Requirement("0<\n0")` parses because the *SPECIFIER* token's own regex
# embeds a generic `\s*` around the operator — an incidental quirk of that
# one token's pattern, not a documented grammar allowance) — investigated
# against real usage and excluded as inert: no real requirements.txt line
# (already newline-split before reaching `_REQ_LINE_RE`) or pyproject.toml
# dependency string a human or tool would ever write contains a literal
# embedded newline mid-spec.
_WS = st.text(alphabet=" \t", max_size=4)
_EXTRA_NAME = st.text(alphabet=_NAME_CHARS, min_size=0, max_size=10)
_EXTRAS = st.one_of(
    st.none(),
    st.lists(_EXTRA_NAME, max_size=4).map(lambda xs: "[" + ",".join(xs) + "]"),
)
_SPEC_OPS = ["==", ">=", "<=", "!=", "~=", ">", "<", "==="]
_VERSION = st.text(alphabet=string.digits + ".*+abrcpostdev!", max_size=12)
_SPEC_CLAUSE = st.tuples(st.sampled_from(_SPEC_OPS), _VERSION).map("".join)
_SPECIFIER = st.one_of(
    st.none(),
    st.lists(_SPEC_CLAUSE, min_size=1, max_size=4).map(lambda xs: ",".join(xs)),
    # PEP 508's legacy parenthesized form, e.g. "(>=1.0,<2.0)" — the form
    # that turned up the real bug this harness now guards against.
    st.lists(_SPEC_CLAUSE, max_size=4).map(lambda xs: "(" + ",".join(xs) + ")"),
)
_MARKER_VAR = st.sampled_from(
    ["python_version", "os_name", "sys_platform", "platform_machine", "extra", "implementation_name"]
)
_MARKER_OP = st.sampled_from(["==", "!=", "<", "<=", ">", ">=", "in", "not in"])
_QUOTE = st.sampled_from(['"', "'"])
_MARKER_VALUE = st.text(alphabet=string.ascii_letters + string.digits + "._ <>!=;", max_size=15)
_BOOL_OP = st.sampled_from(["and", "or"])

_MARKER_CLAUSE = st.builds(
    lambda var, op, quote, value: f"{var} {op} {quote}{value}{quote}",
    _MARKER_VAR, _MARKER_OP, _QUOTE, _MARKER_VALUE,
)


@st.composite
def _marker_expr(draw):
    clauses = [draw(_MARKER_CLAUSE) for _ in range(draw(st.integers(1, 3)))]
    parts = [clauses[0]]
    for clause in clauses[1:]:
        parts.append(f" {draw(_BOOL_OP)} {clause}")
    return "".join(parts)


_MARKER = st.one_of(st.none(), _marker_expr())


@st.composite
def _requirement_spec(draw) -> str:
    parts = [draw(_WS), draw(st.text(alphabet=_NAME_CHARS, max_size=25))]
    extras = draw(_EXTRAS)
    if extras is not None:
        parts += [draw(_WS), extras]
    specifier = draw(_SPECIFIER)
    if specifier is not None:
        parts += [draw(_WS), specifier]
    marker = draw(_MARKER)
    if marker is not None:
        parts += [draw(_WS), ";", marker]
    return "".join(parts)


def _assert_name_matches(spec: str) -> None:
    try:
        parsed = Requirement(spec)
    except InvalidRequirement:
        assume(False)
        return
    if parsed.url is not None:
        # Direct URL/local references ("name @ https://...") aren't PyPI
        # registry deps at all; `_REQ_LINE_RE` correctly refuses to match
        # these so the spec is silently (and correctly) skipped, same as
        # the existing Poetry git/path/url and npm workspace/file/git
        # exclusions elsewhere in this file.
        assume(False)
        return
    match = _REQ_LINE_RE.match(spec.strip())
    assert match is not None, f"regex failed to match a spec packaging accepts: {spec!r}"
    assert match.group("name") == parsed.name, (
        f"name mismatch for {spec!r}: regex={match.group('name')!r} packaging={parsed.name!r}"
    )


@given(_requirement_spec())
@_SETTINGS
def test_req_line_re_matches_packaging_oracle_structured(spec: str) -> None:
    _assert_name_matches(spec)


_GRAMMAR_CHARS = string.ascii_letters + string.digits + "._-[]<>=!~; \t'\"@,()"


@given(st.text(alphabet=_GRAMMAR_CHARS, max_size=50))
@_SETTINGS
def test_req_line_re_matches_packaging_oracle_charset(spec: str) -> None:
    _assert_name_matches(spec)
