"""The destructive-entry gate, tested without a server.

Every test here is a pure function of its inputs: no connection, no
`POSTGRES_TEST_URL`, no skip. That is possible because `decide_verdict` takes a
`SchemaSnapshot` and returns a verdict, so the classification that decides
whether `_SCHEMA` or `_DROP_ALL` may run can be exercised directly, in
milliseconds, on a machine with no PostgreSQL at all.

The same principle governs the split: a rule that can only be tested
through a live database is a rule whose edge cases get skipped.

The server-dependent half -- that `connect` and `drop_all` actually honour the
verdict, and that the search-path pin holds -- is in
`tests/unit/test_postgres_backend.py`, which skips without a server. This file
does not, so these tests run in CI where those do not.
"""
from __future__ import annotations

import pytest
from support.postgres_target import _BOOTSTRAP_RELATION_KINDS, _DROP_AS

from personal_ai_core.persistence.postgres import (
    _APPROVAL_ALLOWS,
    _DROP_ALLOWS,
    _EXPECTED_RELATIONS,
    _EXPECTED_SEQUENCES,
    _EXPECTED_TABLES,
    _INITIALIZE_ALLOWS,
    _OPERATE_ALLOWS,
    _USER_RELATION_KINDS,
    DatabaseIdentity,
    DatabaseIdentityError,
    DatabaseVerdict,
    SchemaIntent,
    SchemaSnapshot,
    connect,
    database_identity_of,
    decide_verdict,
    live_identity_of,
)

ALL_TABLES = frozenset(_EXPECTED_TABLES)


# --- building snapshots -----------------------------------------------------


def snapshot(relations, columns=None) -> SchemaSnapshot:
    return SchemaSnapshot(dict(relations), dict(columns or {}))


def pac_snapshot(*, tables=None, sequences=True, extra=(), **claims) -> SchemaSnapshot:
    """A snapshot shaped the way this project's own schema looks.

    `**claims` overrides what a NAMED relation is claimed to be, as
    `name=(relkind, columns)`. It is a parameter rather than an assignment
    into the returned snapshot because `SchemaSnapshot` is frozen and its
    fields are `Mapping`: a test that wrote `snap.relations["users"] = "r"`
    would be relying on the runtime dict being more permissive than the type
    says, and would stop type-checking the moment the field were a real
    immutable mapping.
    """
    names = ALL_TABLES if tables is None else frozenset(tables)
    relations: dict[str, str] = {name: "r" for name in names}
    if sequences:
        relations.update({name: "S" for name in _EXPECTED_SEQUENCES})
    relations.update(dict(extra))
    columns = {name: _EXPECTED_TABLES[name] for name in names}
    for name, (kind, claimed_columns) in claims.items():
        relations[name] = kind
        columns[name] = claimed_columns
    return snapshot(relations, columns)


def verdict_of(snap: SchemaSnapshot) -> DatabaseVerdict:
    return decide_verdict(snap)[0]


def reason_of(snap: SchemaSnapshot) -> str:
    return decide_verdict(snap)[1]


# --- VACUOUS is emptiness, not absence of collision --------------------------


def test_an_empty_schema_is_vacuous():
    verdict, reason = decide_verdict(snapshot({}))
    assert verdict is DatabaseVerdict.VACUOUS
    assert reason


def test_a_database_of_purely_unrelated_tables_is_occupied_not_vacuous():
    """The hole this gate exists to close, as a pure function.

    Colliding with none of this project's six names is not the same as being
    empty. A database holding only `jobs`, `leads` and `paddle_*` matches no
    expected name, so a name-collision test alone classifies it VACUOUS, and
    INITIALIZE would then create six tables inside somebody else's
    application. The verdict has to be OCCUPIED, and it is.
    """
    assert verdict_of(snapshot({"jobs": "r", "leads": "r", "paddle_txns": "r"})) is (
        DatabaseVerdict.OCCUPIED
    )


def test_one_unrelated_table_is_enough_to_be_occupied():
    assert verdict_of(snapshot({"jobs": "r"})) is DatabaseVerdict.OCCUPIED


# --- PAC is exclusive --------------------------------------------------------


def test_exactly_the_tables_and_their_sequences_is_pac():
    """`_SCHEMA` leaves sequences behind, and a sequence is a relation.

    `seq BIGSERIAL` on messages, events and memories creates
    `<table>_<column>_seq`, so a correctly initialized database has nine
    user-defined relations, not six. Without expecting the three, every
    database this backend created would read as OCCUPIED and OPERATE would
    refuse its own work.
    """
    assert verdict_of(pac_snapshot()) is DatabaseVerdict.PAC


def test_pac_without_the_sequences_is_partial():
    """They are required, not tolerated-if-present."""
    assert verdict_of(pac_snapshot(sequences=False)) is DatabaseVerdict.PARTIAL


def test_the_expected_relations_are_the_six_tables_and_three_sequences():
    assert _EXPECTED_RELATIONS == ALL_TABLES | {
        "messages_seq_seq",
        "events_seq_seq",
        "memories_seq_seq",
    }
    assert len(ALL_TABLES) == 6
    assert len(_EXPECTED_SEQUENCES) == 3


def test_pac_tables_coexisting_with_a_foreign_relation_is_occupied():
    """All six correct, plus somebody else's table: not our database.

    This is a database this project was installed INTO, which is a different
    and worse situation than one it merely resembles.
    """
    assert verdict_of(pac_snapshot(extra={"jobs": "r"})) is DatabaseVerdict.OCCUPIED


def test_pac_tables_coexisting_with_a_foreign_sequence_is_occupied():
    assert verdict_of(pac_snapshot(extra={"audit_id_seq": "S"})) is (
        DatabaseVerdict.OCCUPIED
    )


# --- PARTIAL -----------------------------------------------------------------


@pytest.mark.parametrize("missing", sorted(ALL_TABLES))
def test_five_of_six_correct_tables_is_partial(missing):
    kept = ALL_TABLES - {missing}
    assert verdict_of(pac_snapshot(tables=kept)) is DatabaseVerdict.PARTIAL


def test_a_single_correct_table_is_partial():
    assert verdict_of(pac_snapshot(tables={"users"})) is DatabaseVerdict.PARTIAL


# --- FOREIGN: kind beats shape, shape beats completeness -------------------


def test_a_users_table_with_the_wrong_columns_is_foreign():
    snap = snapshot(
        {"users": "r"},
        {"users": (("id", "integer", True), ("email", "text", True))},
    )
    verdict, reason = decide_verdict(snap)
    assert verdict is DatabaseVerdict.FOREIGN
    assert "users" in reason and "columns" in reason


@pytest.mark.parametrize("kind", ["v", "m", "S", "f", "p"])
def test_a_users_that_is_not_an_ordinary_table_is_foreign(kind):
    """A relation of the right name and the wrong KIND is not this project's.

    A view called `users` whose columns happened to match the table's would
    otherwise pass a column-only comparison, and the backend would then insert
    into a view. A partitioned table is refused for the same reason: same name,
    different object, and `DROP TABLE` does not mean what it appears to.
    """
    snap = snapshot({"users": kind}, {"users": _EXPECTED_TABLES["users"]})
    verdict, reason = decide_verdict(snap)
    assert verdict is DatabaseVerdict.FOREIGN
    assert kind in reason


def test_a_wrong_shaped_claim_outranks_a_correct_count():
    """Shape beats completeness.

    Five tables correct and one wrong-shaped is FOREIGN, not PARTIAL. On a
    foreign database, "most of the tables matched" is exactly the answer that
    fails silently, so the partial reading is the dangerous one.
    """
    snap = pac_snapshot(
        tables=ALL_TABLES - {"memories"},
        users=("r", (("id", "integer", True),)),
    )
    assert verdict_of(snap) is DatabaseVerdict.FOREIGN


def test_a_wrong_shaped_claim_outranks_occupancy():
    snap = pac_snapshot(
        extra={"jobs": "r", "leads": "r"},
        users=("r", (("id", "integer", True),)),
    )
    assert verdict_of(snap) is DatabaseVerdict.FOREIGN


def test_a_wrong_kinded_claim_outranks_occupancy():
    snap = pac_snapshot(
        extra={"jobs": "r", "leads": "r"},
        users=("v", _EXPECTED_TABLES["users"]),
    )
    assert verdict_of(snap) is DatabaseVerdict.FOREIGN


def test_kind_is_only_consulted_for_names_this_project_claims():
    """A foreign VIEW called `jobs` is occupancy, not a collision.

    `decide_verdict` answers "is this our database", not "is everything in it
    ours". An unrelated relation of an unexpected kind is still unrelated.
    """
    assert verdict_of(snapshot({"jobs": "v"})) is DatabaseVerdict.OCCUPIED


# --- every verdict has a producer, and a consumer ---------------------------

PRODUCERS: dict[DatabaseVerdict, SchemaSnapshot] = {
    DatabaseVerdict.VACUOUS: snapshot({}),
    DatabaseVerdict.PAC: pac_snapshot(),
    DatabaseVerdict.PARTIAL: pac_snapshot(tables=ALL_TABLES - {"sessions"}),
    DatabaseVerdict.FOREIGN: snapshot({"users": "v"}),
    DatabaseVerdict.OCCUPIED: snapshot({"jobs": "r"}),
}


def test_every_verdict_is_produced_by_real_classification_logic():
    """No dead enum member, checked from the producers' side.

    An enum member nothing can produce is a verdict the gate can never reach,
    which usually means a branch was deleted and the member left behind
    asserting a behaviour that no longer exists.
    """
    assert set(PRODUCERS) == set(DatabaseVerdict)
    for member, snap in PRODUCERS.items():
        assert verdict_of(snap) is member, f"{member.value} is not produced"


def test_each_gate_admits_exactly_what_it_must():
    assert _INITIALIZE_ALLOWS == {DatabaseVerdict.VACUOUS}
    assert _OPERATE_ALLOWS == {DatabaseVerdict.PAC}
    assert _APPROVAL_ALLOWS == {DatabaseVerdict.VACUOUS, DatabaseVerdict.PAC}
    assert _DROP_ALLOWS == {DatabaseVerdict.VACUOUS, DatabaseVerdict.PAC}
    for allowed in (
        _INITIALIZE_ALLOWS,
        _OPERATE_ALLOWS,
        _APPROVAL_ALLOWS,
        _DROP_ALLOWS,
    ):
        assert allowed, "an allow-set that admits nothing is an allow-set with a bug"


# The ratified contract, written out. Refusals are the ABSENCE of a member from
# an allow-set, so a test cannot prove "handled" by looking at the union of the
# allow-sets -- three of the five verdicts appear in none of them, and that is
# correct. This table is the whole decision surface, and it is compared against
# the allow-sets both ways.
EXPECTED_DECISIONS = {
    DatabaseVerdict.VACUOUS: {
        "initialize": True, "operate": False, "approval": True, "drop": True,
    },
    DatabaseVerdict.PAC: {
        "initialize": False, "operate": True, "approval": True, "drop": True,
    },
    DatabaseVerdict.PARTIAL: {
        "initialize": False, "operate": False, "approval": False, "drop": False,
    },
    DatabaseVerdict.FOREIGN: {
        "initialize": False, "operate": False, "approval": False, "drop": False,
    },
    DatabaseVerdict.OCCUPIED: {
        "initialize": False, "operate": False, "approval": False, "drop": False,
    },
}

GATES = {
    "initialize": _INITIALIZE_ALLOWS,
    "operate": _OPERATE_ALLOWS,
    "approval": _APPROVAL_ALLOWS,
    "drop": _DROP_ALLOWS,
}


def test_the_decision_table_covers_every_verdict():
    """No dead enum member, from the consumers' side.

    A sixth `DatabaseVerdict` member is not a gap this test tolerates: it would
    not be in the table, and the mismatch is the failure. That is the point --
    a new verdict is a new decision, and it has to be made deliberately rather
    than inherited from whatever the allow-sets happen not to say.
    """
    assert set(EXPECTED_DECISIONS) == set(DatabaseVerdict)


def test_every_gate_decides_exactly_as_the_table_says():
    """Each of the twenty verdict/gate pairs, checked against the contract."""
    for verdict, row in EXPECTED_DECISIONS.items():
        for gate, allowed in GATES.items():
            assert (verdict in allowed) is row[gate], (
                f"{verdict.value} / {gate}: allow={verdict in allowed}, "
                f"contract says {row[gate]}"
            )


def test_occupied_is_refused_by_all_four_gates():
    """The fifth verdict is decided everywhere, not just classified."""
    for allowed in GATES.values():
        assert DatabaseVerdict.OCCUPIED not in allowed


# --- the normaliser ----------------------------------------------------------


BASE = "postgresql://pac:pw@localhost:5432/pac_test"


@pytest.mark.parametrize(
    "url",
    [
        BASE,
        "postgresql://pac:OTHER@localhost:5432/pac_test",
        "postgres://someone_else@localhost:5432/pac_test",
        "postgresql://pac:pw@localhost:5432/pac_test?sslmode=require",
        "postgresql://pac:pw@localhost:5432/pac_test?application_name=x",
        # These two are the same query parameters in the opposite order, and the
        # point of the pair is that the identity does not change. They are
        # parenthesised so the two lines read as ONE URL rather than as two
        # list entries that lost a comma -- which is the same thing ruff's
        # ISC004 warns about, and here it is a real risk in a test whose whole
        # subject is a list of URLs.
        (
            "postgresql://pac:pw@localhost:5432/pac_test?sslmode=require"
            "&channel_binding=require"
        ),
        (
            "postgresql://pac:pw@localhost:5432/pac_test?channel_binding=require"
            "&sslmode=require"
        ),
        "postgresql://LOCALHOST:5432/pac_test",
        "postgresql://pac:pw@localhost/pac_test",
    ],
    ids=[
        "baseline",
        "different-credentials",
        "different-user",
        "one-query-parameter",
        "an-extra-query-parameter",
        "two-parameters",
        "same-two-parameters-reordered",
        "host-case-insensitive",
        "port-defaults-to-5432",
    ],
)
def test_urls_naming_the_same_database_normalise_to_one_identity(url):
    """A safety check must survive a different spelling of the same URL.

    Credentials, query parameters, their order, the scheme and the host's case
    change none of WHICH database is reached. Any of them differing is how a
    production URL would slip past a raw string comparison.
    """
    assert database_identity_of(url) == database_identity_of(BASE)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://pac:pw@localhost:5433/pac_test",
        "postgresql://pac:pw@elsewhere:5432/pac_test",
        "postgresql://pac:pw@localhost:5432/other_db",
    ],
    ids=["different-port", "different-host", "different-database"],
)
def test_urls_naming_different_databases_do_not_normalise_together(url):
    """The counterpart: normalisation must not collapse genuinely different
    databases, or the check would be useless in the other direction."""
    assert database_identity_of(url) != database_identity_of(BASE)


def test_the_normaliser_compares_the_three_fields_the_gate_checks():
    assert database_identity_of(BASE) == DatabaseIdentity(
        host="localhost", port=5432, database="pac_test"
    )


@pytest.mark.parametrize(
    "url",
    [
        "postgresql:///pac_test",
        "postgresql://localhost:5432",
        "not-a-url",
    ],
    ids=["no-host-socket-url", "no-database", "not-a-url"],
)
def test_a_url_that_names_no_database_is_refused_rather_than_guessed(url):
    with pytest.raises(ValueError):
        database_identity_of(url)


def test_a_non_numeric_port_is_refused_rather_than_guessed():
    with pytest.raises(ValueError):
        database_identity_of("postgresql://pac:pw@localhost:not-a-port/db")


# --- live identity -----------------------------------------------------------


class _FakeInfo:
    def __init__(self, host, port, dbname):
        self.host = host
        self.port = port
        self.dbname = dbname


class _FakeConnection:
    def __init__(self, info):
        self.info = info


def test_the_live_identity_is_read_from_the_connections_own_report():
    connection = _FakeConnection(_FakeInfo("Example.COM", 5432, "pac_test"))
    assert live_identity_of(connection) == DatabaseIdentity(
        host="example.com", port=5432, database="pac_test"
    )


def test_a_non_tcp_connection_is_refused_rather_than_approximated():
    """A Unix socket reports a directory as its host and has no port.

    There is no host/port identity to confirm, and inventing one -- by
    treating the socket path as a host, say -- would compare the wrong thing
    and pass. So it is refused, and the message says what to do instead.
    """
    connection = _FakeConnection(_FakeInfo("/var/run/postgresql", None, "pac"))
    with pytest.raises(DatabaseIdentityError, match="TCP"):
        live_identity_of(connection)


# --- required arguments ------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"intent": SchemaIntent.INITIALIZE},
        {"identity": DatabaseIdentity("localhost", 5432, "pac_test")},
    ],
    ids=["neither", "intent-only", "identity-only"],
)
def test_connect_requires_both_intent_and_identity(kwargs):
    """No defaults, on either argument.

    A default is a value the caller did not choose, and a value derived from
    the URL would be a restatement of this function's own input. Either way
    the confirmation could not fail, which is the same as not having it. The
    refusal happens at call time, before the body runs, so this needs no driver.
    """
    with pytest.raises(TypeError):
        connect(BASE, **kwargs)


def test_the_two_arguments_are_keyword_only():
    with pytest.raises(TypeError):
        # Deliberately an invalid call: the whole point is that Python, not a
        # check inside the body, is what stops a positional identity. The
        # ignore is scoped to this one line for that reason -- suppressing it
        # anywhere wider would hide a real misuse.
        connect(BASE, SchemaIntent.INITIALIZE, DatabaseIdentity("h", 1, "d"))  # type: ignore[call-arg]


def test_the_intent_has_both_members_and_nothing_else():
    assert {m.name for m in SchemaIntent} == {"INITIALIZE", "OPERATE"}


# --- the test-side bootstrap -------------------------------------------------
# These two are about `tests/support/postgres_target.reset_to_empty`, and they
# are here, in the driver-free file, for a reason that is not tidiness.
#
# The bootstrap is the only code in the repository that drops relations without
# an approval, and its failure mode is not "a test fails" -- it is "the suite
# can no longer reach a clean starting state", which turns every subsequent
# result into noise. It is also the code whose bug is hardest to see: while
# this was being written, a version that dropped only the six known table names
# and then asserted VACUOUS errored all 58 tests in the server-gated module
# the first time anything foreign was left behind.
#
# Both properties below are MAPPING properties, checkable with no server. The
# runtime behaviour is verified by the suite starting from a deliberately
# filthy database, but the drift that would break it -- a relkind the gate
# classifies that the bootstrap does not know how to drop -- is exactly the
# kind that never appears in a normal run and so would never be caught there.


def test_the_bootstrap_can_drop_everything_the_gate_classifies():
    """A relkind the gate calls a user relation must be one the bootstrap drops.

    The bootstrap may know about MORE than the gate does -- it does, because a
    standalone index is not something the gate counts but is still an object in
    the schema. What it must never do is know about LESS, because a relation
    it cannot see is a relation that survives the reset, and the reset is what
    every later INITIALIZE depends on.
    """
    # Both sides are relkind codes, i.e. strings. `_USER_RELATION_KINDS` is a
    # module constant in `postgres.py`, so pyright infers its element type as
    # the literal union ("r" | "p" | ...), and `set[Literal[...]] - set[str]`
    # is an error it is right to report: the operand types genuinely do not
    # line up. The annotated locals widen both to `set[str]`, which is what
    # they are, and make the comparison well-typed without a cast or an
    # ignore. Runtime behaviour is unchanged.
    gates: set[str] = set(_USER_RELATION_KINDS)
    bootstraps: set[str] = set(_BOOTSTRAP_RELATION_KINDS)
    assert gates <= bootstraps, (
        "the gate classifies relkinds the bootstrap cannot drop: "
        f"{sorted(gates - bootstraps)}"
    )


def test_every_kind_the_bootstrap_drops_has_a_drop_keyword():
    """`relkind` is a code; `DROP` needs a word. The mapping must be total.

    A missing entry would raise `KeyError` from inside the reset -- after the
    caller has already been told the reset was in progress, and with the
    database half-cleared. Catching a gap here costs nothing and runs in CI.
    """
    assert set(_BOOTSTRAP_RELATION_KINDS) == set(_DROP_AS)
    assert all(keyword for keyword in _DROP_AS.values())
    # And the keywords are ones PostgreSQL actually accepts, not placeholders.
    assert set(_DROP_AS.values()) == {
        "TABLE",
        "VIEW",
        "MATERIALIZED VIEW",
        "SEQUENCE",
        "FOREIGN TABLE",
        "TYPE",
        "INDEX",
    }
