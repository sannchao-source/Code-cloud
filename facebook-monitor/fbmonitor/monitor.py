"""Run collectors across accounts and assemble a report of what is new."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .collectors import COLLECTORS
from .config import Account
from .graph import GraphClient
from .models import KIND_AD_COMMENT, KIND_ORDER, CollectionResult, Item
from .state import State
from . import triage

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
        return [r for r in self.results if r.error or r.skipped_reason]


@dataclass
class Report:
    accounts: list[AccountReport] = field(default_factory=list)

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
        return any(a.fatal or a.problems for a in self.accounts)


def run(
    accounts: list[Account],
    state: State,
    *,
    sources: list[str] | None = None,
    api_version: str | None = None,
    record: bool = True,
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
                result = collector(client, account)
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

    return report
