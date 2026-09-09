"""Account configuration.

Tokens are never stored in the config file. Each account names an
environment variable, and the token is read from the process environment at
run time, so ``accounts.yaml`` stays safe to commit if you ever want to.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Account:
    """One business, with whichever surfaces it actually has."""

    name: str
    slug: str
    # Page token: reads comments, posts, recommendations and DMs.
    token_env: str
    # A Page token cannot read an ad account -- ads_read is a user-level
    # permission, so reaching ad comments needs a long-lived USER token as
    # well. Falls back to token_env, which will fail loudly rather than
    # silently reporting no ad comments.
    ads_token_env: str | None = None
    facebook_page_id: str | None = None
    instagram_user_id: str | None = None
    # A business commonly runs ads from several accounts (an in-house one
    # plus an agency's). Comments land on the Page post either way, so all
    # of them have to be walked.
    ad_account_ids: list[str] = field(default_factory=list)
    # Per-account opt-out, for a business where (say) nobody reads the
    # Instagram DMs and the noise is not wanted.
    disabled_sources: list[str] = field(default_factory=list)

    @property
    def token(self) -> str | None:
        return os.environ.get(self.token_env) or None

    @property
    def ads_token(self) -> str | None:
        if not self.ads_token_env:
            return None
        return os.environ.get(self.ads_token_env) or None

    def wants(self, kind: str) -> bool:
        return kind not in self.disabled_sources


class ConfigError(RuntimeError):
    pass


def load_accounts(path: str | Path) -> list[Account]:
    path = Path(path)
    if not path.exists():
        raise ConfigError(
            f"no config at {path} -- copy accounts.example.yaml and fill it in")

    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc

    entries = raw.get("accounts")
    if not isinstance(entries, list) or not entries:
        raise ConfigError(f"{path} must define a non-empty 'accounts' list")

    accounts: list[Account] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ConfigError(f"account #{index + 1} in {path} is not a mapping")
        account = _build(entry, index, path)
        if account.slug in seen:
            raise ConfigError(f"duplicate account slug '{account.slug}' in {path}")
        seen.add(account.slug)
        accounts.append(account)
    return accounts


def _build(entry: dict, index: int, path: Path) -> Account:
    name = entry.get("name")
    if not name:
        raise ConfigError(f"account #{index + 1} in {path} has no 'name'")

    slug = entry.get("slug") or _slugify(name)
    token_env = entry.get("token_env")
    if not token_env:
        raise ConfigError(f"account '{name}' has no 'token_env'")

    page_id = _as_id(entry.get("facebook_page_id"))
    ig_id = _as_id(entry.get("instagram_user_id"))
    ad_ids = _ad_account_ids(entry, name)

    if not any([page_id, ig_id, ad_ids]):
        raise ConfigError(
            f"account '{name}' needs at least one of facebook_page_id, "
            "instagram_user_id or ad_account_ids")

    disabled = entry.get("disabled_sources") or []
    if not isinstance(disabled, list):
        raise ConfigError(f"account '{name}': disabled_sources must be a list")

    return Account(
        name=name,
        slug=slug,
        token_env=token_env,
        ads_token_env=entry.get("ads_token_env"),
        facebook_page_id=page_id,
        instagram_user_id=ig_id,
        ad_account_ids=ad_ids,
        disabled_sources=[str(d) for d in disabled],
    )


def _ad_account_ids(entry: dict, name: str) -> list[str]:
    """Accept either a single ad_account_id or a list of ad_account_ids."""
    raw = entry.get("ad_account_ids")
    if raw is None:
        raw = entry.get("ad_account_id")
    if raw is None or raw == "":
        return []
    values = raw if isinstance(raw, list) else [raw]
    out: list[str] = []
    for value in values:
        ad_id = _as_id(value)
        if not ad_id:
            continue
        # Graph wants ad accounts prefixed; accept either form in config.
        if not ad_id.startswith("act_"):
            ad_id = f"act_{ad_id}"
        if ad_id not in out:
            out.append(ad_id)
    if not out:
        raise ConfigError(f"account '{name}': ad_account_ids is empty")
    return out


def _as_id(value) -> str | None:
    """IDs are numeric but must stay strings -- YAML will happily turn a
    17-digit Page ID into an int and lose nothing, but comparing it to
    Graph's string IDs would then fail."""
    if value is None or value == "":
        return None
    return str(value).strip()


def _slugify(name: str) -> str:
    out = [c.lower() if c.isalnum() else "-" for c in name]
    return "-".join(filter(None, "".join(out).split("-")))
