from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[2]

_HORA_RE = re.compile(r"^(\d{1,2}):(\d{2})(?::\d{2})?")


def normalizar_hora(valor: str | None, padrao: str = "00:00") -> str:
    texto = (valor or "").strip()
    if not texto:
        return padrao
    achado = _HORA_RE.match(texto)
    if not achado:
        return padrao
    hora, minuto = int(achado.group(1)), int(achado.group(2))
    if hora > 23 or minuto > 59:
        return padrao
    return f"{hora:02d}:{minuto:02d}"


def periodo_cruza_meia_noite(hora_inicio: str | None, hora_fim: str | None) -> bool:
    return normalizar_hora(hora_inicio, "00:00") > normalizar_hora(hora_fim, "23:59")


def parse_data(valor: str | date | None) -> date | None:
    if valor is None or str(valor).strip() == "":
        return None
    if isinstance(valor, date):
        return valor
    return date.fromisoformat(str(valor).strip())


def resolver_data_fim(
    dia: date,
    hora_inicio: str | None = "00:00",
    hora_fim: str | None = "23:59",
    data_fim: str | date | None = None,
) -> date:
    extra = parse_data(data_fim)
    if extra and extra >= dia:
        return extra
    if periodo_cruza_meia_noite(hora_inicio, hora_fim):
        return dia + timedelta(days=1)
    return dia


def janela_datetime(
    dia: date,
    hora_inicio: str | None = "00:00",
    hora_fim: str | None = "23:59",
    data_fim: str | date | None = None,
) -> tuple[datetime, datetime]:
    hi = normalizar_hora(hora_inicio, "00:00")
    hf = normalizar_hora(hora_fim, "23:59")
    h0, m0 = (int(p) for p in hi.split(":"))
    h1, m1 = (int(p) for p in hf.split(":"))
    inicio = datetime.combine(dia, time(h0, m0))
    fim_dia = resolver_data_fim(dia, hi, hf, data_fim)
    fim = datetime.combine(fim_dia, time(h1, m1, 59))
    if fim < inicio:
        fim = fim + timedelta(days=1)
    return inicio, fim


def dia_operacional(agora: datetime | None = None) -> date:
    """Turno da coleta: 06:00 de um dia até 05:59 do dia seguinte."""
    momento = agora or datetime.now()
    if momento.time() < time(6, 0):
        return (momento - timedelta(days=1)).date()
    return momento.date()


def rotulo_periodo(
    dia: date,
    hora_inicio: str | None = "00:00",
    hora_fim: str | None = "23:59",
    data_fim: str | date | None = None,
) -> str:
    hi = normalizar_hora(hora_inicio, "00:00")
    hf = normalizar_hora(hora_fim, "23:59")
    inicio, fim = janela_datetime(dia, hi, hf, data_fim)
    if inicio.date() == fim.date() and hi == "00:00" and hf == "23:59":
        return dia.strftime("%d/%m/%Y")
    if inicio.date() == fim.date():
        return f"{dia.strftime('%d/%m/%Y')} · {hi}–{hf}"
    return f"{inicio.strftime('%d/%m/%Y %H:%M')} → {fim.strftime('%d/%m/%Y %H:%M')}"


def aplicar_periodo(
    settings: Settings,
    data: str | date | None = None,
    hora_inicio: str | None = None,
    hora_fim: str | None = None,
    data_fim: str | date | None = None,
) -> Settings:
    updates: dict[str, object] = {}
    if data:
        updates["data_rota"] = data if isinstance(data, date) else date.fromisoformat(str(data))
    if hora_inicio:
        updates["hora_inicio"] = normalizar_hora(hora_inicio, settings.hora_inicio)
    if hora_fim:
        updates["hora_fim"] = normalizar_hora(hora_fim, settings.hora_fim)
    if data_fim:
        updates["data_fim"] = parse_data(data_fim) or settings.data_fim
    dia = updates.get("data_rota", settings.data_rota)
    hi = str(updates.get("hora_inicio", settings.hora_inicio))
    hf = str(updates.get("hora_fim", settings.hora_fim))
    if "data_fim" not in updates:
        updates["data_fim"] = resolver_data_fim(dia, hi, hf, data_fim)  # type: ignore[arg-type]
    if updates["data_fim"] < dia:  # type: ignore[operator]
        updates["data_fim"] = dia
    return Settings(**{**settings.__dict__, **updates})


def pasta_dados() -> Path:
    """Estado gravado (grupo do WhatsApp). No Render fica no disco persistente."""
    bruto = os.getenv("SAGTRACK_DATA_DIR", "").strip()
    pasta = Path(bruto) if bruto else ROOT / "data"
    pasta.mkdir(parents=True, exist_ok=True)
    return pasta


def _as_int(value: str | None, default: int) -> int:
    if value is None or value.strip() == "":
        return default
    return int(value)


@dataclass(frozen=True)
class Settings:
    data_rota: date
    data_fim: date
    hora_inicio: str
    hora_fim: str
    output_dir: Path
    porta: int
    veiculos: tuple[str, ...]
    wuzapi_url: str
    wuzapi_token: str
    wuzapi_admin_token: str
    webhook_url: str
    whatsapp_group_jid: str
    whatsapp_allowed_jids: tuple[str, ...]
    fulltrack_base_url: str
    fulltrack_username: str
    fulltrack_password: str
    fulltrack_client_id: str
    fulltrack_api_key: str
    fulltrack_secret_key: str


def load_settings() -> Settings:
    load_dotenv(ROOT / ".env", override=True)
    if not os.getenv("FULLTRACK_API_KEY") and (ROOT / "envsag").is_file():
        load_dotenv(ROOT / "envsag", override=False)

    data_raw = os.getenv("DATA_ROTA", "").strip()
    data_rota = date.fromisoformat(data_raw) if data_raw else date.today()
    hora_inicio = normalizar_hora(os.getenv("HORA_INICIO", ""), "06:00")
    hora_fim = normalizar_hora(os.getenv("HORA_FIM", ""), "05:59")
    data_fim = resolver_data_fim(
        data_rota, hora_inicio, hora_fim, os.getenv("DATA_FIM", "").strip() or None
    )
    veiculos = tuple(
        item.strip()
        for item in os.getenv("SAGTRACK_VEICULOS", "").split(",")
        if item.strip()
    )
    output_bruto = os.getenv("OUTPUT_DIR", "").strip()
    output_dir = Path(output_bruto) if output_bruto else ROOT / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    externo = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
    webhook = os.getenv("WEBHOOK_URL", "").strip()
    if not webhook and externo:
        webhook = externo + "/api/webhooks/whatsapp"
    if not webhook:
        webhook = "http://host.docker.internal:6480/api/webhooks/whatsapp"
    return Settings(
        data_rota=data_rota,
        data_fim=data_fim,
        hora_inicio=hora_inicio,
        hora_fim=hora_fim,
        output_dir=output_dir,
        porta=_as_int(os.getenv("PORT") or os.getenv("PORTA"), 6480),
        veiculos=veiculos,
        wuzapi_url=os.getenv("WUZAPI_URL", "http://127.0.0.1:8081").strip(),
        wuzapi_token=os.getenv("WUZAPI_TOKEN", "").strip(),
        wuzapi_admin_token=os.getenv("WUZAPI_ADMIN_TOKEN", "").strip(),
        webhook_url=webhook,
        whatsapp_group_jid=os.getenv("WHATSAPP_GROUP_JID", "").strip(),
        whatsapp_allowed_jids=tuple(
            item.strip()
            for item in os.getenv("WHATSAPP_ALLOWED_JIDS", "").split(",")
            if item.strip()
        ),
        fulltrack_base_url=os.getenv("FULLTRACK_BASE_URL", "https://web-service2.ftdata.com.br").strip(),
        fulltrack_username=os.getenv("FULLTRACK_USERNAME", "").strip(),
        fulltrack_password=os.getenv("FULLTRACK_PASSWORD", "").strip(),
        fulltrack_client_id=os.getenv("FULLTRACK_CLIENT_ID", "").strip(),
        fulltrack_api_key=os.getenv("FULLTRACK_API_KEY", "").strip(),
        fulltrack_secret_key=os.getenv("FULLTRACK_SECRET_KEY", "").strip(),
    )
