from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def rotulo_pacote() -> str:
    """Nome do ZIP/pasta levado para o outro PC, se existir."""
    for nome in ("pacote-nome.txt", "versao.txt"):
        arq = ROOT / nome
        if not arq.is_file():
            continue
        try:
            linhas = arq.read_text(encoding="utf-8", errors="replace").strip().splitlines()
        except OSError:
            continue
        if linhas and linhas[0].strip():
            return linhas[0].strip()[:80]
    return ""

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


def hora_com_segundos(valor: str | None, *, fim: bool = False) -> str:
    padrao = "23:59" if fim else "00:00"
    normal = normalizar_hora(valor, padrao)
    if fim and normal == "23:59":
        return "23:59:59"
    return f"{normal}:00"


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


def limitar_janela_api(
    inicio: datetime,
    fim: datetime,
    folga_min: int = 6,
) -> tuple[datetime, datetime]:
    """APIs recusam fim no futuro (Inlog: menos de 5 min atrás). Não muda o ciclo do relatório."""
    teto = datetime.now() - timedelta(minutes=max(1, folga_min))
    if fim > teto:
        fim = teto
    if fim < inicio:
        fim = inicio
    return inicio, fim


def dias_do_ciclo(
    dia: date,
    hora_inicio: str | None = "00:00",
    hora_fim: str | None = "23:59",
    data_fim: str | date | None = None,
) -> list[date]:
    inicio, fim = janela_datetime(dia, hora_inicio, hora_fim, data_fim)
    atual = inicio.date()
    saida: list[date] = []
    while atual <= fim.date():
        saida.append(atual)
        atual += timedelta(days=1)
    return saida


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


def rotulo_dia_periodo(
    dia: date,
    hora_inicio: str | None = "00:00",
    hora_fim: str | None = "23:59",
    data_fim: str | date | None = None,
) -> str:
    dias = ["segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo"]
    return f"{rotulo_periodo(dia, hora_inicio, hora_fim, data_fim)} ({dias[dia.weekday()]})"


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
    hi = updates.get("hora_inicio", settings.hora_inicio)
    hf = updates.get("hora_fim", settings.hora_fim)
    if "data_fim" not in updates:
        updates["data_fim"] = resolver_data_fim(dia, str(hi), str(hf), data_fim)
    if updates["data_fim"] < dia:  # type: ignore[operator]
        updates["data_fim"] = dia
    return type(settings)(**{**settings.__dict__, **updates})


def _as_bool(value: str | None, default: bool = True) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "sim", "on"}


def _as_int(value: str | None, default: int) -> int:
    if value is None or value.strip() == "":
        return default
    return int(value)


@dataclass(frozen=True)
class Settings:
    username: str
    password: str
    login_url: str
    base_url: str
    filtro_servico: str
    filtro_veiculos: tuple[str, ...]
    data_rota: date
    data_fim: date
    hora_inicio: str
    hora_fim: str
    headless: bool
    timeout_ms: int
    slow_mo_ms: int
    output_dir: Path
    session_dir: Path
    porta: int
    argos_user: str
    argos_password: str
    argos_login_url: str
    argos_base_url: str
    argos_veiculos: tuple[str, ...]
    segtrack_user: str
    segtrack_password: str
    segtrack_login_url: str
    segtrack_home_url: str
    segtrack_mapa_url: str
    segtrack_veiculos: tuple[str, ...]
    fulltrack_base_url: str
    fulltrack_username: str
    fulltrack_password: str
    fulltrack_client_id: str
    fulltrack_api_key: str
    fulltrack_secret_key: str
    inlog_coleta_token_url: str
    inlog_coleta_base_url: str
    inlog_coleta_client_id: str
    inlog_coleta_client_secret: str
    inlog_coleta_grant_type: str
    inlog_coleta_scope: str

    @property
    def origem(self) -> str:
        return "https://consorcioaparecida.inlog.com.br"


def load_settings() -> Settings:
    porta_forcada = os.getenv("PORTA", "").strip()
    load_dotenv(ROOT / ".env", override=True)
    if porta_forcada:
        os.environ["PORTA"] = porta_forcada

    username = os.getenv("INLOG_USER", "").strip()
    password = os.getenv("INLOG_PASSWORD", "").strip()
    if not username or not password:
        print(
            "Aviso: preencha INLOG_USER e INLOG_PASSWORD no .env "
            "para o robô conseguir entrar no portal.",
            flush=True,
        )

    data_raw = os.getenv("DATA_ROTA", "").strip()
    data_rota = date.fromisoformat(data_raw) if data_raw else date.today()
    hora_inicio = normalizar_hora(os.getenv("HORA_INICIO", ""), "00:00")
    hora_fim = normalizar_hora(os.getenv("HORA_FIM", ""), "23:59")
    data_fim = resolver_data_fim(
        data_rota, hora_inicio, hora_fim, os.getenv("DATA_FIM", "").strip() or None
    )

    veiculos = tuple(
        item.strip()
        for item in os.getenv("FILTRO_VEICULOS", "").split(",")
        if item.strip()
    )

    output_env = os.getenv("OUTPUT_DIR", "").strip()
    if output_env:
        output_dir = Path(output_env)
        session_dir = output_dir.parent / "sessions"
    else:
        output_dir = ROOT / "output"
        session_dir = ROOT / "sessions"
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        session_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"Aviso: não consegui criar a pasta de coletas ({exc}).", flush=True)

    return Settings(
        username=username,
        password=password,
        login_url=os.getenv(
            "INLOG_LOGIN_URL",
            "https://consorcioaparecida.inlog.com.br/Rastreamento/"
            "Apresentacao/Autenticador/Account/Login"
            "?returnUrl=%2FRastreamento%2FApresentacao%2FAutenticador%2F",
        ).strip(),
        base_url=os.getenv(
            "INLOG_BASE_URL",
            "https://consorcioaparecida.inlog.com.br/Rastreamento/Apresentacao/",
        ).strip(),
        filtro_servico=os.getenv("FILTRO_SERVICO", "coleta domiciliar").strip(),
        filtro_veiculos=veiculos,
        data_rota=data_rota,
        data_fim=data_fim,
        hora_inicio=hora_inicio,
        hora_fim=hora_fim,
        headless=_as_bool(os.getenv("HEADLESS"), default=True),
        timeout_ms=_as_int(os.getenv("TIMEOUT_MS"), 45_000),
        slow_mo_ms=_as_int(os.getenv("SLOW_MO_MS"), 0),
        output_dir=output_dir,
        session_dir=session_dir,
        porta=_as_int(os.getenv("PORT") or os.getenv("PORTA"), 5889),
        argos_user=os.getenv("ARGOS_USER", "").strip(),
        argos_password=os.getenv("ARGOS_PASSWORD", "").strip(),
        argos_login_url=os.getenv(
            "ARGOS_LOGIN_URL",
            "https://sso.argosmonitoramento.com.br/",
        ).strip(),
        argos_base_url=os.getenv(
            "ARGOS_BASE_URL",
            "https://app.argosmonitoramento.com.br/",
        ).strip(),
        argos_veiculos=tuple(
            item.strip()
            for item in os.getenv(
                "ARGOS_VEICULOS",
                "RBK7H47,RBM2G21,RBL0C01",
            ).split(",")
            if item.strip()
        ),
        segtrack_user=os.getenv("SEGTRACK_USER", "").strip(),
        segtrack_password=os.getenv("SEGTRACK_PASSWORD", "").strip(),
        segtrack_login_url=os.getenv(
            "SEGTRACK_LOGIN_URL",
            "https://segtrack-seguranca-eletronica-e-servicos-inteligentes.fulltrackapp.com/",
        ).strip(),
        segtrack_home_url=os.getenv(
            "SEGTRACK_HOME_URL",
            "https://segtrack-seguranca-eletronica-e-servicos-inteligentes.fulltrackapp.com/dashboard_controller",
        ).strip(),
        segtrack_mapa_url=os.getenv(
            "SEGTRACK_MAPA_URL",
            "https://segtrack-seguranca-eletronica-e-servicos-inteligentes.fulltrackapp.com/mapaGeral_v3/#/",
        ).strip(),
        segtrack_veiculos=tuple(
            item.strip()
            for item in os.getenv("SEGTRACK_VEICULOS", "").split(",")
            if item.strip()
        ),
        fulltrack_base_url=os.getenv("FULLTRACK_BASE_URL", "https://web-service2.ftdata.com.br").strip(),
        fulltrack_username=os.getenv("FULLTRACK_USERNAME", "").strip(),
        fulltrack_password=os.getenv("FULLTRACK_PASSWORD", "").strip(),
        fulltrack_client_id=os.getenv("FULLTRACK_CLIENT_ID", "").strip(),
        fulltrack_api_key=os.getenv("FULLTRACK_API_KEY", "").strip(),
        fulltrack_secret_key=os.getenv("FULLTRACK_SECRET_KEY", "").strip(),
        inlog_coleta_token_url=os.getenv(
            "INLOG_COLETA_TOKEN_URL",
            "https://consorcioaparecida.inlog.com.br/autenticacao-services/connect/token",
        ).strip(),
        inlog_coleta_base_url=os.getenv(
            "INLOG_COLETA_BASE_URL",
            "https://consorcioaparecida.inlog.com.br/IntegrationColeta",
        ).strip(),
        inlog_coleta_client_id=os.getenv("INLOG_COLETA_CLIENT_ID", "").strip(),
        inlog_coleta_client_secret=os.getenv("INLOG_COLETA_CLIENT_SECRET", "").strip(),
        inlog_coleta_grant_type=os.getenv("INLOG_COLETA_GRANT_TYPE", "client_credentials").strip(),
        inlog_coleta_scope=os.getenv("INLOG_COLETA_SCOPE", "Inlog.Integration.Coleta.Api").strip(),
    )
