from __future__ import annotations

import json
import threading
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from .api import coletar_percursos
from .config import Settings, janela_datetime, rotulo_periodo
from .gps import _datetime_do_timestamp, haversine_m, tracos_na_ordem
from .mapa import aplicar_setores_oficiais, carregar_setores
from .relatorios import _aplicar_apoio, medir_presenca, pontos_leves, setores_leves
from .verificacao import coberturas_executadas

Progresso = Callable[[str], None]


def _km(pontos: list[dict[str, Any]]) -> float:
    total = 0.0
    anterior = None
    for ponto in pontos:
        try:
            atual = (float(ponto["latitude"]), float(ponto["longitude"]))
        except (KeyError, TypeError, ValueError):
            continue
        if anterior is not None:
            dist = haversine_m(anterior, atual)
            if 0 < dist < 2500:
                total += dist
        anterior = atual
    return round(total / 1000.0, 3)


def _thin(pts: list, max_pts: int = 180) -> list:
    if not pts or len(pts) <= max_pts:
        return pts
    passo = max(1, len(pts) // max_pts)
    saida = list(pts[::passo])
    if saida[-1] != pts[-1]:
        saida.append(pts[-1])
    return saida


def _horas(pontos: list[dict[str, Any]]) -> tuple[str, str]:
    marcas = [str(p.get("timestamp") or "") for p in pontos if p.get("timestamp")]
    if not marcas:
        return "", ""
    marcas.sort()
    return marcas[0], marcas[-1]


def folha_caminhao(item: dict[str, Any]) -> dict[str, Any]:
    visiveis = coberturas_executadas(item, max_n=3)
    if not visiveis and (item.get("trechos_feitos") or item.get("trechos_faltando")):
        visiveis = [
            {
                "codigo_rota": item.get("codigo_rota") or "",
                "setor": item.get("setor") or "",
                "rota": item.get("rota") or "",
                "percentual": item.get("percentual_concluido") or 0,
                "execucao": item.get("veredicto") or "",
                "veredicto": item.get("veredicto") or "",
                "km_coleta": item.get("km_coleta"),
                "km_total": item.get("km_total"),
                "km_faltando": item.get("km_faltando"),
                "confianca": item.get("confianca") or "",
                "trechos_feitos": item.get("trechos_feitos") or [],
                "trechos_faltando": item.get("trechos_faltando") or [],
                "poligonos": item.get("poligonos") or [],
            }
        ]
    feitos: list = []
    faltando: list = []
    poligonos: list = []
    setores = []
    for cob in visiveis:
        feitos.extend(_thin(t, 160) for t in (cob.get("trechos_feitos") or []) if t and len(t) >= 2)
        faltando.extend(_thin(t, 160) for t in (cob.get("trechos_faltando") or []) if t and len(t) >= 2)
        for anel in (cob.get("poligonos") or [])[:1]:
            if anel and len(anel) >= 4:
                poligonos.append(_thin(anel, 120))
        setores.append(
            {
                "codigo": cob.get("codigo_rota") or "",
                "setor": cob.get("setor") or "",
                "frequencia": cob.get("rota") or "",
                "percentual": cob.get("percentual") or 0,
                "veredicto": cob.get("execucao") or cob.get("veredicto") or "",
                "km_coleta": cob.get("km_coleta"),
                "km_total": cob.get("km_total"),
                "km_faltando": cob.get("km_faltando"),
                "confianca": cob.get("confianca") or "",
            }
        )
    pontos = item.get("pontos") or []
    inicio, fim = _horas(pontos)
    return {
        "placa": item.get("placa") or "",
        "prefixo": item.get("prefixo") or "",
        "veiculo": item.get("veiculo") or "",
        "papel": item.get("papel") or "",
        "paradas": item.get("paradas") or 0,
        "minutos_parado": item.get("minutos_parado") or 0,
        "saida": item.get("saida"),
        "paradas_pontos": item.get("paradas_pontos") or [],
        "percentual": item.get("percentual_concluido") or 0,
        "veredicto": item.get("veredicto") or ("SEM RASTREIO" if not pontos else "SEM COLETA"),
        "confianca": item.get("confianca") or "",
        "setor": item.get("setor") or "",
        "frequencia": item.get("rota") or "",
        "km_coleta": item.get("km_coleta"),
        "km_total": item.get("km_total"),
        "km_faltando": item.get("km_faltando"),
        "km_dia": item.get("km_dia"),
        "pontos": item.get("total_pontos") or len(pontos),
        "pontos_na_cidade": item.get("pontos_na_cidade") or 0,
        "inicio": inicio,
        "fim": fim,
        "setores": setores,
        "feitos": feitos,
        "faltando": faltando,
        "gps": tracos_na_ordem(pontos),
        "poligonos": poligonos[:3],
    }


def _media(folhas: list[dict[str, Any]]) -> float | None:
    nums = [float(f["percentual"]) for f in folhas if f.get("setores")]
    if not nums:
        return None
    return round(sum(nums) / len(nums), 1)


def montar_resumo(dia: date, folhas: list[dict[str, Any]], settings: Settings) -> dict[str, Any]:
    com_rota = [f for f in folhas if f.get("setores")]
    return {
        "data": dia.isoformat(),
        "periodo": rotulo_periodo(dia, settings.hora_inicio, settings.hora_fim, settings.data_fim),
        "quando": datetime.now().isoformat(timespec="seconds"),
        "frota": len(folhas),
        "com_rota": len(com_rota),
        "media": _media(folhas),
        "feitas": sum(1 for f in com_rota if str(f.get("veredicto") or "").upper() == "FEITA"),
        "parciais": sum(1 for f in com_rota if str(f.get("veredicto") or "").upper() == "PARCIAL"),
        "caminhoes": sorted(folhas, key=lambda f: (-float(f.get("percentual") or 0), str(f.get("placa") or ""))),
    }


def _sem_pontos(item: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in item.items() if k != "pontos"}


_CAMPOS_CONTA = (
    "coberturas_inlog",
    "percentual_concluido",
    "veredicto",
    "confianca",
    "setor",
    "rota",
    "codigo_rota",
    "km_coleta",
    "km_total",
    "km_faltando",
    "qtd_pacmans",
    "trechos_feitos",
    "trechos_faltando",
    "trechos_planejados",
    "trechos_gps",
    "poligonos",
    "vias",
    "rota_planejada",
    "paradas_dia",
    "saida",
    "papel",
    "paradas",
    "minutos_parado",
    "paradas_pontos",
)


def _contas_salvas(pasta: Path) -> dict[str, dict[str, Any]]:
    arquivo = pasta / "frota.json"
    if not arquivo.is_file():
        return {}
    try:
        bruto = json.loads(arquivo.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(bruto, list):
        return {}
    return {
        str(item.get("placa") or ""): item
        for item in bruto
        if isinstance(item, dict) and item.get("placa") and item.get("coberturas_inlog")
    }


def _reaproveitar_conta(item: dict[str, Any], anterior: dict[str, Any] | None) -> bool:
    """Mantém a rua já cruzada quando o caminhão não teve pulso novo."""
    if not anterior or not anterior.get("coberturas_inlog"):
        return False
    for campo in _CAMPOS_CONTA:
        if campo in anterior:
            item[campo] = anterior[campo]
    return True


def executar(settings: Settings, on_progress: Progresso | None = None) -> dict[str, Any]:
    def progress(msg: str) -> None:
        print(msg, flush=True)
        if on_progress:
            on_progress(msg)

    dia = settings.data_rota
    pasta = settings.output_dir / dia.isoformat()
    pasta.mkdir(parents=True, exist_ok=True)

    inicio, fim = janela_datetime(dia, settings.hora_inicio, settings.hora_fim, settings.data_fim)
    agora = datetime.now(timezone(timedelta(hours=-3))).replace(tzinfo=None, microsecond=0)
    if agora < inicio:
        congelado = inicio
    elif agora > fim:
        congelado = fim
    else:
        congelado = agora
    progress(
        f"Horário congelado às {congelado.strftime('%H:%M:%S')}. "
        "A coleta usa os pulsos até esse instante."
    )
    anteriores: dict[str, list] = {}
    arquivo_pontos = pasta / "pontos.json"
    if arquivo_pontos.is_file():
        try:
            bruto = json.loads(arquivo_pontos.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            bruto = {}
        if isinstance(bruto, dict):
            anteriores = {str(placa): pontos for placa, pontos in bruto.items() if isinstance(pontos, list)}
    rotas = coletar_percursos(settings, dia, on_progress=progress, ate=congelado, anteriores=anteriores)
    if (
        (pasta / "resumo.json").is_file()
        and rotas
        and all(str(item.get("origem_pulso") or "") == "salvo" for item in rotas)
    ):
        (pasta / "verificado.txt").write_text(congelado.isoformat(timespec="seconds"), encoding="utf-8")
        progress("Nenhum pulso novo. A conclusão já salva continua valendo.")
        return json.loads((pasta / "resumo.json").read_text(encoding="utf-8"))
    for item in rotas:
        item["km_dia"] = _km(item.get("pontos") or [])

    contas = _contas_salvas(pasta)
    refazer = []
    for item in rotas:
        placa = str(item.get("placa") or "")
        if str(item.get("origem_pulso") or "") == "salvo" and _reaproveitar_conta(item, contas.get(placa)):
            continue
        refazer.append(item)

    progress("Lendo o mapa oficial de Aparecida...")
    setores = carregar_setores(dia)
    if refazer:
        progress(
            f"Setores do dia: {len(setores)}. "
            f"Cruzando ruas de {len(refazer)} caminhão(ões) com pulso novo."
        )
        aplicar_setores_oficiais(refazer, setores, dia, on_progress=progress)
        progress("Medindo paradas de coleta...")
        medir_presenca(refazer, setores, dia)
    _aplicar_apoio(rotas)

    folhas = [folha_caminhao(item) for item in rotas]
    resumo = montar_resumo(dia, folhas, settings)
    resumo["congelado_em"] = congelado.isoformat(timespec="seconds")
    (pasta / "resumo.json").write_text(json.dumps(resumo, ensure_ascii=False), encoding="utf-8")
    (pasta / "setores.json").write_text(json.dumps(setores_leves(setores), ensure_ascii=False), encoding="utf-8")
    (pasta / "pontos.json").write_text(json.dumps(pontos_leves(rotas), ensure_ascii=False), encoding="utf-8")
    (pasta / "frota.json").write_text(
        json.dumps([_sem_pontos(item) for item in rotas], ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    progress(
        f"Pronto. {resumo['com_rota']} caminhão(ões) com rota em Aparecida, "
        f"média {resumo['media'] if resumo['media'] is not None else 0}%."
    )
    return resumo


def carregar_resumo(pasta_output: Path, dia: date) -> dict[str, Any] | None:
    arquivo = pasta_output / dia.isoformat() / "resumo.json"
    if not arquivo.is_file():
        return None
    return json.loads(arquivo.read_text(encoding="utf-8"))


_AO_VIVO = threading.Lock()


def _dentro_dos_aneis(aneis: list, lat: float, lon: float) -> bool:
    from shapely.geometry import Point, Polygon

    ponto = Point(lon, lat)
    for anel in aneis or []:
        coords = [(float(p[1]), float(p[0])) for p in anel if len(p) >= 2]
        if len(coords) < 4:
            continue
        if coords[0] != coords[-1]:
            coords.append(coords[0])
        poligono = Polygon(coords)
        if not poligono.is_valid:
            poligono = poligono.buffer(0)
        if poligono.covers(ponto):
            return True
    return False


def atualizar_rota_em_curso(output_dir: Path, dia: date, consulta: str) -> str | None:
    """Se um caminhão da Segtrack está dentro da rota agora, baixa o trajeto até este instante."""
    from .api import escolher_frota, eventos_atuais, listar_veiculos, login
    from .config import aplicar_periodo, dia_operacional, janela_datetime, load_settings
    from .gps import para_horario_brasilia
    from .relatorios import _achar_setor, _ler

    if dia != dia_operacional():
        return None
    pasta = output_dir / dia.isoformat()
    setor = _achar_setor(_ler(pasta, "setores.json") or [], consulta)
    aneis = [anel for anel in (setor or {}).get("poligonos") or [] if anel and len(anel) >= 4]
    if not aneis:
        return None
    settings = aplicar_periodo(load_settings(), data=dia.isoformat())
    inicio, fim = janela_datetime(dia, settings.hora_inicio, settings.hora_fim, settings.data_fim)
    agora = datetime.now().replace(microsecond=0)
    if agora < inicio or agora > fim:
        return None
    try:
        token = login(settings)
        veiculos = listar_veiculos(settings, token)
        eventos = eventos_atuais(settings, token)
    except Exception as exc:
        print(f"Trajeto ao vivo indisponível: {exc}", flush=True)
        return None
    presentes = []
    for item in escolher_frota(veiculos, eventos, settings.veiculos):
        evento = item.get("_evento") or {}
        try:
            lat = float(evento.get("ras_eve_latitude"))
            lon = float(evento.get("ras_eve_longitude"))
        except (TypeError, ValueError):
            continue
        marca = _datetime_do_timestamp(
            para_horario_brasilia(str(evento.get("ras_eve_data_gps") or evento.get("ras_eve_data_enviado") or "")),
            dia,
        )
        if marca is None or agora - marca > timedelta(minutes=20):
            continue
        if _dentro_dos_aneis(aneis, lat, lon):
            presentes.append(item)
    if not presentes:
        return None
    with _AO_VIVO:
        return _gravar_trajeto_ao_vivo(pasta, dia, settings, inicio, agora, token, presentes, setor or {})


def _gravar_trajeto_ao_vivo(
    pasta: Path,
    dia: date,
    settings: Settings,
    inicio: datetime,
    agora: datetime,
    token: str,
    presentes: list[dict[str, Any]],
    setor: dict[str, Any],
) -> str | None:
    from .api import _ponto, em_aparecida, fmt_placa, historico_veiculo
    from .frota import prefixo_da_placa
    from .gps import filtrar_pontos_periodo
    from .relatorios import _ler, medir_presenca, pontos_leves

    frota = _ler(pasta, "frota.json") or []
    pontos_salvos = _ler(pasta, "pontos.json") or {}
    resumo = _ler(pasta, "resumo.json") or {}
    por_placa = {str(item.get("placa") or ""): item for item in frota}
    setores = carregar_setores(dia)
    atualizados = []
    for item in presentes:
        placa = fmt_placa(str(item.get("ras_vei_placa") or ""))
        vid = str(item.get("ras_vei_id") or "")
        try:
            eventos = historico_veiculo(settings, vid, inicio, agora, token) if vid else []
        except Exception as exc:
            print(f"Trajeto ao vivo {placa}: {exc}", flush=True)
            continue
        pontos = [p for p in (_ponto(ev, placa) for ev in eventos) if p]
        pontos = filtrar_pontos_periodo(
            pontos,
            dia,
            settings.hora_inicio,
            settings.hora_fim,
            data_fim=settings.data_fim,
        )
        pontos.sort(key=lambda p: str(p.get("timestamp") or ""))
        if not pontos:
            continue
        base = por_placa.get(placa) or {
            "veiculo": item.get("ras_vei_veiculo") or placa,
            "prefixo": prefixo_da_placa(placa),
            "placa": placa,
            "codigo_veiculo": vid,
            "fonte": "sagtrack",
            "erro": "",
        }
        base["pontos"] = pontos
        base["total_pontos"] = len(pontos)
        base["pontos_na_cidade"] = sum(1 for p in pontos if em_aparecida(p.get("latitude"), p.get("longitude")))
        base["km_dia"] = _km(pontos)
        atualizados.append(base)
    if not atualizados:
        return None
    print(
        f"Caminhão na rota {setor.get('codigo') or ''}. Atualizando trajeto até {agora.strftime('%H:%M')}.",
        flush=True,
    )
    aplicar_setores_oficiais(atualizados, setores, dia)
    medir_presenca(atualizados, setores, dia)
    for base in atualizados:
        placa = str(base.get("placa") or "")
        por_placa[placa] = base
        pontos_salvos[placa] = pontos_leves([base]).get(placa) or []
    folhas = []
    for folha in resumo.get("caminhoes") or []:
        placa = str(folha.get("placa") or "")
        if placa in {str(item.get("placa") or "") for item in atualizados}:
            continue
        folhas.append(folha)
    folhas.extend(folha_caminhao(item) for item in atualizados)
    resumo_novo = montar_resumo(dia, folhas, settings)
    (pasta / "resumo.json").write_text(json.dumps(resumo_novo, ensure_ascii=False), encoding="utf-8")
    (pasta / "pontos.json").write_text(json.dumps(pontos_salvos, ensure_ascii=False), encoding="utf-8")
    (pasta / "frota.json").write_text(
        json.dumps([_sem_pontos(item) for item in por_placa.values()], ensure_ascii=False, default=str),
        encoding="utf-8",
    )
    nomes = []
    marcas = []
    for item in atualizados:
        prefixo = str(item.get("prefixo") or "")
        placa = str(item.get("placa") or "")
        nomes.append(f"{prefixo} · {placa}".strip(" ·"))
        if item.get("pontos"):
            marcas.append(str(item["pontos"][-1].get("timestamp") or ""))
    hora = agora.strftime("%H:%M")
    if marcas:
        ultima = max(marcas)
        hora = ultima.split(" ")[-1][:5] or hora
    return (
        f"{', '.join(nomes)} está nesta rota agora. "
        f"O trajeto foi atualizado até {hora} para o relatório sair como está neste momento."
    )


def ultimo_resumo(pasta_output: Path) -> dict[str, Any] | None:
    if not pasta_output.is_dir():
        return None
    dias = sorted((p for p in pasta_output.iterdir() if (p / "resumo.json").is_file()), key=lambda p: p.name)
    if not dias:
        return None
    return json.loads((dias[-1] / "resumo.json").read_text(encoding="utf-8"))
