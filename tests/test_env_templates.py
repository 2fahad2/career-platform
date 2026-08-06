"""The environment templates against what the code actually reads (P1-8).

A `.env*.example` is not documentation. It is the checklist a HOST IS BUILT
FROM — the disaster runbook says so literally, and the whole of appendix one is
«fill the secrets file from these names». So a key the code reads and the
template omits is not a missing comment: it is a production host that boots
green and is silently wrong.

The omissions found on 2026-08-05, all in `.env.production.example`, and what
each one would have done to a host built from it:

  SALLA_PRODUCT_CATALOG   defaults to `{}` → every paid order provisions
                          NOTHING; the customer pays and receives silence.
  SALLA_PRODUCT_PRICING   defaults to `{}` → the §09 triple match has no price
                          to match and refuses every order it does see.
  SEARCHAPI_API_KEY       missing → the nightly exits 2 and never runs; no
                          customer is ever searched for.
  WHATSAPP_WABA_ID        missing → the template probe is silently skipped, so
                          a PENDING template is discovered by a failed send.
  SALLA_CLIENT_ID/SECRET/REFRESH_TOKEN
                          absent → when the access token dies (it does), there
                          is nothing on the host to refresh it with.

Hand-checking that list is what produced it in the first place. So the list is
not the deliverable — this file is: the required set is DERIVED from the
`Settings` fields and from the compose interpolations, and drift in either
direction fails here.

Two corrections, 2026-08-06, both to this file rather than to the templates:

* The derivation read `{field.alias for … if field.alias}`. Every field
  happens to declare an alias today, so the set looked complete — but
  pydantic-settings populates an ALIAS-LESS field from its own name uppercased,
  with no alias anywhere to collect. The next `foo: str = ""` written without
  `Field(alias=...)` would have been read from `FOO` in production, absent from
  all three templates, and green here: verbatim the failure the file exists to
  prevent, arriving through the one door it was not watching.
  `test_an_alias_less_field_is_still_an_environment_variable` proves the fix
  against pydantic itself rather than against a belief about pydantic.
* The docstring above used to say the required set was derived from «the ops
  scripts» too. It was not, and it cannot honestly be: the shell in `ops/` and
  `scripts/` is full of `$WORK`, `$TABLES`, `$RESTORED` — local variables that
  have nothing to do with the secrets file. `_UNREAD_BUT_REQUIRED` below is a
  hand-written list and now says so, with a guard that keeps every entry
  honest: the day a key there acquires a real reader, it must leave.
"""

from __future__ import annotations

import pathlib
import re

import pytest

TEMPLATES = (
    ".env.example",
    ".env.staging.example",
    ".env.production.example",
)

#: Keys no `Settings` field reads and no compose file interpolates, which the
#: templates must still carry. HAND-WRITTEN, deliberately and unavoidably:
#: there is no artefact in this repository to derive them from, because their
#: reader is either docker itself or a human with the runbook open. Each is a
#: real thing a rebuilt host needs; the value is WHY, and the tests below
#: insist that the why is written next to the key in the file too — an
#: unexplained key that nothing reads is a key the next person deletes — and
#: that the key really does have no reader, so an entry cannot quietly become
#: a duplicate of a `Settings` field nobody noticed.
_UNREAD_BUT_REQUIRED = {
    # docker compose itself reads this from --env-file: it names the project
    # whose volumes hold the database and every customer's files. Wrong value,
    # and `compose up` builds a second empty environment beside the real one.
    "COMPOSE_PROJECT_NAME": "docker compose project isolation",
    # The Salla OAuth trio. The code does not refresh the token yet — a human
    # does, and he cannot do it from a host that does not have these.
    "SALLA_CLIENT_ID": "manual Salla token refresh",
    "SALLA_CLIENT_SECRET": "manual Salla token refresh",
    "SALLA_REFRESH_TOKEN": "manual Salla token refresh",
    # Operator reference for the Meta dashboard (runbook appendix one, group 5)
    "META_APP_ID": "Meta dashboard reference",
}

#: Secrets that live in the BACKUP env file (/root/.config/career-backup/
#: backup.env), not in .env — the runbook names them beside the others, and
#: they must never migrate into a tracked template.
_BACKUP_ENV_KEYS = frozenset(
    {"RESTIC_REPOSITORY", "RESTIC_PASSWORD", "B2_ACCOUNT_ID", "B2_ACCOUNT_KEY"}
)


def _entries(path: str) -> dict[str, str]:
    """KEY → raw value for one template."""
    out: dict[str, str] = {}
    for line in pathlib.Path(path).read_text(encoding="utf-8").splitlines():
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line)
        if match:
            out[match.group(1)] = match.group(2).strip()
    return out


def _settings_env_names(model: type = None) -> set[str]:  # type: ignore[assignment]
    """Every environment variable a settings field reads — the authority.

    An alias when there is one, and the field's own name uppercased when there
    is not. That second half is the whole point: pydantic-settings does not
    require an alias to read the environment, so «has an alias» was never the
    same question as «is read from the environment», and the first version of
    this function asked the wrong one.
    """
    if model is None:
        from career.config import Settings

        model = Settings
    return {
        field.alias or name.upper()
        for name, field in model.model_fields.items()  # type: ignore[attr-defined]
    }


def _compose_variables() -> set[str]:
    """Every variable the compose files interpolate. These never reach
    `Settings` at all: they are consumed by docker before the app exists, so an
    absent one becomes an empty published port or an empty postgres password."""
    found: set[str] = set()
    for path in sorted(pathlib.Path().glob("docker-compose.*.yml")):
        found |= set(
            re.findall(r"\$\{([A-Z][A-Z0-9_]*)\}", path.read_text(encoding="utf-8"))
        )
    return found


def _required_keys() -> set[str]:
    return _settings_env_names() | _compose_variables() | set(_UNREAD_BUT_REQUIRED)


# ── the derivation itself, checked against pydantic rather than against faith ─


def test_an_alias_less_field_is_still_an_environment_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hole the collector had, closed and then walked into on purpose.

    Two things are asserted, and only together do they mean anything: that
    pydantic-settings really does populate a field with no alias from its own
    name uppercased (so the derivation is a fact about the library, not a
    guess), and that the collector now returns that name. Assert only the
    second and the guard is a mirror; assert only the first and nothing
    connects it to the templates.
    """
    from pydantic import Field
    from pydantic_settings import BaseSettings, SettingsConfigDict

    class Drifting(BaseSettings):
        model_config = SettingsConfigDict(env_file=None, extra="ignore")

        aliased: str = Field(default="", alias="ALIASED_KEY")
        # written by the next person in a hurry — no Field(), no alias
        forgotten_key: str = ""

    monkeypatch.setenv("FORGOTTEN_KEY", "it-was-read-all-along")
    assert Drifting().forgotten_key == "it-was-read-all-along"
    assert _settings_env_names(Drifting) == {"ALIASED_KEY", "FORGOTTEN_KEY"}


@pytest.mark.parametrize("template", TEMPLATES)
def test_the_drift_check_would_catch_an_alias_less_field(template: str) -> None:
    """And the collector's answer reaches the assertion.

    A settings field is added, no alias, nobody touches the templates: the
    missing-key computation the three tests below run must name it. This is
    the failure that was green.
    """
    invented = "A_FIELD_NOBODY_ADDED_TO_THE_TEMPLATES"
    required = _required_keys() | {invented}
    assert invented in required - set(_entries(template))


def test_the_derivation_assumes_no_env_prefix() -> None:
    """`NAME.upper()` is the right env name only while there is no prefix.

    Setting `env_prefix` would rename every alias-less field's variable at once
    and this collector would go back to being confidently wrong — so the
    assumption is asserted instead of remembered.
    """
    from career.config import Settings

    assert not Settings.model_config.get("env_prefix"), (
        "Settings grew an env_prefix — _settings_env_names must prepend it"
    )


def test_every_unread_key_really_has_no_reader() -> None:
    """The hand-written list has to stay hand-written for a stated reason.

    An entry that acquires a `Settings` field or a compose interpolation is no
    longer «a key nothing reads»; leaving it here would keep a false reason in
    front of the next reader and would hide the fact that the code now depends
    on it. Move it out — the derived sets already carry it.
    """
    derived = _settings_env_names() | _compose_variables()
    overlap = sorted(set(_UNREAD_BUT_REQUIRED) & derived)
    assert not overlap, (
        f"these are listed as read by nothing but are now derived: {overlap} — "
        "delete them from _UNREAD_BUT_REQUIRED, the reason beside them is stale"
    )


# ── the drift itself ─────────────────────────────────────────────────────────


@pytest.mark.parametrize("template", TEMPLATES)
def test_the_template_carries_every_key_the_code_reads(template: str) -> None:
    """Adding a `Field(..., alias=...)` and forgetting the templates is the
    exact mistake this catches, one commit after it is made rather than one
    outage after."""
    missing = sorted(_required_keys() - set(_entries(template)))
    assert not missing, (
        f"{template} is missing keys the code reads: {missing} — a host built "
        "from this file would run on their defaults and say nothing"
    )


@pytest.mark.parametrize("template", TEMPLATES)
def test_the_template_carries_no_key_nothing_reads(template: str) -> None:
    """Drift in the other direction is quieter and just as bad: the operator
    sets a value during a rebuild, believes it took effect, and it does
    nothing. A renamed setting leaves exactly this residue."""
    extra = sorted(set(_entries(template)) - _required_keys())
    assert not extra, (
        f"{template} carries keys nothing reads: {extra} — either the code "
        "stopped reading them (delete) or they belong in _UNREAD_BUT_REQUIRED "
        "with the reason written down"
    )


@pytest.mark.parametrize("template", TEMPLATES)
def test_an_unread_key_says_why_it_exists(template: str) -> None:
    """A key with no reader and no comment is indistinguishable from a stale
    one. Every such key must be introduced by a comment IN THE FILE, where the
    person rebuilding the host at 3am is actually looking."""
    lines = pathlib.Path(template).read_text(encoding="utf-8").splitlines()
    for key in _UNREAD_BUT_REQUIRED:
        index = next(
            (i for i, line in enumerate(lines) if line.startswith(f"{key}=")), None
        )
        assert index is not None, f"{template}: {key} is missing"
        # its block = upwards to the nearest blank line, so one comment may
        # introduce a group (the three OAuth keys are one fact, not three)
        block = []
        cursor = index - 1
        while cursor >= 0 and lines[cursor].strip():
            block.append(lines[cursor])
            cursor -= 1
        assert any(line.lstrip().startswith("#") for line in block), (
            f"{template}: {key} is read by nothing and explained by nothing"
        )


# ── defaults that are not merely absent but actively wrong ───────────────────


@pytest.mark.parametrize("template", TEMPLATES)
def test_storage_root_is_absolute(template: str) -> None:
    """`storage_root` defaults to a RELATIVE `./data`, and that default is a
    trap rather than a gap: two processes with different working directories
    resolve it to two different trees, so a tenant's CVs split across both and
    the backup — which is pointed at ONE absolute path — covers half of them.
    Nothing errors; the files are simply not where the next reader looks."""
    value = _entries(template)["STORAGE_ROOT"]
    assert value.startswith("/"), (
        f"{template}: STORAGE_ROOT={value!r} is relative — it resolves against "
        "each process's working directory and splits the tenant files"
    )


def test_the_published_api_port_matches_the_probe() -> None:
    """`api_publish_port` defaults to 8000 while staging publishes 8001, and
    the deploy-drift probe (run_admin_bot._deployed_source) asks the API on
    that port what code it is running. A wrong port does not fail: the probe
    returns None and the screen reads «unknown» — which is how thirty-two
    commits stayed undeployed for four days behind an all-green board."""
    staging = _entries(".env.staging.example")["API_PUBLISH_PORT"]
    probe = pathlib.Path("scripts/verify_restore.sh").read_text(encoding="utf-8")
    ports = set(re.findall(r"http://127\.0\.0\.1:(\d+)/health", probe))
    assert ports == {staging}, (
        f"the staging template publishes the API on {staging} but the restore "
        f"verification probes {sorted(ports)} — one of them is asking a port "
        "nothing answers on, and a silent probe reports «unknown», not «down»"
    )


# ── placeholders stay placeholders ───────────────────────────────────────────


#: Values that must never be shared between the live file and a tracked one.
#: Secrets by name, plus the two keys that are not secrets but ARE personal
#: data — the operator's own phone numbers (§15.13: no PII in the repo either).
_PRIVATE_KEYS = ("CANARY_TEST_PHONE", "WHATSAPP_NUMBER_E164")


def _is_secretish(key: str) -> bool:
    return key.endswith(("_PASSWORD", "_SECRET", "_TOKEN", "_API_KEY"))


def _is_private(key: str) -> bool:
    return _is_secretish(key) or key in _PRIVATE_KEYS


@pytest.mark.parametrize("template", TEMPLATES)
def test_placeholders_are_obviously_placeholders(template: str) -> None:
    """§15.15: no secret in git, ever. A template value that merely LOOKS
    plausible is the dangerous kind — it gets committed and nobody can tell it
    from the real one. Blank, or shouting CHANGE-ME."""
    for key, value in _entries(template).items():
        if _is_secretish(key) and value:
            assert "change-me" in value.lower(), (
                f"{template}: {key} carries a value that is not visibly a "
                "placeholder"
            )


def test_no_template_repeats_a_live_secret() -> None:
    """The failure mode this exists for is mechanical: someone fixes a
    template by copying the live `.env.staging` and deleting «the secret
    ones». Compare against the real file when it is present and refuse."""
    live = pathlib.Path(".env.staging")
    if not live.exists():  # a checkout without the host's secrets
        pytest.skip("no live .env.staging on this machine")
    # Only the private half: a shared `career_app` or `5432` is the two files
    # agreeing about a non-secret, which is the point of a template.
    real = {
        value.strip()
        for key, value in _entries(".env.staging").items()
        if _is_private(key) and len(value.strip()) >= 8
    }
    for template in TEMPLATES:
        for key, value in _entries(template).items():
            # the assertion message names the KEY only — never the value
            assert value not in real, f"{template}: {key} holds a LIVE value"


# ── the third copy of the same fact: the recovery runbook ────────────────────


def test_the_recovery_runbook_promises_no_key_the_templates_lack() -> None:
    """Appendix one of docs/RUNBOOK-DISASTER-RECOVERY.md is the list a human
    fills the secrets file from, by hand, after the server is gone. A key named
    there and absent from the templates sends him looking for a value that has
    no slot; the backup-env secrets are the deliberate exception."""
    text = pathlib.Path("docs/RUNBOOK-DISASTER-RECOVERY.md").read_text(
        encoding="utf-8"
    )
    appendix = text.split("## الملحق الأول", 1)[1].split("## الملحق الثاني", 1)[0]
    named = {
        line.strip()
        for line in appendix.splitlines()
        if re.fullmatch(r"[A-Z][A-Z0-9_]{3,}", line.strip())
    } - _BACKUP_ENV_KEYS
    known = set(_entries(".env.staging.example"))
    assert not named - known, (
        "the recovery runbook names keys the staging template does not have: "
        f"{sorted(named - known)}"
    )
