"""Inference-provider credential management parser."""

from __future__ import annotations

from typing import Callable


def build_provider_parser(subparsers, *, cmd_provider: Callable) -> None:
    provider_parser = subparsers.add_parser(
        "provider",
        help="Manage inference-provider credentials",
    )
    provider_subparsers = provider_parser.add_subparsers(dest="provider_action")
    provider_add = provider_subparsers.add_parser(
        "add", help="Add a pooled credential"
    )
    provider_add.add_argument(
        "provider",
        help="Provider id (for example: anthropic, openai-codex, openrouter)",
    )
    provider_add.add_argument(
        "--type",
        dest="auth_type",
        choices=["oauth", "api-key", "api_key"],
        help="Credential type to add",
    )
    provider_add.add_argument("--label", help="Optional display label")
    provider_add.add_argument(
        "--api-key", help="API key value (otherwise prompted securely)"
    )
    provider_add.add_argument("--portal-url", help="Nous portal base URL")
    provider_add.add_argument("--inference-url", help="Nous inference base URL")
    provider_add.add_argument("--client-id", help="OAuth client id")
    provider_add.add_argument("--scope", help="OAuth scope override")
    provider_add.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not auto-open a browser for OAuth login",
    )
    provider_add.add_argument(
        "--timeout", type=float, help="OAuth/network timeout in seconds"
    )
    provider_add.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS verification for OAuth login",
    )
    provider_add.add_argument("--ca-bundle", help="Custom CA bundle for OAuth login")
    provider_list = provider_subparsers.add_parser(
        "list", help="List pooled credentials"
    )
    provider_list.add_argument("provider", nargs="?", help="Optional provider filter")
    provider_remove = provider_subparsers.add_parser(
        "remove", help="Remove a pooled credential by index, id, or label"
    )
    provider_remove.add_argument("provider", help="Provider id")
    provider_remove.add_argument(
        "target", help="Credential index, entry id, or exact label"
    )
    provider_reset = provider_subparsers.add_parser(
        "reset", help="Clear exhaustion status for all credentials for a provider"
    )
    provider_reset.add_argument("provider", help="Provider id")
    provider_status = provider_subparsers.add_parser(
        "status", help="Show auth status for a provider"
    )
    provider_status.add_argument("provider", help="Provider id")
    provider_logout = provider_subparsers.add_parser(
        "logout", help="Log out a provider and clear stored auth state"
    )
    provider_logout.add_argument("provider", help="Provider id")
    provider_spotify = provider_subparsers.add_parser(
        "spotify", help="Authenticate Hermes with Spotify via PKCE"
    )
    provider_spotify.add_argument(
        "spotify_action",
        nargs="?",
        choices=["login", "status", "logout"],
        default="login",
    )
    provider_spotify.add_argument(
        "--client-id", help="Spotify app client_id (or set HERMES_SPOTIFY_CLIENT_ID)"
    )
    provider_spotify.add_argument(
        "--redirect-uri",
        help="Allow-listed localhost redirect URI for your Spotify app",
    )
    provider_spotify.add_argument("--scope", help="Override requested Spotify scopes")
    provider_spotify.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not attempt to open the browser automatically",
    )
    provider_spotify.add_argument(
        "--timeout", type=float, help="Callback/token exchange timeout in seconds"
    )
    provider_parser.set_defaults(func=cmd_provider)
