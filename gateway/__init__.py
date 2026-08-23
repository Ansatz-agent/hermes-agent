"""
Hermes Gateway - Multi-platform messaging integration.

This module provides a unified gateway for connecting the Hermes agent
to various messaging platforms (Telegram, Discord, WhatsApp, Weixin, and more) with:
- Session management (persistent conversations with reset policies)
- Dynamic context injection (agent knows where messages come from)
- Delivery routing (cron job outputs to appropriate channels)
- Platform-specific toolsets (different capabilities per platform)
"""

from importlib import import_module

__all__ = [
    # Config
    "GatewayConfig",
    "PlatformConfig", 
    "HomeChannel",
    "load_gateway_config",
    # Session
    "SessionContext",
    "SessionStore",
    "SessionResetPolicy",
    "build_session_context_prompt",
    # Delivery
    "DeliveryRouter",
    "DeliveryTarget",
]

_EXPORTS = {
    "GatewayConfig": ("gateway.config", "GatewayConfig"),
    "PlatformConfig": ("gateway.config", "PlatformConfig"),
    "HomeChannel": ("gateway.config", "HomeChannel"),
    "load_gateway_config": ("gateway.config", "load_gateway_config"),
    "SessionContext": ("gateway.session", "SessionContext"),
    "SessionStore": ("gateway.session", "SessionStore"),
    "SessionResetPolicy": ("gateway.session", "SessionResetPolicy"),
    "build_session_context_prompt": (
        "gateway.session",
        "build_session_context_prompt",
    ),
    "DeliveryRouter": ("gateway.delivery", "DeliveryRouter"),
    "DeliveryTarget": ("gateway.delivery", "DeliveryTarget"),
}


def __getattr__(name: str):
    """Load public gateway APIs only after the caller crosses its auth boundary."""
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError as error:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from error

    value = getattr(import_module(module_name), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
