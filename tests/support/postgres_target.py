"""The one place a PostgreSQL test URL is resolved.

`POSTGRES_TEST_URL` is the only variable a PostgreSQL test reads, and this
module is the only code that reads `DATABASE_URL` anywhere in the repository.
That asymmetry is deliberate and is the whole of the argument:

  - The library under `src/` names neither variable. `approve_test_database`
    reads `POSTGRES_TEST_URL` and compares the live connection against it;
    it has no idea a production URL exists. The dependency-direction gate
    asserts the absence.
  - Whether those two URLs point at the SAME database is a question about
    test policy, so it lives here, on the test side of the line, where it can
    be answered.

NORMALISED, NOT COMPARED AS STRINGS
-----------------------------------
The check compares (host, port, database), not the URLs. Two URLs that differ
only in credentials, in query parameters, or in the ORDER of those parameters
reach the same database, and a raw string comparison would pass right through
that:

    postgresql://a:b@host/db?sslmode=require&channel_binding=require
    postgresql://a:c@host/db?channel_binding=require&sslmode=require

A safety check that those are different databases is a check that can be
evaded by typing a parameter in a different order, so it is not a check.

RAISES, DOES NOT SKIP
---------------------
A misconfiguration here RAISES rather than skipping, and that is the more
important of the two decisions. A skip renders in the summary as "no server
available", which is exactly what a correctly configured CI run looks like --
so a test suite pointed at a production URL would report itself as skipping
for want of a server, and nobody would look twice. Raising puts the error at
the top of the output, where it cannot be mistaken for the normal case.
"""
from __future__ import annotations

import os
from typing import Any

from personal_ai_core.persistence.postgres import (
    DatabaseIdentity,
    DatabaseVerdict,
    database_identity_of,
    inspect_database,
)

TEST_URL_VARIABLE = "POSTGRES_TEST_URL"
PRODUCTION_URL_VARIABLE = "DATABASE_URL"


def server_url() -> str:
    """`POSTGRES_TEST_URL`, verified to be a different database from
    `DATABASE_URL`. Returns "" when unset, so a caller can use it directly as a
    skip condition.

    Named `server_url` rather than `test_database_url` on purpose. A function
    called `test_*` that pytest imports into a module is COLLECTED as a test:
    it would be reported as a passing test in every run, and it returns a
    string, so pytest would also warn that a test returned a non-None value.
    A resolver that reports itself as a test is a small lie in every summary
    line that mentions it.

    Call this at MODULE level, not inside a fixture: a misconfiguration is a
    fact about the run, and it should surface during collection where it is
    visible, rather than halfway down a skip reason.
    """
    url = os.environ.get(TEST_URL_VARIABLE, "").strip()
    if not url:
        return ""

    production = os.environ.get(PRODUCTION_URL_VARIABLE, "").strip()
    if not production:
        return url

    # An unnormalisable URL on either side raises out of `database_identity_of`
    # rather than being skipped past. If we cannot reduce the two to a
    # comparable identity, we cannot prove they differ, and "we could not
    # check" is not a licence to run destructive tests.
    identity = database_identity_of(url)
    if database_identity_of(production) == identity:
        raise RuntimeError(
            f"{TEST_URL_VARIABLE} and {PRODUCTION_URL_VARIABLE} both name "
            f"{identity.host}:{identity.port}/{identity.database}. Refusing to "
            "run destructive tests against a production URL. Point "
            f"{TEST_URL_VARIABLE} at a disposable server, or unset "
            f"{PRODUCTION_URL_VARIABLE} for this run."
        )
    return url


def server_identity() -> DatabaseIdentity | None:
    """The test's own statement of which database `POSTGRES_TEST_URL` names,
    or `None` when no server is advertised.

    `None` rather than a raised error, and the distinction matters. Both
    server-gated modules call this at MODULE level, next to their skip marker,
    so the value has to exist before anything decides whether to run. If this
    raised on an absent URL, CI -- which advertises no server -- would get two
    COLLECTION ERRORS instead of two accounted skips, and a red build that says
    "URL names no host" rather than "no PostgreSQL server reached".

    The `None` is never consumed: a module that reaches `None` has already
    been skipped by the `pytestmark` beside it. It exists only so that
    collection survives the case where the answer is "there is nothing to
    connect to yet".

    Derived from the resolved URL on purpose, and the reason is worth being
    explicit about, because it looks like the thing the safety gate forbids.

    The gate's rule is that the LIBRARY must not derive the identity from the
    URL it is about to dial, because a confirmation read off your own input
    agrees with that input by construction. That is true of the library and it
    is exactly what this is not: here the URL and the belief about it come from
    the same place, on purpose, because hand-writing a host and a port into
    every test would mean editing twenty call sites the first time a container
    moved to a different port.

    What the identity check exists to catch is the backend reaching somewhere
    other than where it was told. That is caught by the tests which pass a
    DELIBERATELY WRONG identity and require the refusal -- see
    `test_connect_refuses_a_database_the_caller_did_not_confirm` and the
    drop-approval binding tests. Those are where the check earns its keep, and
    they would be no stronger if the other twenty call sites wrote their
    identities out by hand.
    """
    url = server_url()
    if not url:
        return None
    return database_identity_of(url)


# The value a server-gated module uses when no server is advertised.
#
# `server_identity()` returns `None` there, deliberately, because raising at
# module level would turn CI's accounted skips into collection errors. But a
# module-level CONSTANT has to have some value, and `None` is the wrong one:
# it would flow into `connect(identity=...)` as a silently missing
# confirmation, which is the exact failure this whole module exists to
# prevent.
#
# So the fallback is a `DatabaseIdentity` that names no reachable server. The
# host is not a hostname and the port is not a port, so `connect` refuses it
# against any real connection -- which means that if the skip marker beside
# this constant ever stops working, the tests fail LOUDLY and immediately
# rather than quietly dropping a required argument. A sentinel that could be
# dialled would be worse than no sentinel at all.
NO_SERVER_IDENTITY = DatabaseIdentity("<no server advertised>", 0, "<none>")


# What each relation kind has to be dropped AS. `relkind` is a one-character
# code and `DROP` will not accept it, so the mapping is stated rather than
# guessed at the call site. `r` and `p` share `TABLE` because PostgreSQL has
# used `DROP TABLE` for partitioned tables since they arrived; `i` and `I`
# share `INDEX` for the same reason.
_DROP_AS = {
    "r": "TABLE",
    "p": "TABLE",
    "v": "VIEW",
    "m": "MATERIALIZED VIEW",
    "S": "SEQUENCE",
    "f": "FOREIGN TABLE",
    "c": "TYPE",
    "i": "INDEX",
    "I": "INDEX",
}

# Every relkind this bootstrap will look for, and drop.
#
# A SUPERSET of the library's own `_USER_RELATION_KINDS`, and that is
# deliberate. The library classifies a schema without indexes -- every table has
# them, none is user-authored -- but the bootstrap has a different job: it must
# leave VACUOUS, and a standalone index someone created is still an object
# sitting in the schema. Dropping more than the gate counts is safe here
# precisely because this runs only against a disposable test database; dropping
# less would silently fail to reset, which is the failure mode that makes a
# suite unrecoverable.
#
# `test_the_bootstrap_can_drop_everything_the_gate_classifies` asserts that this
# is a superset, and `test_every_kind_the_bootstrap_drops_has_a_drop_keyword`
# asserts the mapping is total. Both run in CI with no server, because the bug
# they guard against is a mapping gap -- not a runtime one.
_BOOTSTRAP_RELATION_KINDS = tuple(sorted(_DROP_AS))


def reset_to_empty(url: str) -> None:
    """Force `url`'s target schema back to VACUOUS, bypassing the gate on purpose.

    This is a BOOTSTRAP, not a shortcut, and it is the only code in the
    repository that drops relations without an approval. It has to exist
    because the gate is now strict about what it will initialize against: a
    database left PARTIAL by an interrupted run cannot be opened with
    INITIALIZE -- correctly, that refusal is the whole point of the change --
    so without a raw reset the suite could never recover from its own mess, and
    a run that cannot recover is a run whose greenness means nothing.

    It drops EVERY user-defined relation in the target schema, not just this
    project's six tables, and that is not sloppiness -- it is the contract. An
    earlier version of this function dropped the six known names and then
    asserted the result was VACUOUS, which is a function that meets its own
    precondition only when nothing has gone wrong: one foreign table left by a
    crashed run, and the bootstrap's own assertion failed, erroring every test
    in the module. An assertion is the right thing to KEEP. Asserting it
    immediately after a half-meeting precondition is not.

    `url` is REQUIRED, and passed by the caller, rather than re-read from the
    environment here. That is not a style preference. Several tests
    monkeypatch `POSTGRES_TEST_URL` to a target that cannot approve anything,
    precisely so they can prove the approval is refused; if this function read
    the environment itself it would try to clean the monkeypatched target
    during their teardown, fail to connect to it, and leave the real database
    dirty. That is not hypothetical: it is exactly what happened while this was
    being written, and it presented as five unrelated test failures. The
    bootstrap must clean the database the module resolved at COLLECTION time,
    which is the only one the module ever talked to.

    Because `url` is explicit, this function makes no claim about where it
    points. That claim belongs to `server_url()`, which has already proved --
    by raising rather than skipping -- that the value its caller holds is not
    the production database.
    """
    if not url:
        raise RuntimeError(
            "reset_to_empty() was given no URL. Without one there is nothing to "
            f"reset, and guessing is exactly the accident this module exists to "
            f"prevent -- {PRODUCTION_URL_VARIABLE} is never a substitute."
        )

    import psycopg
    from psycopg.rows import dict_row

    # Typed `Any` for the same reason the library's own connections are not
    # annotated precisely: `row_factory` is assigned AFTER the connection is
    # opened, so the static type still claims the default tuple rows, and
    # every `row["..."]` below is then a type error against a type that is
    # already wrong. `row_factory` assignment then `execute` is how the driver
    # is meant to be used; annotating around it adds noise and no safety.
    connection: Any = psycopg.connect(url, autocommit=True)
    connection.row_factory = dict_row
    try:
        # Single-character relkinds from a hardcoded tuple, so quoting them
        # here is not string-building a value from outside -- it is listing a
        # fixed set. `quote_ident` is still used for the relation NAMES, which
        # do come from the server and can contain anything.
        kinds = ", ".join(f"'{kind}'" for kind in _BOOTSTRAP_RELATION_KINDS)
        for _ in range(8):
            remaining = connection.execute(
                "SELECT c.relkind, quote_ident(c.relname) AS quoted FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                f"WHERE n.nspname = current_schema() AND c.relkind IN ({kinds}) "
                "ORDER BY c.relname"
            ).fetchall()
            if not remaining:
                break
            for row in remaining:
                connection.execute(
                    f"DROP {_DROP_AS[row['relkind']]} IF EXISTS {row['quoted']} CASCADE"
                )
        else:
            raise RuntimeError(
                "reset_to_empty() did not converge on VACUOUS after 8 passes; a "
                "relation here is being recreated as fast as it is dropped"
            )
        # The library's OWN classifier says whether the bootstrap worked. Not a
        # re-implementation of the check: if `inspect_database` and this
        # function disagree about what VACUOUS means, the one that matters is
        # the one the gate will use on the next `connect`.
        assert inspect_database(connection).verdict is DatabaseVerdict.VACUOUS
    finally:
        connection.close()
