"""Agente polígono-primeiro: cada setor oficial recebe o GPS da Segtrack."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .cobertura import (
    BORDA_M,
    DENTRO_M,
    _xy,
    cobertura_espacial,
    km_gps,
    passagens_na_rota,
    preparar_pontos,
    recortar_pontos,
    separar_dentro_borda,
)
from .config import dias_do_ciclo, rotulo_dia_periodo, rotulo_periodo
from .export import write_json, write_mapa_html
from .gps import extremos_por_hora, filtrar_pontos_periodo, periodo_gps
from .mapa_oficial import _freq_do_dia, carregar_setores
from .pdf_mapa import cor_da_placa
from .relatorio_diario import _esc, css_relatorio_web, mapa_frota_inlog
from .traco_agente import montar_traco_continuo, recortar_trechos_ao_setor
from .verificacao import VEREDICTOS_OK, _tortuosidade, analisar_execucao, rotulo_papel_relatorio

FONTES = (
    ("segtrack", "segtrack", "Segtrack"),
)

CLIP_MIN = 40
CLIP_MIN_DENTRO = 18
KM_MIN = 4.0


def _nome_pacote() -> str:
    try:
        from . import config as cfg

        fn = getattr(cfg, "rotulo_pacote", None)
        if callable(fn):
            return str(fn() or "")
    except Exception:
        pass
    return ""
KM_MIN_DENTRO = 1.6
PASSAGEM_PTS_DENTRO = 3
PASSAGEM_PTS_ZONA = 6
PASSAGEM_KM = 0.12
CLIP_INSPECAO_M = 40
CLIP_MAPA_M = 40
MIN_PULSOS_SEGTRACK = 80
APOIO_PCT_MIN = 15.0
APOIO_PCT_MAX = 45.0
APOIO_TORTO_MIN = 1.8
APOIO_MINUTOS_MIN = 20.0
def _confianca_relatorio(
    pct: float,
    n_pts: int,
    km_coleta: float,
    km_oficial: float,
    tem_hora: bool,
) -> str:
    """Alta só com pulso, densidade e km coerentes. Nunca assume 100%."""
    if n_pts < 18:
        return "baixa"
    if not tem_hora:
        return "media"
    dens = n_pts / max(km_coleta, 0.4)
    razao = km_coleta / max(km_oficial, 0.1)
    if pct >= 80 and dens >= 12 and razao >= 0.40:
        return "alta"
    if pct >= 45 and dens >= 8 and razao >= 0.22:
        return "media"
    if pct >= 25:
        return "media"
    return "baixa"


def _pontos_segtrack_brt(pontos: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    try:
        from .gps import pontos_segtrack_brt as conv

        return conv(pontos)
    except ImportError:
        return list(pontos or [])


def _norm(texto: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", (texto or "").upper())


def _itens_gps(dados: Any) -> list[dict[str, Any]]:
    if isinstance(dados, list):
        return [item for item in dados if isinstance(item, dict)]
    if not isinstance(dados, dict):
        return []
    for chave in ("rotas", "frota", "caminhoes"):
        bloco = dados.get(chave)
        if isinstance(bloco, list):
            return [item for item in bloco if isinstance(item, dict)]
    return []


def _tem_pontos_gps(itens: list[dict[str, Any]]) -> bool:
    return any((item.get("pontos_dia") or item.get("pontos")) for item in itens)


def _ler_rotas(pasta: Path) -> list[dict[str, Any]]:
    for nome in ("frota.json", "rotas.json"):
        arquivo = pasta / nome
        if not arquivo.is_file():
            continue
        try:
            dados = json.loads(arquivo.read_text(encoding="utf-8"))
        except Exception:
            continue
        itens = _itens_gps(dados)
        if _tem_pontos_gps(itens):
            return itens
    return []


def carregar_caminhoes(
    output_dir: Path,
    dia: date,
    hora_inicio: str = "00:00",
    hora_fim: str = "23:59",
    data_fim: date | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    frota = mapa_frota_inlog(output_dir, dia)
    caminhoes_por_id: dict[tuple[str, str], dict[str, Any]] = {}
    fontes: dict[str, str] = {}
    for chave, pasta_rel, rotulo in FONTES:
        rotas_ciclo: list[dict[str, Any]] = []
        for dia_pasta in dias_do_ciclo(dia, hora_inicio, hora_fim, data_fim):
            rotas_ciclo.extend(_ler_rotas(output_dir / pasta_rel / dia_pasta.isoformat()))
        fontes[chave] = rotulo if rotas_ciclo else "sem extração"
        for item in rotas_ciclo:
            pontos = item.get("pontos_dia") or item.get("pontos") or []
            if chave == "segtrack":
                pontos = _pontos_segtrack_brt(pontos)
            pontos = filtrar_pontos_periodo(
                pontos, dia, hora_inicio, hora_fim, data_fim=data_fim
            )
            if not pontos:
                continue
            placa = str(item.get("placa") or "")
            veiculo = str(item.get("veiculo") or placa)
            inlog = str(item.get("inlog") or "").strip()
            if not inlog:
                ident = frota.get(_norm(placa))
                if ident:
                    inlog = ident.get("veiculo") or ""
                    if not str(veiculo).upper().startswith("CCL") and inlog:
                        veiculo = inlog
            ident_cam = _id_caminhao(veiculo, placa, inlog)
            chave_id = (chave, ident_cam)
            if chave_id in caminhoes_por_id:
                caminhoes_por_id[chave_id]["pontos"].extend(pontos)
                continue
            caminhoes_por_id[chave_id] = {
                "fonte": chave,
                "fonte_nome": rotulo,
                "veiculo": veiculo,
                "placa": placa,
                "inlog": inlog,
                "pontos": list(pontos),
                "id": ident_cam,
            }
    caminhoes = [cam for cam in caminhoes_por_id.values() if len(cam.get("pontos") or []) >= 8]
    return _fundir_mesmo_caminhao(caminhoes), fontes


def _chaves_caminhao(cam: dict[str, Any]) -> set[str]:
    """Identidade real: placa ou código CCL. Modelo (VW 17.260) não junta caminhões."""
    chaves: set[str] = set()
    placa = _norm(str(cam.get("placa") or ""))
    if len(placa) >= 6:
        chaves.add(placa)
    for campo in ("inlog", "id"):
        valor = _norm(str(cam.get(campo) or ""))
        if valor.startswith("CCL") and len(valor) >= 6:
            chaves.add(valor)
    return chaves


def _fundir_mesmo_caminhao(caminhoes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Um caminhão = um GPS da Segtrack. Quem não andou hoje não entra até ligar e sair."""
    grupos: list[list[dict[str, Any]]] = []
    for cam in caminhoes:
        chaves = _chaves_caminhao(cam)
        alvo = None
        for grupo in grupos:
            unidos = set()
            for item in grupo:
                unidos |= _chaves_caminhao(item)
            if chaves & unidos:
                alvo = grupo
                break
        if alvo is None:
            grupos.append([cam])
        else:
            alvo.append(cam)
    saida: list[dict[str, Any]] = []
    for grupo in grupos:
        if len(grupo) == 1:
            saida.append(grupo[0])
            continue
        segs = [
            item
            for item in grupo
            if str(item.get("fonte") or "").lower() == "segtrack"
            and len(item.get("pontos") or []) >= MIN_PULSOS_SEGTRACK
        ]
        if segs:
            segs.sort(key=lambda c: len(c.get("pontos") or []), reverse=True)
            base = dict(segs[0])
            pontos = []
            for item in segs:
                pontos.extend(item.get("pontos") or [])
            pontos.sort(key=lambda p: str(p.get("timestamp") or ""))
            base["pontos"] = pontos
            base["fonte"] = "segtrack"
            base["fonte_nome"] = "Segtrack"
            for item in grupo:
                if item.get("inlog"):
                    base["inlog"] = item["inlog"]
                    break
            for item in grupo:
                vei = str(item.get("veiculo") or "")
                if _norm(vei).startswith("CCL"):
                    base["veiculo"] = item["veiculo"]
                    break
            saida.append(base)
            continue
        grupo = [
            item
            for item in grupo
            if str(item.get("fonte") or "").lower() != "segtrack"
        ] or grupo
        grupo.sort(key=lambda c: len(c.get("pontos") or []), reverse=True)
        base = dict(grupo[0])
        pontos = []
        fontes_nome = []
        vistos_fonte = set()
        for item in grupo:
            pontos.extend(item.get("pontos") or [])
            nome = str(item.get("fonte_nome") or item.get("fonte") or "")
            if nome and nome not in vistos_fonte:
                vistos_fonte.add(nome)
                fontes_nome.append(nome)
        pontos.sort(key=lambda p: str(p.get("timestamp") or ""))
        if not base.get("inlog"):
            for item in grupo:
                if item.get("inlog"):
                    base["inlog"] = item["inlog"]
                    break
        if not base.get("placa") or len(_norm(str(base.get("placa") or ""))) < 7:
            for item in grupo:
                if len(_norm(str(item.get("placa") or ""))) >= 7:
                    base["placa"] = item["placa"]
                    break
        base["pontos"] = pontos
        base["fonte_nome"] = " + ".join(fontes_nome) if fontes_nome else base.get("fonte_nome")
        saida.append(base)
    return saida


def _id_caminhao(veiculo: str, placa: str, inlog: str) -> str:
    for valor in (inlog, veiculo):
        chave = _norm(valor)
        if chave.startswith("CCL") and len(chave) >= 6:
            return chave
    placa_n = _norm(placa)
    return placa_n or _norm(veiculo) or "sem-id"


def _bbox_pontos(pontos: list[dict[str, Any]], folga_m: float = 180.0) -> tuple[float, float, float, float] | None:
    xs: list[float] = []
    ys: list[float] = []
    for ponto in pontos or []:
        par = ponto.get("_utm") if isinstance(ponto, dict) else None
        if not par:
            par = _xy(ponto)
        if par:
            xs.append(par[0])
            ys.append(par[1])
    if not xs:
        return None
    return (min(xs) - folga_m, min(ys) - folga_m, max(xs) + folga_m, max(ys) + folga_m)


def _bbox_cruza(geom, bbox: tuple[float, float, float, float] | None) -> bool:
    if geom is None or bbox is None:
        return True
    minx, miny, maxx, maxy = geom.bounds
    return not (maxx < bbox[0] or minx > bbox[2] or maxy < bbox[1] or miny > bbox[3])


def analisar_setores(
    dia: date,
    caminhoes: list[dict[str, Any]],
    on_progress: Callable[[str], None] | None = None,
    turno: str | None = None,
) -> list[dict[str, Any]]:
    todos = carregar_setores(None)
    if (turno or "").strip().lower() in {"diurno", "noturno"}:
        pedido = turno.strip().lower()
        todos = [
            setor
            for setor in todos
            if _freq_do_dia(str(setor.get("frequencia") or ""), dia)
            and _casa_turno(str(setor.get("frequencia") or ""), pedido)
        ]
    for cam in caminhoes:
        cam["_pares"] = preparar_pontos(cam.get("pontos") or [])
        cam["_bbox"] = _bbox_pontos(cam.get("pontos") or [])
    for setor in todos:
        geom = setor.get("geom")
        if geom is None:
            setor["_zona"] = None
            setor["_nucleo"] = None
            continue
        setor["_zona"] = geom.buffer(BORDA_M)
        setor["_nucleo"] = geom.buffer(DENTRO_M)
    saida: list[dict[str, Any]] = []
    total = len(todos)

    def _uma(item: tuple[int, dict[str, Any]]) -> tuple[int, dict[str, Any]]:
        i, setor = item
        row = _analisar_setor(setor, caminhoes, dia)
        row["grade_do_dia"] = _freq_do_dia(str(setor.get("frequencia") or ""), dia)
        return i, row

    with ThreadPoolExecutor(max_workers=8) as pool:
        futuros = [pool.submit(_uma, (i, setor)) for i, setor in enumerate(todos, start=1)]
        for feito, fut in enumerate(as_completed(futuros), start=1):
            i, row = fut.result()
            saida.append(row)
            if on_progress and (feito == 1 or feito % 10 == 0 or feito == total):
                on_progress(f"Mapa oficial {feito}/{total}: {row.get('codigo')}")
    saida.sort(key=lambda row: str(row.get("codigo") or ""))
    return saida


def _passou_no_setor(dentro: list, zona: list, km_zona: float) -> bool:
    """GPS no polígono (ou via interna), não no vizinho. 3 pulsos dentro = passagem certa."""
    if len(dentro) >= PASSAGEM_PTS_DENTRO:
        return True
    if len(dentro) >= 1 and len(zona) >= PASSAGEM_PTS_ZONA and km_zona >= PASSAGEM_KM:
        return True
    return False


def _papel_caminhao(execucao: str, status_setor: str = "") -> str:
    st = (status_setor or "").upper()
    ex = (execucao or "").upper()
    if st == "APOIO" or ex == "APOIO":
        return "apoio"
    if ex in VEREDICTOS_OK:
        return "coleta"
    return "passagem"


def _montar_caminhoes(
    candidatos: list[dict[str, Any]],
    status_setor: str,
    vencedor: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    saida: list[dict[str, Any]] = []
    ordenados = sorted(
        candidatos,
        key=lambda row: (-float(row.get("percentual") or 0), str(row.get("placa") or "")),
    )
    for cand in ordenados:
        papel = _papel_caminhao(str(cand.get("execucao") or ""), "")
        if status_setor.upper() == "APOIO" and vencedor and _mesmo_caminhao(cand, vencedor):
            papel = "apoio"
        saida.append(
            {
                "id": cand.get("id"),
                "placa": cand.get("placa") or "",
                "veiculo": cand.get("veiculo") or "",
                "fonte": cand.get("fonte"),
                "fonte_nome": cand.get("fonte_nome"),
                "inlog": cand.get("inlog"),
                "cor": cor_da_placa(str(cand.get("placa") or "")),
                "papel": papel,
                "percentual": float(cand.get("percentual") or 0),
                "km_coleta": cand.get("km_coleta"),
                "pontos_dentro": cand.get("pontos_dentro"),
                "pontos_no_setor": cand.get("pontos_no_setor"),
                "horario": cand.get("horario") or "",
                "hora_inicio": cand.get("hora_inicio") or "",
                "hora_fim": cand.get("hora_fim") or "",
                "ponto_inicio": cand.get("ponto_inicio") or [],
                "ponto_fim": cand.get("ponto_fim") or [],
                "passagens": cand.get("passagens") or [],
                "trechos": cand.get("trechos") or [],
                "lacunas": cand.get("lacunas") or [],
                "execucao": cand.get("execucao") or "",
                "motivo": cand.get("motivo") or "",
                "tortuosidade": cand.get("tortuosidade"),
            }
        )
    return saida


def _txt_caminhoes(setor: dict[str, Any]) -> str:
    partes = []
    for cam in setor.get("caminhoes") or []:
        placa = str(cam.get("placa") or cam.get("veiculo") or "").strip()
        if not placa:
            continue
        papel = str(cam.get("papel") or "passagem")
        pct = float(cam.get("percentual") or 0)
        hora = str(cam.get("horario") or "").strip()
        extra = f"{papel} {pct:.0f}%"
        if hora:
            extra += f" {hora}"
        partes.append(f"{placa} ({extra})")
    return " · ".join(partes)


def _analisar_setor(setor: dict[str, Any], caminhoes: list[dict[str, Any]], dia: date) -> dict[str, Any]:
    codigo = str(setor.get("codigo") or "")
    clips: list[tuple[dict[str, Any], list, list, float]] = []
    for cam in caminhoes:
        if not _bbox_cruza(setor.get("geom"), cam.get("_bbox")):
            continue
        dentro, _borda, zona = separar_dentro_borda(
            cam["pontos"],
            setor["geom"],
            pares=cam.get("_pares"),
            zona_geom=setor.get("_zona"),
            nucleo_geom=setor.get("_nucleo"),
        )
        km_dentro = km_gps(dentro, salto_m=180)
        km_zona = km_gps(zona, salto_m=180)
        if not _passou_no_setor(dentro, zona, km_zona):
            continue
        clips.append((cam, dentro, zona, km_dentro if dentro else km_zona))

    base = {
        "codigo": codigo,
        "setor": setor.get("completo") or f"{codigo} - {setor.get('nome')}",
        "frequencia": setor.get("frequencia") or "",
        "km_oficial": setor.get("km_oficial"),
        "moradias": setor.get("moradias"),
        "poligonos": setor.get("poligonos") or [],
        "vias": setor.get("vias") or [],
        "candidatos": [],
        "caminhoes": [],
        "status": "NÃO FEITA",
        "percentual": 0.0,
        "confianca": "baixa",
        "motivo": "nenhum caminhão com volume de coleta neste polígono",
        "caminhao": None,
        "_geom": setor.get("geom"),
    }
    if not clips:
        return base

    melhor_pts = max(len(dentro) or len(zona) for _, dentro, zona, _ in clips) or 1
    melhor_km = max(km for _, _, _, km in clips) or 0.1
    candidatos: list[dict[str, Any]] = []
    for cam, dentro, zona, km in clips:
        uso = dentro if len(dentro) >= PASSAGEM_PTS_DENTRO else zona
        mapa = cobertura_espacial(uso, setor.get("geom"))
        km_rua = km_gps(uso, salto_m=80)
        pulso = extremos_por_hora(uso, dia)
        _ini, _fim, horario = pulso.get("inicio_dt"), pulso.get("fim_dt"), str(pulso.get("horario") or "")
        if not horario:
            _ini, _fim, horario = periodo_gps(uso, dia)
        cob = {
            "percentual": mapa["percentual"],
            "veredicto": (
                "FEITA" if mapa["percentual"] >= 80
                else "PARCIAL" if mapa["percentual"] >= 45
                else "NÃO FEITA"
            ),
            "confianca": _confianca_relatorio(
                mapa["percentual"],
                len(uso),
                km_rua,
                float(setor.get("km_oficial") or 0),
                bool(horario),
            ),
            "metodo": "mapa",
            "km_coleta": round(km_rua, 3),
            "km_gps": round(km, 3),
            "area_pct": mapa["area_pct"],
            "pontos_no_setor": len(uso),
            "pontos_dentro": len(dentro),
            "pontos_clip": uso,
        }
        analise = analisar_execucao(
            cob,
            {"pontos": uso},
            melhor_pts=melhor_pts,
            melhor_km=melhor_km,
        )
        visitas = passagens_na_rota(cam.get("pontos") or [], setor.get("geom"), dia)
        if visitas:
            trechos = [v["trecho"] for v in visitas if len(v.get("trecho") or []) >= 2]
            horario = " · ".join(v["horario"] for v in visitas if v.get("horario"))
            pulso = {
                "inicio": visitas[0].get("ponto_inicio") or [],
                "fim": visitas[-1].get("ponto_fim") or [],
                "hora_inicio": visitas[0].get("hora_inicio") or "",
                "hora_fim": visitas[-1].get("hora_fim") or "",
                "horario": horario,
            }
        else:
            traco = montar_traco_continuo(uso)
            trechos = recortar_trechos_ao_setor(
                traco.get("trechos") or [],
                setor.get("poligonos") or [],
                buffer_m=CLIP_MAPA_M,
            )
        row = {
            "id": cam["id"],
            "fonte": cam["fonte"],
            "fonte_nome": cam["fonte_nome"],
            "veiculo": cam["veiculo"],
            "placa": cam["placa"],
            "inlog": cam["inlog"],
            "pontos_no_setor": len(uso),
            "pontos_dentro": len(dentro),
            "km_coleta": round(float(cob.get("km_coleta") or km_rua), 3),
            "percentual": float(cob.get("percentual") or 0),
            "area_pct": cob.get("area_pct"),
            "veredicto_gps": cob.get("veredicto"),
            "confianca": cob.get("confianca"),
            "execucao": analise["execucao"],
            "motivo": analise["motivo"],
            "horario": horario,
            "hora_inicio": pulso.get("hora_inicio") or "",
            "hora_fim": pulso.get("hora_fim") or "",
            "ponto_inicio": pulso.get("inicio") or [],
            "ponto_fim": pulso.get("fim") or [],
            "passagens": visitas,
            "trechos": trechos,
            "lacunas": [],
            "clip": uso,
            "tortuosidade": round(_tortuosidade(uso), 2),
        }
        candidatos.append(row)

    coletaram = [c for c in candidatos if c["execucao"] in VEREDICTOS_OK]
    base["candidatos"] = [
        {k: v for k, v in c.items() if k not in {"clip", "trechos", "lacunas"}}
        for c in sorted(candidatos, key=lambda r: r["percentual"], reverse=True)
    ]
    if not coletaram:
        melhor = max(candidatos, key=lambda r: r["pontos_no_setor"])
        placas = ", ".join(
            f"{c.get('placa') or c.get('veiculo')}"
            for c in sorted(candidatos, key=lambda r: r["percentual"], reverse=True)
        )
        base["status"] = "PASSAGEM"
        base["percentual"] = melhor["percentual"]
        base["confianca"] = melhor.get("confianca") or "baixa"
        base["motivo"] = (
            f"passagem (não coleta) de {placas}: "
            f"{melhor['pontos_no_setor']} pts, {melhor['percentual']:.0f}% — {melhor['motivo']}"
        )
        base["caminhao"] = {
            "id": melhor["id"],
            "veiculo": melhor["veiculo"],
            "placa": melhor["placa"],
            "inlog": melhor.get("inlog"),
            "fonte": melhor["fonte"],
            "fonte_nome": melhor["fonte_nome"],
            "pontos_no_setor": melhor["pontos_no_setor"],
            "pontos_dentro": melhor.get("pontos_dentro"),
            "km_coleta": melhor["km_coleta"],
            "horario": melhor.get("horario") or "",
            "hora_inicio": melhor.get("hora_inicio") or "",
            "hora_fim": melhor.get("hora_fim") or "",
            "ponto_inicio": melhor.get("ponto_inicio") or [],
            "ponto_fim": melhor.get("ponto_fim") or [],
            "passagens": melhor.get("passagens") or [],
            "trechos": melhor.get("trechos") or [],
            "lacunas": melhor.get("lacunas") or [],
            "evidencias": "passagem, não coleta",
            "tortuosidade": melhor.get("tortuosidade"),
        }
        base["caminhoes"] = _montar_caminhoes(candidatos, "PASSAGEM", melhor)
        return base

    vencedor, evidencias = _escolher_vencedor(coletaram)
    base.update(
        {
            "status": vencedor["execucao"],
            "percentual": vencedor["percentual"],
            "confianca": vencedor["confianca"],
            "motivo": vencedor["motivo"],
            "caminhao": {
                "id": vencedor["id"],
                "veiculo": vencedor["veiculo"],
                "placa": vencedor["placa"],
                "inlog": vencedor["inlog"],
                "fonte": vencedor["fonte"],
                "fonte_nome": vencedor["fonte_nome"],
                "pontos_no_setor": vencedor["pontos_no_setor"],
                "pontos_dentro": vencedor.get("pontos_dentro"),
                "km_coleta": vencedor["km_coleta"],
                "horario": vencedor.get("horario") or "",
                "hora_inicio": vencedor.get("hora_inicio") or "",
                "hora_fim": vencedor.get("hora_fim") or "",
                "ponto_inicio": vencedor.get("ponto_inicio") or [],
                "ponto_fim": vencedor.get("ponto_fim") or [],
                "passagens": vencedor.get("passagens") or [],
                "trechos": vencedor["trechos"],
                "lacunas": vencedor.get("lacunas") or [],
                "evidencias": evidencias,
                "tortuosidade": vencedor.get("tortuosidade"),
            },
        }
    )
    if evidencias:
        base["motivo"] = f"{vencedor['motivo']}. {evidencias}"
        if "diverg" in evidencias.lower():
            base["confianca"] = "revisar"
    outros = [c for c in candidatos if not _mesmo_caminhao(c, vencedor)]
    if outros:
        placas_outros = ", ".join(
            f"{c.get('placa') or c.get('veiculo')} "
            f"({_papel_caminhao(str(c.get('execucao') or ''))} {c['percentual']:.0f}%)"
            for c in sorted(outros, key=lambda r: r["percentual"], reverse=True)
        )
        extra_pass = f" Também no polígono: {placas_outros}."
        if extra_pass.strip() not in str(base.get("motivo") or ""):
            base["motivo"] = f"{base.get('motivo') or ''}{extra_pass}".strip()
    base["caminhoes"] = _montar_caminhoes(candidatos, str(base.get("status") or ""), vencedor)
    return base


def _escolher_vencedor(coletaram: list[dict[str, Any]]) -> tuple[dict[str, Any], str]:
    por_id: dict[str, list[dict[str, Any]]] = {}
    for row in coletaram:
        por_id.setdefault(row["id"], []).append(row)
    escolhidos = []
    notas = []
    for _id, grupo in por_id.items():
        grupo.sort(key=lambda r: (r["pontos_no_setor"], r["percentual"]), reverse=True)
        melhor = grupo[0]
        escolhidos.append(melhor)
        if len(grupo) > 1:
            outros = ", ".join(
                f"{g['fonte_nome']} {g['percentual']:.0f}%/{g['pontos_no_setor']} pts" for g in grupo[1:]
            )
            delta = abs(melhor["percentual"] - grupo[1]["percentual"])
            extra = f"mesmo caminhão também em {outros}"
            if delta >= 20:
                extra += f" — divergência de {delta:.0f} pp; usei a fonte com mais GPS ({melhor['fonte_nome']})"
            notas.append(extra)
    escolhidos.sort(key=lambda r: (r["km_coleta"], r["percentual"], r["pontos_no_setor"]), reverse=True)
    vencedor = escolhidos[0]
    if len(escolhidos) > 1 and escolhidos[1]["km_coleta"] >= 0.6 * max(vencedor["km_coleta"], 0.1):
        segundo = escolhidos[1]
        notas.append(
            f"também coletou {segundo['veiculo']} ({segundo['fonte_nome']}) "
            f"{segundo['percentual']:.0f}% / {segundo['km_coleta']:.1f} km — setor possivelmente dividido"
        )
    return vencedor, "; ".join(notas)


def _codigos_vizinhos(a: str, b: str) -> bool:
    m1 = re.fullmatch(r"(\d{2})([A-Z]{2,4})", (a or "").upper())
    m2 = re.fullmatch(r"(\d{2})([A-Z]{2,4})", (b or "").upper())
    if m1 and m2 and m1.group(2) == m2.group(2):
        return abs(int(m1.group(1)) - int(m2.group(1))) == 1
    return False


def _casa_turno(freq: str, turno: str | None) -> bool:
    pedido = (turno or "").strip().lower()
    if pedido not in {"diurno", "noturno"}:
        return True
    noturno = "NOTURNO" in (freq or "").upper()
    if pedido == "noturno":
        return noturno
    return not noturno


def _mesmo_ciclo(a: dict[str, Any], b: dict[str, Any]) -> bool:
    fa = str(a.get("frequencia") or "").upper()
    fb = str(b.get("frequencia") or "").upper()
    if fa and fb and fa == fb:
        return True
    def turno(freq: str) -> str:
        if "NOTURNO" in freq:
            return "N"
        if "DIURNO" in freq:
            return "D"
        return ""
    return bool(turno(fa) and turno(fa) == turno(fb))


def _sao_vizinhos(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if not _mesmo_ciclo(a, b):
        return False
    if _codigos_vizinhos(str(a.get("codigo") or ""), str(b.get("codigo") or "")):
        return True
    ga, gb = a.get("_geom"), b.get("_geom")
    if ga is None or gb is None:
        return False
    try:
        if ga.intersects(gb) or ga.touches(gb):
            return True
        return float(ga.distance(gb)) <= 80
    except Exception:
        return False


def _trabalhou_no_poligono(setor: dict[str, Any]) -> bool:
    """Apoio: 15–45% e traço de coleta (não avenida em linha reta)."""
    pct = float(setor.get("percentual") or 0)
    if pct < APOIO_PCT_MIN or pct >= APOIO_PCT_MAX:
        return False
    cam = setor.get("caminhao") or {}
    minutos = _minutos_no_setor(cam)
    if minutos < APOIO_MINUTOS_MIN:
        return False
    torto = cam.get("tortuosidade")
    if torto in (None, ""):
        torto = _tortuosidade(cam.get("clip") or [])
    try:
        torto = float(torto)
    except (TypeError, ValueError):
        torto = 1.0
    return torto >= APOIO_TORTO_MIN


def _parece_apoio(principal: dict[str, Any], vizinho: dict[str, Any]) -> bool:
    if not _mesmo_caminhao(principal.get("caminhao"), vizinho.get("caminhao")):
        return False
    return _trabalhou_no_poligono(vizinho)


def _mesmo_caminhao(a: dict[str, Any] | None, b: dict[str, Any] | None) -> bool:
    if not a or not b:
        return False
    chaves = set()
    outros = set()
    for origem, destino in ((a, chaves), (b, outros)):
        for campo in ("id", "inlog", "placa", "veiculo"):
            norm = _norm(str(origem.get(campo) or ""))
            if norm:
                destino.add(norm)
    return bool(chaves & outros)


def _marcar_apoio(principal: dict[str, Any], vizinho: dict[str, Any]) -> None:
    cam_p = principal.get("caminhao") or {}
    cam_v = vizinho.get("caminhao") or {}
    hora_rota = cam_p.get("horario") or "horário não informado"
    hora_apoio = cam_v.get("horario") or "horário não informado"
    codigo_p = str(principal.get("codigo") or "")
    codigo_v = str(vizinho.get("codigo") or "")
    veiculo = cam_p.get("veiculo") or cam_v.get("veiculo") or "caminhão"
    vizinho["status"] = "APOIO"
    vizinho["rota_principal"] = codigo_p
    vizinho["horario_rota"] = hora_rota
    vizinho["horario_apoio"] = hora_apoio
    vizinho["motivo"] = (
        f"Apoio de {veiculo} no polígono vizinho {codigo_v} ({hora_apoio}). "
        f"A rota do caminhão é {codigo_p} ({hora_rota}). "
        "Entrou no setor ao lado para completar o caminhão; não assume a coleta desta rota."
    )
    cam_v["evidencias"] = f"apoio à rota {codigo_p}"
    for item in vizinho.get("caminhoes") or []:
        if _mesmo_caminhao(item, cam_v):
            item["papel"] = "apoio"
    apoios = list(principal.get("apoios") or [])
    if not any(item.get("codigo") == codigo_v for item in apoios):
        apoios.append(
            {
                "codigo": codigo_v,
                "setor": vizinho.get("setor"),
                "horario": hora_apoio,
                "percentual": float(vizinho.get("percentual") or 0),
            }
        )
    principal["apoios"] = apoios
    extra = f"Apoio em {codigo_v} das {hora_apoio} (rota {codigo_p} das {hora_rota})."
    motivo_p = str(principal.get("motivo") or "").strip()
    if extra not in motivo_p:
        principal["motivo"] = f"{motivo_p} {extra}".strip()


def _minutos_no_setor(cam: dict[str, Any]) -> float:
    hi = str(cam.get("hora_inicio") or "").strip()
    hf = str(cam.get("hora_fim") or "").strip()
    horario = str(cam.get("horario") or "").strip()
    texto = f"{hi} {hf} {horario}"
    horas = re.findall(r"(\d{1,2}):(\d{2})", texto)
    if len(horas) < 2:
        return 0.0
    h0, m0 = int(horas[0][0]), int(horas[0][1])
    h1, m1 = int(horas[-1][0]), int(horas[-1][1])
    ini = timedelta(hours=h0, minutes=m0)
    fim = timedelta(hours=h1, minutes=m1)
    if fim < ini:
        fim += timedelta(days=1)
    return (fim - ini).total_seconds() / 60.0


def _passagem_residual_e_apoio(principal: dict[str, Any], vizinho: dict[str, Any]) -> bool:
    """Quem já coletou outra rota e trabalhou 15–45% neste polígono (não cruzou em linha)."""
    if not vizinho.get("grade_do_dia"):
        return False
    if str(vizinho.get("status") or "") != "PASSAGEM":
        return False
    return _parece_apoio(principal, vizinho)


def aplicar_apoios(setores: list[dict[str, Any]]) -> None:
    """Setor vizinho com GPS residual do caminhão da rota principal vira apoio, com horário."""
    principais = [
        s for s in setores
        if s.get("status") in VEREDICTOS_OK and s.get("caminhao")
    ]
    principais.sort(
        key=lambda s: (
            float(s.get("percentual") or 0),
            float((s.get("caminhao") or {}).get("km_coleta") or 0),
        ),
        reverse=True,
    )
    for principal in principais:
        if principal.get("status") == "APOIO":
            continue
        for vizinho in setores:
            if vizinho is principal or vizinho.get("status") == "APOIO":
                continue
            if not _mesmo_caminhao(principal.get("caminhao"), vizinho.get("caminhao")):
                continue
            status_v = str(vizinho.get("status") or "")
            pct_v = float(vizinho.get("percentual") or 0)
            if pct_v >= 45:
                continue
            if _sao_vizinhos(principal, vizinho):
                if status_v in VEREDICTOS_OK:
                    if pct_v >= float(principal.get("percentual") or 0):
                        continue
                    if not _parece_apoio(principal, vizinho):
                        continue
                elif status_v == "PASSAGEM":
                    if not _parece_apoio(principal, vizinho):
                        continue
                else:
                    continue
                _marcar_apoio(principal, vizinho)
                continue
            if _passagem_residual_e_apoio(principal, vizinho):
                _marcar_apoio(principal, vizinho)


def _pasta_relatorio_fonte(output_dir: Path, fonte: str, dia: date) -> Path:
    chave = (fonte or "unificado").lower()
    nome = {"unificado": "unificado", "segtrack": "segtrack", "argos": "argos"}.get(chave, "rotas")
    return Path(output_dir) / nome / dia.isoformat()


def _ler_cobertura_grade(pasta: Path) -> dict[str, float]:
    arq = pasta / "cobertura-grade.json"
    if not arq.is_file():
        return {}
    try:
        dados = json.loads(arq.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    bruto = dados.get("percentuais") if isinstance(dados, dict) else None
    if not isinstance(bruto, dict):
        return {}
    saida: dict[str, float] = {}
    for chave, valor in bruto.items():
        codigo = str(chave or "").strip().upper()
        if not codigo:
            continue
        try:
            saida[codigo] = float(valor)
        except (TypeError, ValueError):
            continue
    return saida


def _gravar_cobertura_grade(pasta: Path, setores: list[dict[str, Any]], dia: date) -> None:
    grade: dict[str, float] = {}
    for setor in setores:
        codigo = str(setor.get("codigo") or "").strip().upper()
        if not codigo:
            continue
        pct = float(setor.get("percentual") or 0)
        if codigo not in grade or pct > grade[codigo]:
            grade[codigo] = pct
    write_json(pasta / "cobertura-grade.json", {"data": dia.isoformat(), "percentuais": grade})


def _ler_pct_diario(diario: Path) -> dict[str, float]:
    """% da grade daquele dia. Se o código repetir, fica o maior. Concluída conta como feita."""
    if not diario.is_file():
        return {}
    try:
        dados = json.loads(diario.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    grade: dict[str, float] = {}
    outros: dict[str, float] = {}
    for row in dados.get("linhas") or []:
        setor = str(row.get("Setor") or "")
        achado = re.search(r"\b(\d{2}[A-Z]{2,4})\b", setor, re.I)
        if not achado:
            continue
        codigo = achado.group(1).upper()
        bruto = str(row.get("% Concluída") or "0").replace("%", "").replace(",", ".").strip()
        try:
            pct = float(bruto or 0)
        except ValueError:
            continue
        status = str(row.get("Status") or "").upper()
        if "CONCLU" in status and "PARCIAL" not in status:
            pct = max(pct, 80.0)
        na = str(row.get("Na grade") or "sim").strip().lower()
        alvo = grade if na not in {"não", "nao", "false", "0"} else outros
        if codigo not in alvo or pct > alvo[codigo]:
            alvo[codigo] = pct
    saida = dict(outros)
    saida.update(grade)
    return saida


def _pct_ontem(output_dir: Path | None, fonte: str, ontem: date) -> dict[str, float]:
    if output_dir is None:
        return {}
    ordem = [fonte, "unificado", "segtrack", "inlog", "argos"]
    vistos: set[str] = set()
    for chave in ordem:
        nome = (chave or "").lower()
        if nome in vistos:
            continue
        vistos.add(nome)
        pasta = _pasta_relatorio_fonte(output_dir, nome, ontem)
        saida = _ler_cobertura_grade(pasta)
        if saida:
            return saida
        saida = _ler_pct_diario(pasta / "relatorio-diario.json")
        if saida:
            return saida
    return {}


def _tem_pulso_hoje(setor: dict[str, Any]) -> bool:
    """Movimentação no polígono hoje: % do dia ≥ 1. Sem pulso, não lista."""
    if float(setor.get("percentual") or 0) >= 1:
        return True
    for cam in [setor.get("caminhao") or {}, *(setor.get("caminhoes") or [])]:
        if not cam:
            continue
        if float(cam.get("percentual") or 0) >= 1:
            return True
    return False


def _rotas_ontem_hoje(
    setores: list[dict[str, Any]],
    dia: date,
    output_dir: Path | None,
    fonte: str,
) -> tuple[list[dict[str, Any]], str]:
    """Grade de ontem não concluída, só se houver pulso no polígono hoje."""
    ontem = dia - timedelta(days=1)
    ontem_pct = _pct_ontem(output_dir, fonte, ontem)
    if not ontem_pct:
        return [], (
            f"Sem o relatório de {ontem.isoformat()} nesta pasta. "
            "O bloco Ontem não feitas · GPS hoje só aparece com o JSON do dia anterior "
            "(o pacote novo já leva esses arquivos)."
        )
    saida = []
    for setor in setores:
        freq = str(setor.get("frequencia") or "")
        if not _freq_do_dia(freq, ontem) or _freq_do_dia(freq, dia):
            continue
        if str(setor.get("status") or "").upper() == "APOIO":
            continue
        codigo = str(setor.get("codigo") or "").upper()
        pct_on = ontem_pct.get(codigo)
        if pct_on is None or pct_on >= 80:
            continue
        if float(setor.get("percentual") or 0) < 1 or not _tem_pulso_hoje(setor):
            continue
        setor["recuperada_ontem"] = True
        setor["fase_ontem"] = "gps hoje"
        setor["percentual_ontem"] = pct_on
        saida.append(setor)
    return saida, ""


def _achar_setor_oficial(codigo: str) -> dict[str, Any] | None:
    busca = str(codigo or "").strip().upper()
    achado = re.search(r"\b(\d{2}[A-Z]{2,4})\b", busca)
    if achado:
        busca = achado.group(1)
    if not busca:
        return None
    for setor in carregar_setores(None):
        if str(setor.get("codigo") or "").strip().upper() == busca:
            return setor
    return None


def inspecionar_rota(
    output_dir: Path,
    codigo: str,
    dia: date,
    hora_inicio: str = "06:00",
    hora_fim: str = "05:59",
    data_fim: date | None = None,
    gerar_pdf: bool = True,
) -> dict[str, Any]:
    """Corta o GPS do ciclo no polígono escolhido e monta mapa + PDF (06:00 → 05:59)."""
    setor = _achar_setor_oficial(codigo)
    if not setor:
        raise ValueError(f"Rota {codigo} não está no mapa oficial.")
    caminhoes, fontes = carregar_caminhoes(output_dir, dia, hora_inicio, hora_fim, data_fim)
    if not caminhoes:
        raise ValueError(
            "Não há GPS da Segtrack neste ciclo. Use Gerar relatório (ou Buscar GPS Segtrack) e tente de novo."
        )
    for cam in caminhoes:
        cam["_pares"] = preparar_pontos(cam.get("pontos") or [])
        cam["_bbox"] = _bbox_pontos(cam.get("pontos") or [])
    geom = setor.get("geom")
    if geom is not None:
        setor["_zona"] = geom.buffer(BORDA_M)
        setor["_nucleo"] = geom.buffer(DENTRO_M)
    else:
        setor["_zona"] = None
        setor["_nucleo"] = None
    row = _analisar_setor(setor, caminhoes, dia)
    row["grade_do_dia"] = _freq_do_dia(str(setor.get("frequencia") or ""), dia)
    row.pop("_geom", None)
    for i, cam in enumerate(row.get("caminhoes") or []):
        cam["cor"] = cor_da_placa(str(cam.get("placa") or ""), i)
    rotas_mapa = _rotas_mapa([row], clip_m=CLIP_INSPECAO_M)
    for item in rotas_mapa:
        item["inspecao"] = True
    pasta = output_dir / "unificado" / dia.isoformat()
    pasta.mkdir(parents=True, exist_ok=True)
    slug = str(row.get("codigo") or "rota").upper()
    titulo = (
        f"{row.get('setor') or slug} · "
        f"{rotulo_periodo(dia, hora_inicio, hora_fim, data_fim)}"
    )
    mapa = write_mapa_html(pasta / f"inspecao-{slug}.html", titulo, rotas_mapa)
    pdf = None
    if gerar_pdf:
        from .pdf import gerar_pdf_inspecao

        pdf = gerar_pdf_inspecao(pasta, rotas_mapa, slug=slug, periodo=titulo, fonte="unificado")
    caminhoes_info = []
    for cam in row.get("caminhoes") or []:
        caminhoes_info.append(
            {
                "placa": cam.get("placa") or "",
                "veiculo": cam.get("veiculo") or "",
                "fonte_nome": cam.get("fonte_nome") or "",
                "papel": cam.get("papel") or "passagem",
                "execucao": cam.get("execucao") or "",
                "percentual": round(float(cam.get("percentual") or 0), 1),
                "horario": cam.get("horario") or "",
                "cor": cam.get("cor") or "",
            }
        )
    rel = mapa.relative_to(output_dir).as_posix()
    pdf_rel = pdf.relative_to(output_dir).as_posix() if pdf else ""
    return {
        "codigo": slug,
        "setor": row.get("setor"),
        "frequencia": row.get("frequencia") or "",
        "percentual": round(float(row.get("percentual") or 0), 1),
        "status": row.get("status") or "",
        "grade_do_dia": bool(row.get("grade_do_dia")),
        "caminhoes": caminhoes_info,
        "n_caminhoes": len(caminhoes_info),
        "mapa_url": f"/arquivos/{rel}",
        "pdf_url": f"/arquivos/{pdf_rel}?v={int(datetime.now().timestamp())}" if pdf_rel else "",
        "periodo": rotulo_periodo(dia, hora_inicio, hora_fim, data_fim),
        "fontes": fontes,
    }


def incorporar_confirmados(setores: list[dict[str, Any]], output_dir: Path, dia: date) -> None:
    """Não perde rota que a Inlog/Argos/Segtrack já confirmou como coleta."""
    por_codigo = {str(s.get("codigo") or ""): s for s in setores}
    iso = dia.isoformat()
    for chave, pasta_rel, rotulo in FONTES:
        for item in _ler_rotas(output_dir / pasta_rel / iso):
            veiculo = str(item.get("veiculo") or item.get("placa") or "")
            placa = str(item.get("placa") or "")
            inlog = str(item.get("inlog") or "")
            for cob in item.get("coberturas_inlog") or []:
                execucao = str(cob.get("execucao") or cob.get("veredicto") or "")
                if execucao not in VEREDICTOS_OK:
                    continue
                codigo = str(cob.get("codigo_rota") or "")
                alvo = por_codigo.get(codigo)
                if not alvo:
                    continue
                if alvo.get("status") == "PASSAGEM" or float(alvo.get("percentual") or 0) < 22:
                    continue
                pct = float(alvo.get("percentual") or cob.get("percentual") or 0)
                atual = float(alvo.get("percentual") or 0)
                if alvo.get("status") in VEREDICTOS_OK and atual >= float(cob.get("percentual") or 0):
                    continue
                trechos = cob.get("trechos_gps") or []
                cam_atual = alvo.get("caminhao") or {}
                alvo.update(
                    {
                        "status": execucao,
                        "percentual": pct,
                        "confianca": cob.get("confianca") or alvo.get("confianca"),
                        "motivo": cob.get("motivo_execucao")
                        or f"coleta confirmada no relatório {rotulo}",
                        "caminhao": {
                            "id": _id_caminhao(veiculo, placa, inlog),
                            "veiculo": veiculo,
                            "placa": placa,
                            "inlog": inlog,
                            "fonte": chave,
                            "fonte_nome": rotulo,
                            "pontos_no_setor": cob.get("pontos_no_setor"),
                            "pontos_dentro": cam_atual.get("pontos_dentro"),
                            "km_coleta": cob.get("km_coleta"),
                            "horario": cam_atual.get("horario") or "",
                            "hora_inicio": cam_atual.get("hora_inicio") or "",
                            "hora_fim": cam_atual.get("hora_fim") or "",
                            "ponto_inicio": cam_atual.get("ponto_inicio") or [],
                            "ponto_fim": cam_atual.get("ponto_fim") or [],
                            "trechos": trechos or cam_atual.get("trechos") or [],
                            "evidencias": f"fonte {rotulo}",
                        },
                    }
                )


def caminhoes_de_rotas(
    rotas: list[dict[str, Any]],
    fonte: str = "segtrack",
    fonte_nome: str = "Segtrack",
) -> list[dict[str, Any]]:
    saida = []
    for item in rotas or []:
        pontos = item.get("pontos_dia") or item.get("pontos") or []
        if len(pontos) < 8:
            continue
        placa = str(item.get("placa") or "")
        veiculo = str(item.get("veiculo") or placa)
        inlog = str(item.get("inlog") or "").strip()
        saida.append(
            {
                "fonte": fonte,
                "fonte_nome": fonte_nome,
                "veiculo": veiculo,
                "placa": placa,
                "inlog": inlog,
                "pontos": list(pontos),
                "id": _id_caminhao(veiculo, placa, inlog),
            }
        )
    return saida


def gerar_relatorio_setores(
    pasta: Path,
    dia: date,
    caminhoes: list[dict[str, Any]],
    fontes: dict[str, str],
    hora_inicio: str = "00:00",
    hora_fim: str = "23:59",
    data_fim: date | None = None,
    on_progress: Callable[[str], None] | None = None,
    fonte: str = "unificado",
    titulo_mapa: str | None = None,
    output_dir: Path | None = None,
    incorporar: bool = False,
    turno: str | None = None,
) -> dict[str, Any]:
    def progress(msg: str) -> None:
        print(msg, flush=True)
        if on_progress:
            on_progress(msg)

    pedido = (turno or "").strip().lower()
    if pedido in {"diurno", "noturno"}:
        progress(
            "Cruzando os polígonos "
            + ("noturnos" if pedido == "noturno" else "diurnos")
            + " do dia com o GPS. A trajetória fica dentro de cada polígono..."
        )
    else:
        progress("Cruzando cada polígono oficial com os rastreios...")
    setores = analisar_setores(dia, caminhoes, on_progress=progress, turno=pedido or None)
    if incorporar and output_dir is not None:
        incorporar_confirmados(setores, output_dir, dia)
    aplicar_apoios(setores)
    for setor in setores:
        setor.pop("_geom", None)
    feitos = [s for s in setores if s["status"] in VEREDICTOS_OK]
    apoios = [s for s in setores if s["status"] == "APOIO"]
    faltando = [
        s for s in setores
        if s.get("grade_do_dia") and s["status"] not in VEREDICTOS_OK and s["status"] != "APOIO"
    ]
    na_grade = [s for s in feitos if s.get("grade_do_dia")]
    apoios_grade = [s for s in apoios if s.get("grade_do_dia")]
    recuperadas, aviso_ontem = _rotas_ontem_hoje(setores, dia, output_dir, fonte)
    pasta.mkdir(parents=True, exist_ok=True)
    _gravar_cobertura_grade(pasta, setores, dia)

    linhas = [_linha(s) for s in na_grade] + [_linha(s) for s in apoios] + [_linha(s) for s in faltando] + [
        _linha(s) for s in feitos if not s.get("grade_do_dia")
    ]
    vistos_linha = {
        achado.group(1).upper()
        for row in linhas
        if (achado := re.search(r"\b(\d{2}[A-Z]{2,4})\b", str(row.get("Setor") or ""), re.I))
    }
    for setor in recuperadas:
        codigo = str(setor.get("codigo") or "").upper()
        if codigo and codigo not in vistos_linha:
            linhas.append(_linha(setor))
            vistos_linha.add(codigo)
    vistos_mapa: set[str] = set()
    mapa_setores: list[dict[str, Any]] = []
    for setor in na_grade + apoios_grade + faltando + recuperadas:
        chave = str(setor.get("codigo") or setor.get("setor") or id(setor))
        if chave in vistos_mapa:
            continue
        vistos_mapa.add(chave)
        mapa_setores.append(setor)
    rotas_mapa = _rotas_mapa(mapa_setores)
    html = _html(
        pasta,
        dia,
        fontes,
        caminhoes,
        feitos,
        faltando,
        hora_inicio,
        hora_fim,
        data_fim,
        apoios,
        fonte=fonte,
        recuperadas=recuperadas,
        aviso_ontem=aviso_ontem,
        turno=pedido or None,
    )
    mapa = write_mapa_html(
        pasta / "mapa.html",
        titulo_mapa or f"Coleta — {rotulo_periodo(dia, hora_inicio, hora_fim, data_fim)}",
        rotas_mapa,
    )
    write_json(pasta / "relatorio-diario.json", {
        "data": dia.isoformat(),
        "data_fim": (data_fim or dia).isoformat(),
        "hora_inicio": hora_inicio,
        "hora_fim": hora_fim,
        "periodo": rotulo_periodo(dia, hora_inicio, hora_fim, data_fim),
        "fonte": fonte,
        "gerado_em": datetime.now().isoformat(timespec="seconds"),
        "total_rotas": len(feitos),
        "linhas": linhas,
        "recuperadas": [_linha(s) for s in recuperadas],
        "frota": [
            {
                "Empresa": cam.get("fonte_nome") or "—",
                "Veículo": cam.get("veiculo") or "—",
                "Placa": cam.get("placa") or "—",
                "Inlog": cam.get("inlog") or "—",
                "Pontos GPS": len(cam.get("pontos") or []),
            }
            for cam in sorted(caminhoes, key=lambda c: (c.get("fonte_nome") or "", c.get("veiculo") or ""))
        ],
    })
    write_json(pasta / "rotas.json", {"data": dia.isoformat(), "rotas": rotas_mapa})
    payload = {
        "quando": datetime.now().isoformat(timespec="seconds"),
        "data": dia.isoformat(),
        "hora_inicio": hora_inicio,
        "hora_fim": hora_fim,
        "data_fim": (data_fim or dia).isoformat(),
        "periodo": rotulo_periodo(dia, hora_inicio, hora_fim, data_fim),
        "fonte": fonte,
        "turno": pedido if pedido in {"diurno", "noturno"} else "",
        "fontes": fontes,
        "caminhoes_gps": len(caminhoes),
        "setores_grade": sum(1 for s in setores if s.get("grade_do_dia")),
        "feitas": sum(1 for s in na_grade if float(s.get("percentual") or 0) >= 80),
        "parciais": sum(1 for s in na_grade if 45 <= float(s.get("percentual") or 0) < 80),
        "apoios": len(apoios),
        "nao_feitas": len(faltando),
        "recuperadas_ontem": len(recuperadas),
        "arquivos": {
            "relatorio": str(html),
            "mapa": str(mapa),
        },
    }
    write_json(pasta / "relatorio.json", payload)
    if pedido in {"diurno", "noturno"}:
        from .pdf_cache import espelhar_turno

        espelhar_turno(pasta, pedido)
    progress(
        f"Pronto: {payload['feitas']} feitas, {payload['parciais']} parciais, "
        f"{payload['apoios']} apoios, {payload['nao_feitas']} da grade ainda sem coleta"
        + (
            f", {payload['recuperadas_ontem']} rota(s) de ontem com GPS hoje."
            if payload["recuperadas_ontem"]
            else "."
        )
    )
    return payload


def gerar_relatorio_unificado(
    output_dir: Path,
    dia: date,
    on_progress: Callable[[str], None] | None = None,
    hora_inicio: str = "00:00",
    hora_fim: str = "23:59",
    data_fim: date | None = None,
    turno: str | None = None,
) -> dict[str, Any]:
    def progress(msg: str) -> None:
        print(msg, flush=True)
        if on_progress:
            on_progress(msg)

    pedido = (turno or "").strip().lower()
    nome = {"diurno": "diurna", "noturno": "noturna"}.get(pedido, "")
    progress("5/7 — Lendo o GPS da Segtrack...")
    caminhoes, fontes = carregar_caminhoes(output_dir, dia, hora_inicio, hora_fim, data_fim)
    progress(
        f"{len(caminhoes)} caminhões com GPS · "
        + " · ".join(f"{rotulo}: {fontes[chave]}" for chave, _pasta, rotulo in FONTES)
    )
    if pedido == "noturno":
        progress("6/7 — Polígonos noturnos do dia. A trajetória fica dentro de cada polígono...")
    elif pedido == "diurno":
        progress("6/7 — Polígonos diurnos do dia. A trajetória fica dentro de cada polígono...")
    else:
        progress("6/7 — Cruzando cada polígono oficial com os rastreios...")
    pasta = output_dir / "unificado" / dia.isoformat()
    payload = gerar_relatorio_setores(
        pasta,
        dia,
        caminhoes,
        fontes,
        hora_inicio,
        hora_fim,
        data_fim,
        on_progress=on_progress,
        fonte="unificado",
        titulo_mapa=(
            f"Coleta {nome} — {rotulo_periodo(dia, hora_inicio, hora_fim, data_fim)}"
            if nome
            else f"Coleta unificada — {rotulo_periodo(dia, hora_inicio, hora_fim, data_fim)}"
        ),
        output_dir=output_dir,
        incorporar=False,
        turno=pedido or None,
    )
    progress("7/7 — Relatório pronto no painel. Use Relatório PDF ou Relatório Word se precisar do arquivo.")
    return payload


_GPS_FRESCO_S = 8 * 60
_ultimo_gps: dict[str, float] = {}


def atualizar_gps_ciclo(
    settings: Any,
    *,
    headed: bool | None = None,
    on_progress: Callable[[str], None] | None = None,
    forcar: bool = False,
) -> dict[str, Any]:
    """Nova consulta de pulsos na Segtrack (API, sem PDF)."""
    import time

    from .segtrack_rotas import executar_segtrack

    def progress(msg: str) -> None:
        print(msg, flush=True)
        if on_progress:
            on_progress(msg)

    dia = settings.data_rota
    chave = (
        f"{dia.isoformat()}|{settings.hora_inicio}|{settings.hora_fim}|"
        f"{getattr(settings, 'data_fim', None)}"
    )
    agora = time.time()
    if not forcar and agora - _ultimo_gps.get(chave, 0) < _GPS_FRESCO_S:
        progress("Pulsos recém consultados. Cruzando de novo neste polígono.")
        return {"extraidos": {}, "falhas": {}, "reaproveitado": True}

    extraidos: dict[str, dict[str, Any]] = {}
    falhas: dict[str, str] = {}
    progress(
        f"Consultando pulsos do ciclo {rotulo_periodo(dia, settings.hora_inicio, settings.hora_fim, settings.data_fim)}..."
    )
    progress("Segtrack: histórico Fulltrack por placa...")
    try:
        rel = executar_segtrack(settings, headed=headed, on_progress=on_progress, gerar_pdf=False, so_gps=True)
        extraidos["segtrack"] = {
            "veiculos": (rel or {}).get("total_veiculos"),
            "pontos": (rel or {}).get("total_pontos"),
        }
        progress(
            f"Segtrack ok: {(rel or {}).get('total_veiculos') or 0} caminhões, "
            f"{(rel or {}).get('total_pontos') or 0} pontos GPS."
        )
    except Exception as exc:
        falhas["segtrack"] = str(exc)
        progress(f"Segtrack falhou: {exc}")
    _ultimo_gps[chave] = time.time()
    return {"extraidos": extraidos, "falhas": falhas, "reaproveitado": False}


def executar_coleta_do_dia(
    settings: Any,
    *,
    extrair: bool = True,
    headed: bool | None = None,
    on_progress: Callable[[str], None] | None = None,
    turno: str | None = None,
) -> dict[str, Any]:
    """Consulta a Segtrack, puxa a trajetória e monta mapa + relatório de conclusão."""
    dia = settings.data_rota

    def progress(msg: str) -> None:
        print(msg, flush=True)
        if on_progress:
            on_progress(msg)

    extraidos: dict[str, dict[str, Any]] = {}
    falhas: dict[str, str] = {}
    if extrair:
        progress("1/7 — Coleta do dia")
        pedido = (turno or "").strip().lower()
        if pedido == "noturno":
            progress(
                "2/7 — Turno noturno, a partir das 19:00 até 05:59. "
                "Polígonos noturnos do dia; a trajetória fica dentro de cada um."
            )
        elif pedido == "diurno":
            progress(
                "2/7 — Turno diurno, 06:00 até 18:59. "
                "Polígonos diurnos do dia; a trajetória fica dentro de cada um."
            )
        else:
            progress(
                f"2/7 — Ciclo {rotulo_periodo(dia, settings.hora_inicio, settings.hora_fim, settings.data_fim)}: "
                "06:00 de um dia até 05:59 do outro."
            )
        progress("3/7 — Buscando o GPS da frota na Segtrack (placa a placa).")
        gps = atualizar_gps_ciclo(settings, headed=headed, on_progress=on_progress, forcar=True)
        extraidos = gps.get("extraidos") or {}
        falhas = gps.get("falhas") or {}
        progress("4/7 — GPS recebido. Cruzando com o mapa oficial de rotas...")
    else:
        progress(
            f"4/7 — Montando o relatório com o GPS já extraído de "
            f"{rotulo_periodo(dia, settings.hora_inicio, settings.hora_fim, settings.data_fim)}..."
        )

    payload = gerar_relatorio_unificado(
        settings.output_dir,
        dia,
        on_progress=on_progress,
        hora_inicio=getattr(settings, "hora_inicio", "00:00"),
        hora_fim=getattr(settings, "hora_fim", "23:59"),
        data_fim=getattr(settings, "data_fim", None),
        turno=turno,
    )
    payload["extraidos"] = extraidos
    payload["falhas"] = falhas
    pasta = settings.output_dir / "unificado" / dia.isoformat()
    write_json(pasta / "relatorio.json", payload)
    if (turno or "").strip().lower() in {"diurno", "noturno"}:
        from .pdf_cache import espelhar_turno

        espelhar_turno(pasta, turno)
    if falhas:
        progress("Atenção: " + " · ".join(f"{k}: {v}" for k, v in falhas.items()))
    return payload


def _rotulo_status(status: str) -> str:
    mapa = {
        "FEITA": "Concluída",
        "PARCIAL": "Concluída parcialmente",
        "NÃO FEITA": "Não iniciada",
        "SEM RASTREIO": "Sem GPS",
        "SEM COLETA": "Sem coleta",
        "PASSAGEM": "Só passagem",
        "APOIO": "Apoio",
    }
    return mapa.get((status or "").upper(), status or "—")


def _cards_conclusao(rotas: list[dict[str, Any]], vazia: str = "") -> str:
    if not rotas:
        return f"<p class='regra'>{_esc(vazia or 'Nenhuma rota da grade com coleta confirmada neste dia.')}</p>"
    blocos = []
    for setor in rotas:
        cam = setor.get("caminhao") or {}
        pct = float(setor.get("percentual") or 0)
        rotulo, classe = _rotulo_pct(pct, setor.get("status") or "")
        largura = max(2.0, min(100.0, pct))
        pts = cam.get("pontos_no_setor")
        nome = str(setor.get("setor") or "")
        codigo = str(setor.get("codigo") or "")
        if codigo and nome.startswith(codigo):
            titulo = nome
        elif codigo:
            titulo = f"{codigo} · {nome}" if nome else codigo
        else:
            titulo = nome or "Setor"
        outros = ""
        caminhoes_rota = [c for c in (setor.get("caminhoes") or setor.get("candidatos") or []) if c]
        if caminhoes_rota:
            chips = []
            for cand in caminhoes_rota:
                cor = cand.get("cor") or "#637986"
                papel = rotulo_papel_relatorio(cand)
                hora = cand.get("horario") or ""
                pct_cam = float(cand.get("percentual") or 0)
                chips.append(
                    "<span class='placa-chip'>"
                    f"<i style='background:{_esc(cor)}'></i>"
                    f"{_esc(cand.get('placa') or cand.get('veiculo') or '—')} "
                    f"<small>{_esc(papel)}"
                    f"{f' {pct_cam:.0f}%' if pct_cam else ''}"
                    f"{' · ' + _esc(hora) if hora else ''}</small></span>"
                )
            outros = "<p class='outros'>Caminhões no polígono: " + " ".join(chips) + "</p>"
        if (setor.get("status") or "").upper() == "APOIO":
            horas = (
                f"<div><dt>Horário do apoio</dt><dd>{_esc(setor.get('horario_apoio') or cam.get('horario') or '—')}</dd></div>"
                f"<div><dt>Horário da rota</dt><dd>{_esc(setor.get('horario_rota') or '—')}</dd></div>"
                f"<div><dt>Rota principal</dt><dd>{_esc(setor.get('rota_principal') or '—')}</dd></div>"
            )
        else:
            visitas = [v for v in (cam.get("passagens") or []) if v.get("hora_inicio") or v.get("hora_fim")]
            if len(visitas) > 1:
                horas = "".join(
                    f"<div><dt>Entrada {i}</dt><dd>{_esc(v.get('hora_inicio') or '—')}</dd></div>"
                    f"<div><dt>Saída {i}</dt><dd>{_esc(v.get('hora_fim') or '—')}</dd></div>"
                    for i, v in enumerate(visitas, start=1)
                )
            else:
                vis = visitas[0] if visitas else cam
                horas = (
                    f"<div><dt>Entrada na rota</dt><dd>{_esc(vis.get('hora_inicio') or '—')}</dd></div>"
                    f"<div><dt>Saída da rota</dt><dd>{_esc(vis.get('hora_fim') or '—')}</dd></div>"
                )
        blocos.append(
            "<article class='rota'>"
            "<header>"
            f"<h3>{_esc(titulo)}</h3>"
            f"<span class='pill {classe}'>{_esc(rotulo)}</span>"
            "</header>"
            f"<p class='turno'>{_esc(setor.get('frequencia') or '—')}</p>"
            f"<div class='trilha'><i class='{classe}' style='width:{largura:.0f}%'></i></div>"
            "<dl>"
            f"<div><dt>Conclusão</dt><dd class='{classe}'>{pct:.0f}%</dd></div>"
            f"<div><dt>Caminhão</dt><dd>{_esc(cam.get('veiculo') or '—')}</dd></div>"
            f"<div><dt>Placa</dt><dd>{_esc(cam.get('placa') or '—')}</dd></div>"
            f"<div><dt>Empresa GPS</dt><dd>{_esc(cam.get('fonte_nome') or '—')}</dd></div>"
            f"<div><dt>Km no polígono</dt><dd>{_esc(_fmt_num(cam.get('km_coleta'), ' km'))}</dd></div>"
            f"<div><dt>Km oficial</dt><dd>{_esc(_fmt_num(setor.get('km_oficial'), ' km'))}</dd></div>"
            f"<div><dt>Pontos GPS</dt><dd>{pts if pts not in (None, '') else '—'}</dd></div>"
            f"{horas}"
            "</dl>"
            f"<p class='motivo'>{_esc(setor.get('motivo') or '—')}</p>"
            f"{outros}"
            "</article>"
        )
    return "<div class='cartas'>" + "".join(blocos) + "</div>"


def _rotulo_pct(pct: float, status: str = "") -> tuple[str, str]:
    st = (status or "").upper()
    if st == "APOIO":
        return "Apoio", "apoio"
    if st == "PASSAGEM":
        return "Só passagem", "critico"
    if pct >= 80:
        return "Concluída", "ok"
    if pct >= 45:
        return "Concluída parcialmente", "medio"
    if pct >= 25:
        return "Início de coleta", "medio"
    if st in {"SEM RASTREIO", "SEM COLETA", "NÃO FEITA", "NAO FEITA"}:
        return "Não iniciada", "critico"
    return "Não iniciada", "critico"


def _fmt_num(valor: Any, sufixo: str = "") -> str:
    if valor in (None, "", "—"):
        return "—"
    try:
        return f"{float(valor):.1f}{sufixo}".replace(".", ",")
    except (TypeError, ValueError):
        return str(valor)


def _linha(setor: dict[str, Any]) -> dict[str, str]:
    cam = setor.get("caminhao") or {}
    pct = float(setor.get("percentual") or 0)
    status = setor.get("status") or ""
    rotulo, _cls = _rotulo_pct(pct, status)
    return {
        "Setor": setor.get("setor") or "",
        "Rota": setor.get("frequencia") or "",
        "Status": rotulo,
        "% Concluída": f"{pct:.0f}%",
        "Veículo": cam.get("veiculo") or "—",
        "Placa": cam.get("placa") or "—",
        "Caminhões na rota": _txt_caminhoes(setor) or (cam.get("placa") or "—"),
        "Empresa": cam.get("fonte_nome") or "—",
        "Km Coleta": _fmt_num(cam.get("km_coleta"), " km"),
        "Km Total": _fmt_num(setor.get("km_oficial"), " km"),
        "Pontos GPS": str(int(cam["pontos_no_setor"])) if cam.get("pontos_no_setor") not in (None, "", "—") else "—",
        "Motivo": setor.get("motivo") or "",
        "Horário": cam.get("horario") or setor.get("horario_apoio") or setor.get("horario_rota") or "",
        "Início": cam.get("hora_inicio") or "",
        "Fim": cam.get("hora_fim") or "",
        "Rota principal": setor.get("rota_principal") or "",
        "Na grade": "sim" if setor.get("grade_do_dia") else "não",
        "Confiança": setor.get("confianca") or cam.get("confianca") or "",
        "Rota de ontem": "sim" if setor.get("recuperada_ontem") else "não",
        "Fase ontem": setor.get("fase_ontem") or "",
        "% ontem": (
            f"{float(setor.get('percentual_ontem')):.0f}%"
            if setor.get("percentual_ontem") not in (None, "")
            else ""
        ),
    }


def _caminhao_operacao(setor: dict[str, Any], papel: str) -> dict[str, Any]:
    alvo = (papel or "").lower()
    for item in setor.get("caminhoes") or []:
        if str(item.get("papel") or "").lower() == alvo:
            return item
    if alvo == "coleta" and str(setor.get("status") or "") in VEREDICTOS_OK:
        return setor.get("caminhao") or {}
    if alvo == "apoio" and str(setor.get("status") or "") == "APOIO":
        return setor.get("caminhao") or {}
    return {}


def _trechos_da_passagem(cam: dict[str, Any], aneis: list, clip_m: float) -> list:
    """A passagem já vai da entrada à saída. Não recorta de novo no meio."""
    prontos = [t for t in (cam.get("trechos") or []) if t and len(t) >= 2]
    if cam.get("passagens") and prontos:
        return prontos
    return recortar_trechos_ao_setor(prontos, aneis, buffer_m=clip_m)


def _rotas_mapa(feitos: list[dict[str, Any]], *, clip_m: float = CLIP_MAPA_M) -> list[dict[str, Any]]:
    rotas = []
    for setor in feitos:
        cam = _caminhao_operacao(setor, "coleta") or setor.get("caminhao") or {}
        trechos = _trechos_da_passagem(cam, setor.get("poligonos") or [], clip_m)
        lacunas: list = []
        caminhoes_mapa = []
        for item in setor.get("caminhoes") or []:
            rec_cam = _trechos_da_passagem(item, setor.get("poligonos") or [], clip_m)
            caminhoes_mapa.append(
                {
                    **{k: v for k, v in item.items() if k not in {"trechos", "lacunas"}},
                    "trechos": rec_cam,
                    "lacunas": [],
                }
            )
        cob = {
            "codigo_rota": setor.get("codigo"),
            "setor": setor.get("setor"),
            "rota": setor.get("frequencia"),
            "percentual": setor.get("percentual"),
            "veredicto": setor.get("status"),
            "execucao": setor.get("status"),
            "confianca": setor.get("confianca"),
            "km_coleta": cam.get("km_coleta"),
            "km_total": setor.get("km_oficial"),
            "pontos_no_setor": cam.get("pontos_no_setor"),
            "poligonos": setor.get("poligonos") or [],
            "vias": setor.get("vias") or [],
            "trechos_gps": trechos,
            "lacunas": lacunas,
            "motivo_execucao": setor.get("motivo"),
            "horario": cam.get("horario") or setor.get("horario_apoio") or "",
            "horario_rota": setor.get("horario_rota") or "",
            "horario_apoio": setor.get("horario_apoio") or "",
            "hora_inicio": cam.get("hora_inicio") or "",
            "hora_fim": cam.get("hora_fim") or "",
            "ponto_inicio": cam.get("ponto_inicio") or [],
            "ponto_fim": cam.get("ponto_fim") or [],
            "rota_principal": setor.get("rota_principal") or "",
            "caminhoes": caminhoes_mapa,
        }
        pct = float(setor.get("percentual") or 0)
        rotulo, _cls = _rotulo_pct(pct, setor.get("status") or "")
        rotas.append(
            {
                "veiculo": cam.get("veiculo"),
                "placa": cam.get("placa"),
                "fonte": cam.get("fonte"),
                "fonte_nome": cam.get("fonte_nome"),
                "inlog": cam.get("inlog"),
                "setor": setor.get("setor"),
                "rota": setor.get("frequencia"),
                "codigo_rota": setor.get("codigo"),
                "percentual": pct,
                "percentual_concluido": pct,
                "veredicto": rotulo,
                "execucao": setor.get("status"),
                "confianca": setor.get("confianca"),
                "km_coleta": cam.get("km_coleta"),
                "km_total": setor.get("km_oficial"),
                "horario": cam.get("horario") or setor.get("horario_apoio") or "",
                "horario_rota": setor.get("horario_rota") or "",
                "horario_apoio": setor.get("horario_apoio") or "",
                "hora_inicio": cam.get("hora_inicio") or "",
                "hora_fim": cam.get("hora_fim") or "",
                "ponto_inicio": cam.get("ponto_inicio") or [],
                "ponto_fim": cam.get("ponto_fim") or [],
                "rota_principal": setor.get("rota_principal") or "",
                "trechos": trechos,
                "lacunas": lacunas,
                "pontos": [],
                "pontos_dia": [],
                "poligonos": setor.get("poligonos") or [],
                "vias": setor.get("vias") or [],
                "na_grade": bool(setor.get("grade_do_dia")),
                "ontem": bool(setor.get("recuperada_ontem")),
                "fase_ontem": setor.get("fase_ontem") or "",
                "caminhoes": caminhoes_mapa,
                "coberturas_inlog": [cob],
            }
        )
    return rotas


def _html(
    pasta: Path,
    dia: date,
    fontes: dict[str, str],
    caminhoes: list[dict[str, Any]],
    feitos: list[dict[str, Any]],
    faltando: list[dict[str, Any]],
    hora_inicio: str = "00:00",
    hora_fim: str = "23:59",
    data_fim: date | None = None,
    apoios: list[dict[str, Any]] | None = None,
    fonte: str = "unificado",
    recuperadas: list[dict[str, Any]] | None = None,
    aviso_ontem: str = "",
    turno: str | None = None,
) -> Path:
    periodo = rotulo_dia_periodo(dia, hora_inicio, hora_fim, data_fim)
    pedido = (turno or "").strip().lower()
    if (fonte or "").lower() == "segtrack":
        titulo = f"Coleta Segtrack — {periodo}"
        intro = "Grade do mapa oficial. Só rota e % — mapas e trajetórias no PDF."
    elif pedido == "noturno":
        titulo = f"Coleta noturna — {periodo}"
        intro = "Polígonos noturnos do dia, a partir das 19:00. A trajetória do GPS fica dentro de cada polígono."
    elif pedido == "diurno":
        titulo = f"Coleta diurna — {periodo}"
        intro = "Polígonos diurnos do dia, das 06:00 às 18:59. A trajetória do GPS fica dentro de cada polígono."
    else:
        titulo = f"Coleta do dia — {periodo}"
        intro = "GPS da Segtrack. Só rota e % — mapas e trajetórias no PDF."
    avisos = []
    _ = caminhoes
    if aviso_ontem:
        avisos.append(aviso_ontem)
    if (fonte or "").lower() == "unificado":
        for chave, _pasta, rotulo in FONTES:
            if fontes.get(chave) == "sem extração":
                avisos.append(
                    f"{rotulo} sem extração neste dia. Extraia essa frota na aba correspondente e monte o relatório de novo."
                )
    na_grade = [s for s in feitos if s.get("grade_do_dia")]
    apoios_grade = [s for s in (apoios or []) if s.get("grade_do_dia")]
    vistos: set[str] = set()
    grade: list[dict[str, Any]] = []
    for setor in na_grade + apoios_grade + faltando:
        chave = str(setor.get("codigo") or setor.get("setor") or id(setor))
        if chave in vistos:
            continue
        vistos.add(chave)
        grade.append(setor)
    blocos = []
    for setor in sorted(grade, key=lambda s: (-float(s.get("percentual") or 0), str(s.get("codigo") or ""))):
        pct = float(setor.get("percentual") or 0)
        _rotulo, classe = _rotulo_pct(pct, setor.get("status") or "")
        if classe == "medio":
            classe = "mid"
        elif classe == "critico":
            classe = "no"
        codigo = str(setor.get("codigo") or "")
        nome = str(setor.get("setor") or "")
        if codigo and nome.startswith(codigo):
            label = nome
        elif codigo and nome:
            label = f"{codigo} — {nome}"
        else:
            label = codigo or nome or "Rota"
        blocos.append(
            f"<div class='barra'><span class='nome'>{_esc(label)}</span>"
            f"<b class='{classe}'>{pct:.0f}%</b></div>"
        )
    barras = "".join(blocos)
    recuperadas_html = ""
    if recuperadas:
        itens = []
        for setor in sorted(recuperadas, key=lambda s: (-float(s.get("percentual") or 0), str(s.get("codigo") or ""))):
            pct = float(setor.get("percentual") or 0)
            _rotulo, classe = _rotulo_pct(pct, setor.get("status") or "")
            if classe == "medio":
                classe = "mid"
            elif classe == "critico":
                classe = "no"
            codigo = str(setor.get("codigo") or "")
            nome = str(setor.get("setor") or "")
            if codigo and nome.startswith(codigo):
                label = nome
            elif codigo and nome:
                label = f"{codigo} — {nome}"
            else:
                label = codigo or nome or "Rota"
            itens.append(
                f"<div class='barra'><span class='nome'>{_esc(label)} "
                f"<small><span class='tag'>dia anterior</span>GPS hoje</small></span>"
                f"<b class='{classe}'>{pct:.0f}%</b></div>"
            )
        recuperadas_html = (
            "<div class='bloco-ontem'>"
            "<h2>Ontem não feitas · GPS hoje</h2>"
            "<p class='sub'>Grade de ontem incompleta, só se o rastreador entrou no polígono hoje. O % é o de hoje.</p>"
            "<div class='resumo-grade'>" + "".join(itens) + "</div>"
            "</div>"
        )
    avisos_html = "".join(f"<p class='aviso-ontem'>{_esc(a)}</p>" for a in avisos)
    tpl = Path(__file__).resolve().parent / "templates" / "relatorio"
    css = (tpl / "relatorio.css").read_text(encoding="utf-8")
    css += """
      @page { size: auto; margin: 0; }
      html, body { background: #fff; }
      .page { width: 100%; min-height: 0; height: auto; }
      .folha { height: auto; overflow: visible; }
      .body { padding: 14px 18px 24px; }
      .barra .nome { font-size: 11px; }
      .barra b { font-size: 14px; }
    """
    nome_fonte = {
        "argos": "ARGOS",
        "segtrack": "SEGTRACK",
    }.get((fonte or "").lower(), "INLOG · ARGOS · SEGTRACK")
    dia_br = rotulo_periodo(dia, hora_inicio, hora_fim, data_fim)
    header = (tpl / "header.html").read_text(encoding="utf-8")
    header = header.replace("{{FONTE}}", _esc(nome_fonte)).replace("{{DIA}}", _esc(dia_br))
    capa = (tpl / "capa.html").read_text(encoding="utf-8")
    capa = (
        capa.replace("{{HEADER}}", header)
        .replace("{{TITULO}}", "Rotas do dia")
        .replace("{{DIA}}", _esc(dia_br))
        .replace("{{FONTE}}", _esc(nome_fonte))
        .replace("{{BARRAS}}", barras)
        .replace("{{RECUPERADAS}}", recuperadas_html + avisos_html)
        .replace(
            "{{GERADO}}",
            datetime.now().strftime("%d/%m/%Y %H:%M")
            + (f" · {_nome_pacote()}" if _nome_pacote() else ""),
        )
    )
    html = (
        "<!DOCTYPE html><html lang='pt-BR'><head><meta charset='utf-8' />"
        f"<title>Rotas do dia — {_esc(dia_br)}</title><style>{css}</style></head>"
        f"<body>{capa}</body></html>"
    )
    alvo = pasta / "relatorio-diario.html"
    alvo.write_text(html, encoding="utf-8")
    extra = pasta / ("relatorio-segtrack.html" if (fonte or "").lower() == "segtrack" else "relatorio-unificado.html")
    extra.write_text(html, encoding="utf-8")
    return alvo
