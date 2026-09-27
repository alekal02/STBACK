"""Compactadores da coleta. Entram no painel só quando a placa já existe na Sagtrack."""

from __future__ import annotations

# Prefixo operacional e placa. Argos ainda não tem prefixo CCL neste cadastro.
ROSTER: tuple[tuple[str, str], ...] = (
    ("CCL-002", "SDI-4H17"),
    ("CCL-003", "SDG-1J76"),
    ("CCL-005", "SDJ-5A17"),
    ("CCL-006", "SDJ-6C47"),
    ("CCL-007", "SDE-5I26"),
    ("CCL-008", "SDG-6D46"),
    ("CCL-009", "SDE-6B76"),
    ("CCL-010", "SDN-8J06"),
    ("CCL-012", "SDN-8F66"),
    ("CCL-013", "SDO-2J06"),
    ("CCL-014", "SDI-4I97"),
    ("CCL-015", "SDI-1H97"),
    ("CCL-016", "SDM-4D87"),
    ("CCL-017", "SDK-8H37"),
    ("CCL-018", "RBM-8I85"),
    ("CCL-020", "SDN-6J87"),
    ("", "RBK-7H47"),
    ("", "RBL-0C01"),
    ("", "RBM-2G21"),
)


def norm_placa(placa: str) -> str:
    return "".join(ch for ch in (placa or "").upper() if ch.isalnum())


def placas_da_frota() -> set[str]:
    return {norm_placa(placa) for _, placa in ROSTER if norm_placa(placa)}


def prefixo_da_placa(placa: str) -> str:
    chave = norm_placa(placa)
    for prefixo, cadastrada in ROSTER:
        if norm_placa(cadastrada) == chave:
            return prefixo
    return ""
