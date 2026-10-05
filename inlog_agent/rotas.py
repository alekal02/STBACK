from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from . import inlog_api as api
from .browser import open_session
from .cobertura import km_gps
from .config import Settings
from .export import export_rotas, write_json
from .inlog_historico import coletar_historico_api, coletar_historico_caminhao
from .login import fazer_login
from .mapa_oficial import aplicar_setores_oficiais, carregar_setores
from .relatorio_diario import exportar_relatorio_diario, montar_linhas_setores
from .ui import MENU_ALIASES, click_by_text, fill_filter

PREFIXOS_COMPACTADOR = ("CCL",)
TIPOS_COMPACTADOR = {5, 6, 8, 22}  # elétrico, compactador, mini, 15m³ toco


def _norm(texto: Any) -> str:
    return str(texto or "").strip().upper()


def _texto_frota(item: dict[str, Any]) -> str:
    return " ".join(
        _norm(item.get(chave))
        for chave in (
            "Identificador",
            "Prefixo",
            "Nome",
            "Descricao",
            "Placa",
            "CodigoIdentificador",
        )
    )


def _tem_nomenclatura_ccl(item: dict[str, Any]) -> bool:
    blob = _texto_frota(item).replace("\r", "").replace("\n", "")
    return any(pref in blob for pref in PREFIXOS_COMPACTADOR)


def _chave_veiculo(item: dict[str, Any]) -> str:
    codigo = item.get("CodigoVeiculo") or item.get("Codigo") or item.get("Id")
    if codigo not in (None, "", 0, "0"):
        return f"id:{codigo}"
    ident = _norm(item.get("Identificador") or item.get("Prefixo") or item.get("Placa"))
    return f"nome:{ident}" if ident else ""


def _juntar_frota(*listas: list[dict[str, Any]]) -> list[dict[str, Any]]:
    juntos: dict[str, dict[str, Any]] = {}
    for lista in listas:
        for item in lista or []:
            if not isinstance(item, dict):
                continue
            if not item.get("Identificador") and item.get("Prefixo"):
                item = {**item, "Identificador": item.get("Prefixo")}
            chave = _chave_veiculo(item)
            if not chave:
                continue
            atual = juntos.get(chave, {})
            atual.update({k: v for k, v in item.items() if v not in (None, "")})
            juntos[chave] = atual
    return list(juntos.values())


def _eh_domiciliar(item: dict[str, Any]) -> bool:
    blob = " ".join(
        [
            _norm(item.get("MacroSetor")),
            _norm(item.get("Setor")),
            _norm(item.get("DescricaoOperacao")),
        ]
    )
    grupos = item.get("GrupoControle") or item.get("OperacoesVinculadasVeiculo") or []
    for grupo in grupos:
        if isinstance(grupo, dict):
            blob += " " + _norm(grupo.get("DescricaoGrupoControle") or grupo.get("DescricaoOperacao"))
    return "DOMICILIAR" in blob


def _eh_compactador(item: dict[str, Any]) -> bool:
    return _tem_nomenclatura_ccl(item)


def _ident_veiculo(item: dict[str, Any]) -> str:
    return _norm(item.get("Identificador") or item.get("Prefixo") or "").replace("\r", "").replace("\n", "")


def listar_compactadores(page, settings: Settings) -> list[dict[str, Any]]:
    indice = api.index_sinotico(page)
    grid = ((indice.get("Data") or {}) if isinstance(indice, dict) else {}).get("VeiculosGrid") or []
    cadastro: list[dict[str, Any]] = []
    try:
        cadastro = api.carregar_todos_veiculos(page)
    except Exception:
        cadastro = []
    frota = _juntar_frota(grid if isinstance(grid, list) else [], cadastro)
    compactadores = [item for item in frota if _eh_compactador(item)]
    filtro = settings.filtro_veiculos or ("CCL",)
    compactadores = [
        item
        for item in compactadores
        if any(pref.lower() in _texto_frota(item).lower() for pref in filtro)
    ]
    compactadores.sort(key=lambda item: _ident_veiculo(item) or _norm(item.get("Placa") or ""))
    return compactadores


def _abrir_sinotico(page, settings: Settings, progress: Callable[[str], None]) -> None:
    progress("Login ok. Abrindo Sinótico de Veículos...")
    try:
        page.goto(settings.base_url, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        click_by_text(page, MENU_ALIASES)
    except Exception:
        pass
    page.goto(f"{api.RAST}/", wait_until="domcontentloaded")
    page.wait_for_timeout(2500)
    termo = (settings.filtro_veiculos[0] if settings.filtro_veiculos else "CCL").strip()
    fill_filter(page, termo)


def _salvar_so_gps(settings: Settings, rotas: list[dict[str, Any]], progress: Callable[[str], None]) -> dict:
    data_iso = settings.data_rota.isoformat()
    for item in rotas:
        pontos = item.get("pontos") or []
        item["km_dia"] = round(km_gps(pontos), 3)
        item["total_pontos"] = len(pontos)
    progress("GPS Inlog salvo. O cruzamento com o mapa oficial fica no relatório único.")
    arquivos = export_rotas(settings.output_dir, data_iso, rotas, leve=True)
    relatorio = {
        "quando": datetime.now().isoformat(timespec="seconds"),
        "data": data_iso,
        "fonte": "inlog-coleta-api",
        "filtro_servico": "coleta domiciliar / compactadores CCL",
        "caminho": "IntegrationColeta /api/Historico",
        "total_rotas_domiciliar": 0,
        "total_rotas_do_dia": 0,
        "total_compactadores": len(rotas),
        "total_veiculos": len(rotas),
        "total_pontos": sum(item.get("total_pontos") or 0 for item in rotas),
        "percentual_medio": 0,
        "arquivos": arquivos,
    }
    write_json(settings.output_dir / "rotas" / data_iso / "relatorio.json", relatorio)
    write_json(
        settings.output_dir / "rotas" / data_iso / "frota.json",
        [
            {
                "Identificador": item.get("veiculo"),
                "Placa": item.get("placa"),
                "Descricao": item.get("descricao"),
            }
            for item in rotas
        ],
    )
    progress(
        f"Pronto. {relatorio['total_veiculos']} veículos, "
        f"{relatorio['total_pontos']} pontos pela API de Aparecida."
    )
    return relatorio


def _tentar_api_coleta(settings: Settings, progress: Callable[[str], None]) -> dict | None:
    if not settings.inlog_coleta_client_id or not settings.inlog_coleta_client_secret:
        return None
    try:
        from .inlog_coleta_api import coletar_percursos

        progress("Inlog: buscando GPS na IntegrationColeta de Aparecida (sem abrir o site)...")
        rotas = coletar_percursos(settings, on_progress=progress)
    except Exception as exc:
        progress(f"Inlog Coleta API falhou ({exc}).")
        return None
    total = sum(int(item.get("total_pontos") or 0) for item in rotas)
    if total < 80:
        progress(f"Inlog Coleta API com pouco GPS ({total} pontos).")
        return None
    return _salvar_so_gps(settings, rotas, progress)


def executar_rotas(
    settings: Settings,
    headed: bool | None = None,
    on_progress: Callable[[str], None] | None = None,
    gerar_pdf: bool = True,
    so_gps: bool = False,
) -> dict:
    data_iso = settings.data_rota.isoformat()

    def progress(msg: str) -> None:
        print(msg, flush=True)
        if on_progress:
            on_progress(msg)

    if so_gps:
        pela_api = _tentar_api_coleta(settings, progress)
        if pela_api:
            return pela_api
        progress("API Coleta sem GPS suficiente. Abrindo o portal Inlog...")

    with open_session(settings, headed=headed, perfil="inlog") as session:
        page = session.page
        assert page is not None
        progress(f"Abrindo o Inlog para {data_iso}...")
        fazer_login(page, settings)
        _abrir_sinotico(page, settings, progress)
        if not so_gps:
            session.screenshot("rotas-sinotico")

        compactadores = listar_compactadores(page, settings)
        nomes = [_ident_veiculo(item) for item in compactadores]
        progress(
            f"Frota no sinótico: {len(compactadores)} compactadores"
            + (f" ({', '.join(n for n in nomes if n)})." if nomes else ".")
        )

        relatorio_rotas: list[dict[str, Any]] = []
        lote_api: dict[int, Any] = {}
        if so_gps:
            progress("Buscando o histórico GPS da frota pela API do Inlog (sem abrir caminhão por caminhão)...")
            lote_api = api.buscar_historicos_lote(
                page,
                compactadores,
                settings.data_rota,
                hora_inicio=settings.hora_inicio,
                hora_fim=settings.hora_fim,
                data_fim=settings.data_fim,
            )
        for i, veiculo in enumerate(compactadores, start=1):
            codigo = int(veiculo.get("CodigoVeiculo") or veiculo.get("Codigo") or 0)
            ident = _ident_veiculo(veiculo) or f"veiculo-{codigo}"
            if so_gps:
                executada = coletar_historico_api(
                    page,
                    veiculo,
                    settings.data_rota,
                    hora_inicio=settings.hora_inicio,
                    hora_fim=settings.hora_fim,
                    data_fim=settings.data_fim,
                    payload=lote_api.get(codigo),
                )
                progress(f"  {ident}: {len(executada)} pontos GPS")
            else:
                progress(f"Histórico {i}/{len(compactadores)}: abrindo {ident} no sinótico...")
                executada = coletar_historico_caminhao(
                    page,
                    veiculo,
                    settings.data_rota,
                    spy=session.spy,
                    hora_inicio=settings.hora_inicio,
                    hora_fim=settings.hora_fim,
                    data_fim=settings.data_fim,
                )
                progress(f"  {ident}: {len(executada)} pontos no trajeto")
                if ident == "CCL-014":
                    session.screenshot("historico-ccl-014")
            relatorio_rotas.append(
                {
                    "veiculo": ident,
                    "placa": str(veiculo.get("Placa") or "").strip(),
                    "descricao": str(veiculo.get("Descricao") or ident).strip(),
                    "codigo_veiculo": codigo,
                    "ignicao": veiculo.get("StatusIgnicao"),
                    "conectado": veiculo.get("VeiculoConectado"),
                    "ultima_posicao": veiculo.get("DataHora"),
                    "setor": "APARECIDA DE GOIANIA - DOMICILIAR - INLOG",
                    "rota": "Percurso do dia",
                    "macro": veiculo.get("MacroSetor") or "",
                    "codigo_rota": None,
                    "percentual_concluido": 0,
                    "veredicto": "SEM RASTREIO" if not executada else "NÃO FEITA",
                    "confianca": "baixa",
                    "eficiencia_planejada": None,
                    "km_coleta": None,
                    "km_total": None,
                    "km_faltando": None,
                    "km_dia": round(km_gps(executada), 3),
                    "qtd_pacmans": None,
                    "total_pontos": len(executada),
                    "pontos": executada,
                    "rota_planejada": [],
                    "trechos_feitos": [],
                    "trechos_faltando": [],
                    "trechos_planejados": [],
                    "trechos_gps": [],
                    "poligonos": [],
                }
            )

        if so_gps:
            progress("GPS Inlog salvo. O cruzamento com o mapa oficial fica no relatório único.")
            arquivos = export_rotas(settings.output_dir, data_iso, relatorio_rotas, leve=True)
            linhas_dia = []
        else:
            progress("Casando o GPS com os setores oficiais do mapa de coleta...")
            oficiais = carregar_setores(settings.data_rota)
            if oficiais:
                aplicar_setores_oficiais(
                    relatorio_rotas,
                    oficiais,
                    settings.data_rota,
                    usar_planejado_inlog=False,
                )
            else:
                progress("GPKG de setores não encontrado. Relatório segue só com o GPS do histórico.")
            progress("Montando relatório e mapa...")
            arquivos = export_rotas(settings.output_dir, data_iso, relatorio_rotas)
            linhas_dia = montar_linhas_setores(settings.data_rota, relatorio_rotas)
            arquivos.update(exportar_relatorio_diario(
                settings.output_dir / "rotas" / data_iso,
                settings.data_rota,
                linhas_dia,
                rotas=relatorio_rotas,
                hora_inicio=settings.hora_inicio,
                hora_fim=settings.hora_fim,
                data_fim=settings.data_fim,
            ))
        if gerar_pdf and not so_gps:
            try:
                from .pdf import gerar_pdf_dia

                progress("Gerando PDF com rotas e mapa...")
                arquivos["pdf"] = str(gerar_pdf_dia(settings.output_dir / "rotas" / data_iso))
            except Exception as exc:
                progress(f"PDF não gerado: {exc}")
        relatorio = {
            "quando": datetime.now().isoformat(timespec="seconds"),
            "data": data_iso,
            "url": page.url,
            "filtro_servico": "coleta domiciliar / compactadores CCL",
            "caminho": "login → sinótico → caminhão → detalhes → histórico → aplicar",
            "total_rotas_domiciliar": 0,
            "total_rotas_do_dia": len(linhas_dia),
            "relatorio_diario": arquivos.get("diario_html"),
            "total_compactadores": len(compactadores),
            "total_veiculos": len(relatorio_rotas),
            "total_pontos": sum(item["total_pontos"] for item in relatorio_rotas),
            "percentual_medio": round(
                sum(float(item.get("percentual_concluido") or 0) for item in relatorio_rotas)
                / len(relatorio_rotas),
                1,
            )
            if relatorio_rotas
            else 0,
            "arquivos": arquivos,
        }
        write_json(settings.output_dir / "rotas" / data_iso / "relatorio.json", relatorio)
        write_json(settings.output_dir / "rotas" / data_iso / "frota.json", compactadores)
        progress(
            f"Pronto. {relatorio['total_veiculos']} veículos, "
            f"{relatorio['total_pontos']} pontos, {relatorio['percentual_medio']}% médio."
        )
        return relatorio
