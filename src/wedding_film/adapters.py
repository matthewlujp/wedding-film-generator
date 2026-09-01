from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AdapterDefinition:
    name: str
    credential_variables: tuple[str, ...] = ()


ADAPTERS: dict[str, AdapterDefinition] = {
    definition.name: definition
    for definition in (
        AdapterDefinition("none"),
        AdapterDefinition("fake"),
        AdapterDefinition("openai", ("OPENAI_API_KEY",)),
        AdapterDefinition("deepseek", ("DEEPSEEK_API_KEY",)),
    )
}


def is_supported_adapter(name: str) -> bool:
    return name in ADAPTERS


def required_credentials(adapter_names: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            variable
            for name in adapter_names
            for variable in ADAPTERS[name].credential_variables
        )
    )
