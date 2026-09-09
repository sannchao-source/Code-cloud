"""Run collectors across accounts and assemble a report of what is new."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .collectors import COLLECTORS
from .config import Account
from .graph import GraphClient
from .models import KIND_AD_COMMENT, KIND_ORDER, CollectionResult, Item
from .state import State
from . import tokens, triage

log = logging.getLogger(__name__)


@dataclass
class AccountReport:
    account: Account
    results: list[CollectionResult] = field(default_factory=list)
    # Set when the account could not be reached at all, e.g. no token.
    fatal: str | None = None

    @property
    def new_items(self) -> list[Item]:
        items: list[Item] = []
        for result in self.results:
            items.extend(result.items)
        # Most severe first, then newest -- so a complaint never sits
        # below a run of ordinary comments that happen to be newer.
        return sorted(
            items,
            key=lambda i: (i.severity, i.sort_key),
            reverse=True,
        )

    @property
    def flagged(self) -> list[Item]:
        return [i for i in self.new_items if i.needs_attention]

    @property
    def complaints(self) -> list[Item]:
        return [i for i in self.new_items
                if i.severity >= triage.SEVERITY_COMPLAINT]

    @property
    def problems(self) -> list[CollectionResult]:
        """Everything not read this run, for display in the digest."""
        return [r for r in self.results if r.error or r.skipped_reason]

    @property
    def failures(self) -> list[CollectionResult]:
        """Only what actually broke.

        A source that is unavailable -- not configured, or switched off on
        the Page -- is a standing fact, not a fault, and reports identically
        forever. Counting those as failures pinned the exit code to 1
        permanently, and an exit code that never changes cannot tell a
        healthy run from a broken one. On Windows that code is
        LastTaskResult, the only health signal the scheduler exposes.
        """
        return [r for r in self.results if r.error]


@dataclass
class Report:
    accounts: list[AccountReport] = field(default_factory=list)
    # Conditions that are not about any one source -- currently a token
    # approaching expiry, which would otherwise fail silently.
    warnings: list[str] = field(default_factory=list)

    @property
    def total_new(self) -> int:
        return sum(len(a.new_items) for a in self.accounts)

    @property
    def total_flagged(self) -> int:
        return sum(len(a.flagged) for a in self.accounts)

    @property
    def total_complaints(self) -> int:
        return sum(len(a.complaints) for a in self.accounts)

    @property
    def has_problems(self) -> bool:
        """Whether this run should be treated as unhealthy."""
        return bool(self.warnings) or any(
            a.fatal or a.failures for a in self.accounts)

    @property
    def has_unavailable_sources(self) -> bool:
        """Sources that could not be read, fault or not -- for the digest."""
        return any(a.fatal or a.problems for a in self.accounts)


def run(
    accounts: list[Account],
    state: State,
    *,
    sources: list[str] | None = None,
    api_version: str | None = None,
    record: bool = True,
    token_checker=None,
) -> Report:
    """Collect new items for every account.

    ``record`` exists so a preview run can show what would be reported
    without marking anything as seen -- otherwise the first dry run would
    silently swallow the backlog.
    """
    wanted = [k for k in KIND_ORDER if not sources or k in sources]
    report = Report()

    for account in accounts:
        account_report = AccountReport(account=account)
        report.accounts.append(account_report)

        token = account.token
        if not token:
            account_report.fatal = (
                f"no token -- set ${account.token_env} in the environment")
            continue

        client_kwargs = {"api_version": api_version} if api_version else {}
        page_client = GraphClient(token, **client_kwargs)

        # Ad accounts are not Page objects: ads_read is a user-level
        # permission, so a Page token gets "(#100) Unsupported get request"
        # against /act_<id>/ads. Ad comments therefore need a separate,
        # long-lived user token.
        ads_token = account.ads_token
        ads_client = (GraphClient(ads_token, **client_kwargs)
                      if ads_token else None)

        for kind in wanted:
            if not account.wants(kind):
                continue

            if kind == KIND_AD_COMMENT and ads_client is None:
                account_report.results.append(CollectionResult(
                    kind=kind, account=account.slug,
                    skipped_reason=(
                        "no ads token -- a Page token cannot read an ad "
                        f"account. Set ads_token_env for '{account.name}' to a "
                        "long-lived user token with ads_read")))
                continue

            collector = COLLECTORS[kind]
            client = ads_client if kind == KIND_AD_COMMENT else page_client
            try:
                # The ad collector lists ads with the user token but must
                # read comments with the Page token; everything else
                # ignores the extra argument.
                result = collector(client, account, page_client=page_client)
            except Exception as exc:  # a bug in one collector, not a reason to stop
                log.exception("collector %s failed for %s", kind, account.slug)
                result = CollectionResult(
                    kind=kind, account=account.slug,
                    error=f"unexpected error: {exc}")

            if result.ok and result.items:
                fresh = state.filter_new(account.slug, kind, result.items)
                result.items = triage.apply(fresh)
                if record:
                    state.record(account.slug, kind, [i.id for i in fresh])
            elif result.ok and record:
                state.mark_run(account.slug, kind)

            account_report.results.append(result)

    _check_tokens(report, accounts, state, api_version, record=record,
                  checker=token_checker or tokens.check)
    return report


def _check_tokens(report, accounts, state, api_version, *, record: bool,
                  checker) -> None:
    """Warn before the ads token dies, not after.

    Its expiry is silent: ad comments simply stop being reported and the
    channel stays quiet, which is what this tool uses to mean "nothing
    wrong". Checked once a day, since the answer moves in weeks.
    """
    if not state.due_for_token_check(tokens.CHECK_EVERY_HOURS):
        # Re-raise the last answer so the warning persists between checks.
        existing = state.token_warning()
        if existing:
            report.warnings.append(existing)
        return

    seen: set[str] = set()
    warnings: list[str] = []
    kwargs = {"api_version": api_version} if api_version else {}
    for account in accounts:
        ads_token = account.ads_token
        # One token usually serves every account; only inspect it once.
        if not ads_token or ads_token in seen:
            continue
        seen.add(ads_token)
        warning = checker(
            GraphClient(ads_token, **kwargs), ads_token,
            label=f"ads token ({account.ads_token_env})")
        if warning:
            warnings.append(warning)

    report.warnings.extend(warnings)
    if record:
        state.mark_token_checked("; ".join(warnings) if warnings else None)
