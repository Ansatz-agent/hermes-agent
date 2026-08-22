"""Allow running the ACP adapter as ``python -m acp_adapter``."""

if __name__ == "__main__":
    from hermes_cli.client_auth.guard import enforce_direct_entrypoint

    enforce_direct_entrypoint("direct.acp_adapter")

from .entry import main

main()
