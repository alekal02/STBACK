from __future__ import annotations

import hmac
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .config import ROOT, load_settings

WEB_DIR = Path(__file__).resolve().parent / "web"
mimetypes.add_type(
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".docx",
)

WORD_JOB: dict[str, Any] = {
    "lock": threading.Lock(),
    "rodando": False,
    "data": "",
    "erro": "",
    "cancelado": False,
}
PDF_JOB: dict[str, Any] = {
    "lock": threading.Lock(),
    "rodando": False,
    "data": "",
    "turno": "",
    "chave": "",
    "erro": "",
    "cancelado": False,
}


def _estimar_progresso(texto: str, atual: int) -> int:
    t = (texto or "").lower()
    pares = re.search(r"(\d+)\s*/\s*(\d+)", texto or "")
    if pares and int(pares.group(2)) > 0 and "mapa oficial" in t:
        feito = int(pares.group(1)) / int(pares.group(2))
        return min(94, max(atual, int(48 + 40 * feito)))
    regras = (
        ("parado pelo", 0),
        ("concluído", 100),
        ("relatório no painel", 96),
        ("relatório pronto", 96),
        ("montando o relatório", 88),
        ("cruzando", 48),
        ("segtrack ok", 46),
        ("argos ok", 44),
        ("inlog ok", 42),
        ("integrationcoleta", 18),
        ("api de rastreio", 28),
        ("sem abrir o site", 40),
        ("login simultâneo", 10),
        ("entrando nas três", 10),
        ("segtrack:", 36),
        ("argos:", 26),
        ("inlog:", 16),
        ("ciclo ", 6),
        ("coleta do dia", 4),
        ("iniciando", 3),
        ("fila:", 2),
    )
    for chave, valor in regras:
        if chave in t:
            return valor if valor in (0, 100) else max(atual, valor)
    return atual


class JobState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.rodando = False
        self.nome = ""
        self.log = ""
        self.logs: list[str] = []
        self.message = "Pronto para começar."
        self.progress = 0
        self.erro = ""
        self.resultado: dict[str, Any] | None = None
        self.proc: subprocess.Popen[str] | None = None
        self.extras: list[subprocess.Popen[str]] = []
        self.cancelado = False

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {
                "rodando": self.rodando,
                "nome": self.nome,
                "log": self.log,
                "logs": list(self.logs),
                "message": self.message,
                "progress": self.progress,
                "erro": self.erro,
                "resultado": self.resultado,
                "cancelado": self.cancelado,
            }

    def reset(self, nome: str) -> None:
        self.rodando = True
        self.nome = nome
        self.logs = []
        self.log = ""
        self.message = f"Iniciando {nome}..."
        self.progress = 2
        self.erro = ""
        self.resultado = None
        self.proc = None
        self.cancelado = False

    def append(self, texto: str) -> None:
        if not texto:
            return
        with self.lock:
            hora = datetime.now().strftime("%H:%M:%S")
            linha = f"{hora}  {texto}"
            self.logs.append(linha)
            if len(self.logs) > 250:
                self.logs = self.logs[-250:]
            self.log = "\n".join(self.logs)
            self.message = texto
            self.progress = _estimar_progresso(texto, self.progress)


JOB = JobState()
CHATBOT_ENVIO: dict[str, Any] = {
    "lock": threading.Lock(),
    "grupo": "",
    "dia": "",
    "forcar": False,
    "msg": "",
    "erro": False,
}


def marcar_envio_chatbot(grupo: str, dia: str | None = None) -> None:
    """Garante envio do PDF quando o relatório (este ou o que já está rodando) terminar."""
    with CHATBOT_ENVIO["lock"]:
        CHATBOT_ENVIO["grupo"] = (grupo or "").strip()
        CHATBOT_ENVIO["dia"] = (dia or "").strip()
        CHATBOT_ENVIO["forcar"] = True
        CHATBOT_ENVIO["msg"] = "Pedido recebido. Aviso no grupo e, em seguida, o PDF."
        CHATBOT_ENVIO["erro"] = False


def registrar_retorno_chatbot(texto: str, *, erro: bool = False) -> None:
    with CHATBOT_ENVIO["lock"]:
        CHATBOT_ENVIO["msg"] = (texto or "").strip()
        CHATBOT_ENVIO["erro"] = bool(erro)


def chatbot_envio_publico() -> dict[str, Any]:
    with CHATBOT_ENVIO["lock"]:
        return {
            "msg": str(CHATBOT_ENVIO.get("msg") or ""),
            "erro": bool(CHATBOT_ENVIO.get("erro")),
            "pendente": bool(CHATBOT_ENVIO.get("forcar")),
        }


def _matar_arvore(proc: subprocess.Popen[str] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
            capture_output=True,
            text=True,
            check=False,
        )
        return
    proc.terminate()
    try:
        proc.wait(timeout=4)
    except subprocess.TimeoutExpired:
        proc.kill()


def cancelar_execucao() -> dict[str, Any]:
    with WORD_JOB["lock"]:
        WORD_JOB["cancelado"] = True
        word_ativa = bool(WORD_JOB["rodando"])
    with PDF_JOB["lock"]:
        PDF_JOB["cancelado"] = True
        pdf_ativa = bool(PDF_JOB["rodando"])
    with JOB.lock:
        ativos = JOB.rodando or bool(JOB.proc) or bool(JOB.extras) or word_ativa or pdf_ativa
        if not ativos:
            return {"ok": True, "msg": "Nada em execução."}
        JOB.cancelado = True
        procs = ([JOB.proc] if JOB.proc else []) + list(JOB.extras)
    for proc in procs:
        _matar_arvore(proc)
    JOB.append("Parado pelo usuário.")
    return {"ok": True, "msg": "Parando a geração..."}


def _rodar_rastreado(cmd: list[str]) -> str:
    env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    with JOB.lock:
        JOB.extras.append(proc)
    try:
        saida, _ = proc.communicate()
    finally:
        with JOB.lock:
            if proc in JOB.extras:
                JOB.extras.remove(proc)
    if JOB.cancelado:
        raise RuntimeError("Geração interrompida.")
    if proc.returncode != 0:
        texto = (saida or "").strip()
        if texto:
            print(texto[-4000:], flush=True)
        linhas = [ln.strip() for ln in texto.splitlines() if ln.strip()]
        resumo = ""
        for ln in reversed(linhas):
            if ln.startswith("RuntimeError") or ln.startswith("Error") or "Executable doesn't exist" in ln:
                resumo = ln.split("RuntimeError:", 1)[-1].strip()
                break
        if not resumo and linhas:
            resumo = linhas[-1][:500]
        raise RuntimeError(resumo or "Falha ao gerar o arquivo.")
    return saida or ""


def _dias_argos(output_dir: Path) -> list[str]:
    base = output_dir / "argos"
    if not base.exists():
        return []
    dias = []
    for pasta in base.iterdir():
        if pasta.is_dir() and (pasta / "relatorio.json").exists():
            dias.append(pasta.name)
    return sorted(dias, reverse=True)


def _dias_disponiveis(output_dir: Path) -> list[str]:
    base = output_dir / "rotas"
    if not base.exists():
        return []
    dias = []
    for pasta in base.iterdir():
        if pasta.is_dir() and (pasta / "relatorio-diario.html").exists():
            dias.append(pasta.name)
    return sorted(dias, reverse=True)


def _dias_segtrack(output_dir: Path) -> list[str]:
    base = output_dir / "segtrack"
    if not base.exists():
        return []
    dias = []
    for pasta in base.iterdir():
        if pasta.is_dir() and (pasta / "relatorio.json").exists():
            dias.append(pasta.name)
    return sorted(dias, reverse=True)


def _dias_unificado(output_dir: Path) -> list[str]:
    dias = set(_dias_disponiveis(output_dir) + _dias_argos(output_dir) + _dias_segtrack(output_dir))
    base = output_dir / "unificado"
    if base.exists():
        for pasta in base.iterdir():
            if pasta.is_dir() and (pasta / "relatorio-diario.html").exists():
                dias.add(pasta.name)
    return sorted(dias, reverse=True)


def _pasta_unificado(output_dir: Path, data: str | None = None) -> Path | None:
    base = output_dir / "unificado"
    if data:
        alvo = base / data
        return alvo if alvo.is_dir() else None
    if not base.exists():
        return None
    pastas = [p for p in base.iterdir() if p.is_dir() and (p / "relatorio-diario.html").exists()]
    return max(pastas, key=lambda p: p.name) if pastas else None


def _pasta_segtrack(output_dir: Path, data: str | None = None) -> Path | None:
    base = output_dir / "segtrack"
    if data:
        alvo = base / data
        return alvo if alvo.is_dir() else None
    if not base.exists():
        return None
    pastas = [p for p in base.iterdir() if p.is_dir()]
    if not pastas:
        return None
    return max(pastas, key=lambda p: p.stat().st_mtime)


def _pasta_argos(output_dir: Path, data: str | None = None) -> Path | None:
    base = output_dir / "argos"
    if data:
        alvo = base / data
        return alvo if alvo.is_dir() else None
    if not base.exists():
        return None
    pastas = [p for p in base.iterdir() if p.is_dir()]
    if not pastas:
        return None
    return max(pastas, key=lambda p: p.stat().st_mtime)


def _pasta_rotas(output_dir: Path, data: str | None = None) -> Path | None:
    base = output_dir / "rotas"
    if data:
        alvo = base / data
        if alvo.is_dir():
            return alvo
        return None
    if not base.exists():
        return None
    pastas = [p for p in base.iterdir() if p.is_dir()]
    if not pastas:
        return None
    return max(pastas, key=lambda p: p.stat().st_mtime)


def _nomes_ccl(pasta: Path | None) -> list[str]:
    if not pasta or not (pasta / "frota.json").is_file():
        return []
    try:
        itens = json.loads((pasta / "frota.json").read_text(encoding="utf-8"))
    except Exception:
        return []
    nomes = []
    for item in itens if isinstance(itens, list) else []:
        ident = str(item.get("Identificador") or item.get("veiculo") or "").strip()
        if ident:
            nomes.append(ident.replace("\r", "").replace("\n", ""))
    return nomes


def _arquivos_publicos(output_dir: Path, pasta: Path | None) -> list[dict[str, str]]:
    if not pasta:
        return []
    itens = []
    for arquivo in sorted(pasta.iterdir()):
        if arquivo.is_file():
            rel = arquivo.relative_to(output_dir).as_posix()
            itens.append({"nome": arquivo.name, "url": f"/arquivos/{rel}"})
    return itens


def _rotas_painel(pasta: Path | None) -> list[dict[str, Any]]:
    if not pasta:
        return []
    arq = pasta / "relatorio-diario.json"
    if not arq.is_file():
        return []
    try:
        dados = json.loads(arq.read_text(encoding="utf-8"))
    except Exception:
        return []
    saida: list[dict[str, Any]] = []
    for row in dados.get("linhas") or []:
        setor = str(row.get("Setor") or "")
        achado = re.search(r"\b(\d{2}[A-Z]{2,4})\b", setor, re.I)
        saida.append(
            {
                "codigo": achado.group(1).upper() if achado else "",
                "setor": setor,
                "status": str(row.get("Status") or ""),
                "percentual": str(row.get("% Concluída") or ""),
                "placa": str(row.get("Placa") or ""),
                "horario": str(row.get("Horário") or ""),
            }
        )
    return saida


def status_payload(
    data: str | None = None,
    data_argos: str | None = None,
    data_segtrack: str | None = None,
) -> dict[str, Any]:
    settings = load_settings()
    pasta = _pasta_rotas(settings.output_dir, data)
    mapa = None
    mapa_rev = None
    diario = None
    diario_rev = None
    pdf = None
    relatorio_salvo: dict[str, Any] = {}
    if pasta and (pasta / "mapa.html").exists():
        mapa_arq = pasta / "mapa.html"
        rel = mapa_arq.relative_to(settings.output_dir).as_posix()
        mapa = f"/arquivos/{rel}"
        mapa_rev = str(int(mapa_arq.stat().st_mtime))
    if pasta and (pasta / "relatorio-diario.html").exists():
        diario_arq = pasta / "relatorio-diario.html"
        diario = f"/arquivos/{diario_arq.relative_to(settings.output_dir).as_posix()}"
        diario_rev = str(int(diario_arq.stat().st_mtime))
    if pasta and (pasta / "relatorio-rotas.pdf").exists():
        pdf = f"/arquivos/{(pasta / 'relatorio-rotas.pdf').relative_to(settings.output_dir).as_posix()}"
    if pasta and (pasta / "relatorio.json").exists():
        try:
            relatorio_salvo = json.loads((pasta / "relatorio.json").read_text(encoding="utf-8"))
        except Exception:
            relatorio_salvo = {}
    pasta_argos = _pasta_argos(settings.output_dir, data_argos or data)
    argos_rel: dict[str, Any] = {}
    argos_mapa = None
    argos_mapa_rev = None
    argos_diario = None
    argos_diario_rev = None
    if pasta_argos and (pasta_argos / "relatorio.json").exists():
        try:
            argos_rel = json.loads((pasta_argos / "relatorio.json").read_text(encoding="utf-8"))
        except Exception:
            argos_rel = {}
    if pasta_argos and (pasta_argos / "mapa.html").exists():
        am = pasta_argos / "mapa.html"
        argos_mapa = f"/arquivos/{am.relative_to(settings.output_dir).as_posix()}"
        argos_mapa_rev = str(int(am.stat().st_mtime))
    diario_argos = None
    if pasta_argos and (pasta_argos / "relatorio-diario.html").exists():
        diario_argos = pasta_argos / "relatorio-diario.html"
    elif pasta_argos and (pasta_argos / "relatorio-argos.html").exists():
        diario_argos = pasta_argos / "relatorio-argos.html"
    if diario_argos is not None:
        argos_diario = f"/arquivos/{diario_argos.relative_to(settings.output_dir).as_posix()}"
        argos_diario_rev = str(int(diario_argos.stat().st_mtime))
    pasta_seg = _pasta_segtrack(settings.output_dir, data_segtrack or data)
    seg_rel: dict[str, Any] = {}
    seg_mapa = None
    seg_mapa_rev = None
    seg_diario = None
    seg_diario_rev = None
    if pasta_seg and (pasta_seg / "relatorio.json").exists():
        try:
            seg_rel = json.loads((pasta_seg / "relatorio.json").read_text(encoding="utf-8"))
        except Exception:
            seg_rel = {}
    if pasta_seg and (pasta_seg / "mapa.html").exists():
        sm = pasta_seg / "mapa.html"
        seg_mapa = f"/arquivos/{sm.relative_to(settings.output_dir).as_posix()}"
        seg_mapa_rev = str(int(sm.stat().st_mtime))
    diario_seg = None
    if pasta_seg and (pasta_seg / "relatorio-diario.html").exists():
        diario_seg = pasta_seg / "relatorio-diario.html"
    elif pasta_seg and (pasta_seg / "relatorio-segtrack.html").exists():
        diario_seg = pasta_seg / "relatorio-segtrack.html"
    if diario_seg is not None:
        seg_diario = f"/arquivos/{diario_seg.relative_to(settings.output_dir).as_posix()}"
        seg_diario_rev = str(int(diario_seg.stat().st_mtime))
    pasta_uni = _pasta_unificado(settings.output_dir, data)
    uni_rel: dict[str, Any] = {}
    uni_mapa = None
    uni_mapa_rev = None
    uni_diario = None
    uni_diario_rev = None
    uni_pdf = None
    uni_word = None
    if pasta_uni and (pasta_uni / "relatorio.json").exists():
        try:
            uni_rel = json.loads((pasta_uni / "relatorio.json").read_text(encoding="utf-8"))
        except Exception:
            uni_rel = {}
    if pasta_uni and (pasta_uni / "mapa.html").exists():
        um = pasta_uni / "mapa.html"
        uni_mapa = f"/arquivos/{um.relative_to(settings.output_dir).as_posix()}"
        uni_mapa_rev = str(int(um.stat().st_mtime))
    if pasta_uni and (pasta_uni / "relatorio-diario.html").exists():
        ud = pasta_uni / "relatorio-diario.html"
        uni_diario = f"/arquivos/{ud.relative_to(settings.output_dir).as_posix()}"
        uni_diario_rev = str(int(ud.stat().st_mtime))
    if pasta_uni and (pasta_uni / "relatorio-rotas.pdf").exists():
        uni_pdf = f"/arquivos/{(pasta_uni / 'relatorio-rotas.pdf').relative_to(settings.output_dir).as_posix()}"
    if pasta_uni and (pasta_uni / "relatorio-rotas.docx").exists():
        uni_word = f"/arquivos/{(pasta_uni / 'relatorio-rotas.docx').relative_to(settings.output_dir).as_posix()}"
    resultado = relatorio_salvo
    job_res = JOB.resultado or {}
    if not resultado and (not data or job_res.get("data") == data) and job_res.get("fonte") not in {"argos", "segtrack", "unificado"}:
        resultado = job_res
    return {
        "usuario": settings.username,
        "argos_usuario": settings.argos_user,
        "segtrack_usuario": settings.segtrack_user,
        "filtro_servico": settings.filtro_servico,
        "data_rota": data or settings.data_rota.isoformat(),
        "ultima_rota": pasta.name if pasta else None,
        "tem_coleta": bool(pasta),
        "dias_disponiveis": _dias_disponiveis(settings.output_dir),
        "mapa_url": mapa,
        "mapa_rev": mapa_rev,
        "diario_url": diario,
        "diario_rev": diario_rev,
        "pdf_url": pdf,
        "arquivos": _arquivos_publicos(settings.output_dir, pasta),
        "total_veiculos": resultado.get("total_veiculos") or resultado.get("total_compactadores"),
        "percentual_medio": resultado.get("percentual_medio"),
        "filtro_veiculos": ", ".join(settings.filtro_veiculos) or "CCL",
        "frota_ccl": _nomes_ccl(pasta),
        "argos_tem_coleta": bool(pasta_argos and (pasta_argos / "relatorio.json").exists()),
        "argos_obra": argos_rel.get("obra"),
        "argos_veiculos": argos_rel.get("total_veiculos"),
        "argos_pontos": argos_rel.get("total_pontos"),
        "argos_cruzados": argos_rel.get("cruzados_inlog"),
        "argos_mapa_url": argos_mapa,
        "argos_mapa_rev": argos_mapa_rev,
        "argos_diario_url": argos_diario,
        "argos_diario_rev": argos_diario_rev,
        "argos_dias_disponiveis": _dias_argos(settings.output_dir),
        "argos_arquivos": _arquivos_publicos(settings.output_dir, pasta_argos),
        "argos_placas": ", ".join(settings.argos_veiculos),
        "segtrack_tem_coleta": bool(pasta_seg and (pasta_seg / "relatorio.json").exists()),
        "segtrack_obra": seg_rel.get("obra"),
        "segtrack_veiculos": seg_rel.get("total_veiculos"),
        "segtrack_pontos": seg_rel.get("total_pontos"),
        "segtrack_mapa_url": seg_mapa,
        "segtrack_mapa_rev": seg_mapa_rev,
        "segtrack_diario_url": seg_diario,
        "segtrack_diario_rev": seg_diario_rev,
        "segtrack_dias_disponiveis": _dias_segtrack(settings.output_dir),
        "segtrack_arquivos": _arquivos_publicos(settings.output_dir, pasta_seg),
        "segtrack_placas": ", ".join(settings.segtrack_veiculos) or "compactadores em Aparecida",
        "unificado_tem_coleta": bool(pasta_uni and (pasta_uni / "relatorio-diario.html").exists()),
        "unificado_mapa_url": uni_mapa,
        "unificado_mapa_rev": uni_mapa_rev,
        "unificado_diario_url": uni_diario,
        "unificado_diario_rev": uni_diario_rev,
        "unificado_pdf_url": uni_pdf,
        "unificado_word_url": uni_word,
        "unificado_dias_disponiveis": _dias_unificado(settings.output_dir),
        "unificado_arquivos": _arquivos_publicos(settings.output_dir, pasta_uni),
        "unificado_feitas": uni_rel.get("feitas"),
        "unificado_parciais": uni_rel.get("parciais"),
        "unificado_apoios": uni_rel.get("apoios"),
        "unificado_nao_feitas": uni_rel.get("nao_feitas"),
        "unificado_fontes": uni_rel.get("fontes") or {},
        "unificado_rotas": _rotas_painel(pasta_uni),
        "job": JOB.snapshot(),
        "chatbot_envio": chatbot_envio_publico(),
    }


def _ler_relatorio(data: str | None) -> dict[str, Any] | None:
    pasta = _pasta_rotas(load_settings().output_dir, data)
    if not pasta or not (pasta / "relatorio.json").exists():
        return None
    try:
        return json.loads((pasta / "relatorio.json").read_text(encoding="utf-8"))
    except Exception:
        return None


def _executar_job(nome: str, payload: dict[str, Any]) -> None:
    data = str(payload.get("data") or "").strip() or None
    hora_inicio = str(payload.get("hora_inicio") or "").strip() or None
    hora_fim = str(payload.get("hora_fim") or "").strip() or None
    data_fim = str(payload.get("data_fim") or "").strip() or None
    veiculos = str(payload.get("veiculos") or "").strip() or None
    try:
        if nome == "inspecionar":
            _executar_inspecao_job(payload)
            JOB.append("Concluído.")
            return
        cmd = [sys.executable, "-u", "-m", "inlog_agent", nome]
        if nome in {"rotas", "argos-rotas", "segtrack-rotas", "unificado"}:
            if data:
                cmd.extend(["--data", data])
            if hora_inicio:
                cmd.extend(["--hora-inicio", hora_inicio])
            if hora_fim:
                cmd.extend(["--hora-fim", hora_fim])
            if data_fim:
                cmd.extend(["--data-fim", data_fim])
            if veiculos and nome == "rotas":
                cmd.extend(["--veiculos", veiculos])
            if nome == "unificado":
                turno = str(payload.get("turno") or "").strip().lower()
                if turno in {"diurno", "noturno"}:
                    cmd.extend(["--turno", turno])
                if payload.get("extrair") is False or payload.get("so_relatorio"):
                    cmd.append("--so-relatorio")
                else:
                    cmd.append("--extrair")
        janela = ""
        if data:
            janela = f" ({data}"
            if hora_inicio or hora_fim or data_fim:
                janela += f" {hora_inicio or '00:00'}→{data_fim or data} {hora_fim or '23:59'}"
            janela += ")"
        JOB.append(f"Iniciando {nome}{janela}...")
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"}
        proc = subprocess.Popen(
            cmd,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        with JOB.lock:
            JOB.proc = proc
        assert proc.stdout is not None
        for line in proc.stdout:
            JOB.append(line.rstrip())
        codigo = proc.wait()
        with JOB.lock:
            JOB.proc = None
            cancelado = JOB.cancelado
        if cancelado:
            JOB.append("Parado pelo usuário.")
            return
        if codigo != 0:
            raise RuntimeError(f"O processo {nome} terminou com código {codigo}.")
        if nome == "rotas":
            JOB.resultado = _ler_relatorio(data)
        elif nome == "argos-rotas":
            pasta = _pasta_argos(load_settings().output_dir, data)
            if pasta and (pasta / "relatorio.json").exists():
                JOB.resultado = json.loads((pasta / "relatorio.json").read_text(encoding="utf-8"))
            else:
                JOB.resultado = {"ok": True, "fonte": "argos"}
        elif nome == "segtrack-rotas":
            pasta = _pasta_segtrack(load_settings().output_dir, data)
            if pasta and (pasta / "relatorio.json").exists():
                JOB.resultado = json.loads((pasta / "relatorio.json").read_text(encoding="utf-8"))
            else:
                JOB.resultado = {"ok": True, "fonte": "segtrack"}
        elif nome == "unificado":
            pasta = _pasta_unificado(load_settings().output_dir, data)
            if pasta and (pasta / "relatorio.json").exists():
                JOB.resultado = json.loads((pasta / "relatorio.json").read_text(encoding="utf-8"))
            else:
                JOB.resultado = {"ok": True, "fonte": "unificado"}
        else:
            JOB.resultado = {"ok": True}
        JOB.append("Concluído.")
        if nome == "unificado":
            _enviar_chatbot_apos_unificado(data, payload)
    except Exception as exc:
        if JOB.cancelado:
            JOB.append("Parado pelo usuário.")
        else:
            JOB.erro = str(exc)
            JOB.append(f"Erro: {exc}")
    finally:
        with JOB.lock:
            JOB.rodando = False
            JOB.proc = None


def _garantir_pdf(data: str) -> Path:
    settings = load_settings()
    pasta = _pasta_rotas(settings.output_dir, data)
    if not pasta:
        raise FileNotFoundError("Não há extração deste dia. Extraia as rotas antes de baixar o PDF.")
    alvo = pasta / "relatorio-rotas.pdf"
    mapa = pasta / "mapa.html"
    diario = pasta / "relatorio-diario.json"
    if not mapa.is_file() or not diario.is_file():
        raise FileNotFoundError("Faltam o mapa ou o relatório deste dia.")
    if alvo.is_file():
        mais_novo = max(mapa.stat().st_mtime, diario.stat().st_mtime)
        if alvo.stat().st_mtime >= mais_novo:
            return alvo
    _rodar_rastreado([sys.executable, "-u", "-m", "inlog_agent", "pdf", "--data", data])
    if not alvo.is_file():
        raise RuntimeError("Falha ao gerar o PDF.")
    return alvo


def _garantir_pdf_argos(data: str) -> Path:
    settings = load_settings()
    pasta = _pasta_argos(settings.output_dir, data)
    if not pasta:
        raise FileNotFoundError("Não há extração Argos deste dia. Extraia os percursos antes de baixar o PDF.")
    alvo = pasta / "relatorio-rotas.pdf"
    mapa = pasta / "mapa.html"
    diario = pasta / "relatorio-diario.json"
    rotas = pasta / "rotas.json"
    if not mapa.is_file() or not diario.is_file():
        raise FileNotFoundError("Faltam o mapa ou o relatório Argos deste dia.")
    if not rotas.is_file():
        raise FileNotFoundError("Falta o arquivo de rotas Argos deste dia.")
    if alvo.is_file():
        fontes = [mapa, diario, rotas]
        mais_novo = max(arq.stat().st_mtime for arq in fontes)
        if alvo.stat().st_mtime >= mais_novo:
            return alvo
    from .export import reconstruir_mapa

    reconstruir_mapa(pasta)
    _rodar_rastreado([sys.executable, "-u", "-m", "inlog_agent", "pdf", "--data", data, "--argos"])
    if not alvo.is_file():
        raise RuntimeError("Falha ao gerar o PDF Argos.")
    return alvo


def _url_saida(pasta: Path, nome: str) -> str:
    settings = load_settings()
    return f"/arquivos/{(pasta / nome).relative_to(settings.output_dir).as_posix()}"


def _word_atualizado(pasta: Path) -> bool:
    alvo = pasta / "relatorio-rotas.docx"
    if not alvo.is_file() or alvo.stat().st_size < 50_000:
        return False
    fontes = [pasta / "relatorio-diario.json", pasta / "mapa.html", pasta / "rotas.json"]
    existentes = [arq for arq in fontes if arq.is_file()]
    if not existentes:
        return True
    return alvo.stat().st_mtime >= max(arq.stat().st_mtime for arq in existentes)


def _iniciar_word_unificado(data: str, pasta: Path) -> None:
    with WORD_JOB["lock"]:
        if WORD_JOB["rodando"]:
            return
        WORD_JOB["rodando"] = True
        WORD_JOB["data"] = data
        WORD_JOB["erro"] = ""
        WORD_JOB["cancelado"] = False

    def _rodar() -> None:
        try:
            if WORD_JOB.get("cancelado"):
                return
            from .word import gerar_word_dia

            gerar_word_dia(pasta)
        except Exception as exc:
            with WORD_JOB["lock"]:
                WORD_JOB["erro"] = str(exc)
        finally:
            with WORD_JOB["lock"]:
                WORD_JOB["rodando"] = False

    threading.Thread(target=_rodar, daemon=True, name="word-unificado").start()


def _status_word_unificado(data: str) -> dict[str, Any]:
    settings = load_settings()
    pasta = _pasta_unificado(settings.output_dir, data)
    if not pasta:
        raise FileNotFoundError("Monte o relatório único deste dia antes de baixar o Word.")
    if not (pasta / "relatorio-diario.json").is_file():
        raise FileNotFoundError("Falta o relatório único deste dia.")
    with WORD_JOB["lock"]:
        gerando = bool(WORD_JOB["rodando"] and WORD_JOB["data"] == data)
        erro = str(WORD_JOB["erro"] or "") if WORD_JOB["data"] == data else ""
    if _word_atualizado(pasta) and not gerando:
        return {
            "ok": True,
            "pronto": True,
            "url": _url_saida(pasta, "relatorio-rotas.docx"),
            "nome": f"frota-rotas-{data}.docx",
        }
    if not gerando:
        _iniciar_word_unificado(data, pasta)
        return {
            "ok": True,
            "pronto": False,
            "gerando": True,
            "msg": "Montando o Word com os mapas. Pode levar alguns minutos na primeira vez.",
        }
    if erro:
        return {"ok": False, "pronto": False, "gerando": False, "erro": erro}
    return {
        "ok": True,
        "pronto": False,
        "gerando": True,
        "msg": "Montando o Word com os mapas. Pode levar alguns minutos na primeira vez.",
    }


def _garantir_word_unificado(data: str) -> Path:
    settings = load_settings()
    pasta = _pasta_unificado(settings.output_dir, data)
    if not pasta:
        raise FileNotFoundError("Monte o relatório único deste dia antes de baixar o Word.")
    alvo = pasta / "relatorio-rotas.docx"
    if _word_atualizado(pasta):
        return alvo
    from .word import gerar_word_dia

    return gerar_word_dia(pasta)


def _pdf_atualizado(pasta: Path) -> bool:
    alvo = pasta / "relatorio-rotas.pdf"
    if not alvo.is_file() or alvo.stat().st_size < 20_000:
        return False
    fontes = [pasta / "relatorio-diario.json", pasta / "mapa.html", pasta / "rotas.json"]
    existentes = [arq for arq in fontes if arq.is_file()]
    if not existentes:
        return True
    return alvo.stat().st_mtime >= max(arq.stat().st_mtime for arq in existentes)


def _iniciar_pdf_unificado(data: str, pasta_dia: Path, turno: str) -> str:
    from .pdf_cache import resolver_trabalho

    trabalho = resolver_trabalho(pasta_dia, turno or None)
    chave = f"{data}|{turno or 'atual'}"
    with PDF_JOB["lock"]:
        if PDF_JOB["rodando"] and PDF_JOB.get("chave") == chave:
            return chave
        if PDF_JOB["rodando"]:
            return ""
        PDF_JOB["rodando"] = True
        PDF_JOB["data"] = data
        PDF_JOB["turno"] = turno
        PDF_JOB["chave"] = chave
        PDF_JOB["erro"] = ""
        PDF_JOB["cancelado"] = False

    def _rodar() -> None:
        try:
            if PDF_JOB.get("cancelado"):
                return
            cmd = [sys.executable, "-u", "-m", "inlog_agent", "pdf", "--data", data, "--unificado"]
            if turno in {"diurno", "noturno"}:
                cmd.extend(["--turno", turno])
            _rodar_rastreado(cmd)
            if not (trabalho / "relatorio-rotas.pdf").is_file() and not (pasta_dia / "relatorio-rotas.pdf").is_file():
                raise RuntimeError("Falha ao gerar o PDF da frota.")
        except Exception as exc:
            with PDF_JOB["lock"]:
                PDF_JOB["erro"] = str(exc)
        finally:
            with PDF_JOB["lock"]:
                PDF_JOB["rodando"] = False

    threading.Thread(target=_rodar, daemon=True, name="pdf-unificado").start()
    return chave


def _status_pdf_unificado(data: str, turno: str = "") -> dict[str, Any]:
    from .pdf_cache import ler_progresso, pdf_inteiro_valido, resolver_trabalho, turno_de

    settings = load_settings()
    pasta = _pasta_unificado(settings.output_dir, data)
    if pasta is None or not (pasta / "relatorio-diario.json").is_file():
        pasta = _pasta_unificado(settings.output_dir, None)
    if pasta is None or not (pasta / "relatorio-diario.json").is_file():
        raise FileNotFoundError("Monte o relatório único deste dia (Coletar GPS) antes de baixar o PDF.")
    data_real = pasta.name
    pedido = (turno or turno_de(pasta) or "").strip().lower()
    trabalho = resolver_trabalho(pasta, pedido or None)
    chave = f"{data_real}|{pedido or 'atual'}"
    pronto = pdf_inteiro_valido(trabalho)
    with PDF_JOB["lock"]:
        gerando = bool(PDF_JOB["rodando"] and PDF_JOB.get("chave") == chave)
        erro = str(PDF_JOB["erro"] or "") if PDF_JOB.get("chave") == chave else ""
    if pronto and not gerando:
        return {
            "ok": True,
            "pronto": True,
            "progresso": 100,
            "job": chave,
            "url": _url_saida(trabalho, "relatorio-rotas.pdf"),
            "nome": f"frota-rotas-{pedido or 'dia'}-{data_real}.pdf",
        }
    with PDF_JOB["lock"]:
        ocupado = bool(PDF_JOB["rodando"] and PDF_JOB.get("chave") != chave)
    if ocupado:
        return {
            "ok": True,
            "pronto": False,
            "gerando": True,
            "progresso": 0,
            "job": chave,
            "msg": "Outro PDF está sendo montado. Este entra em seguida.",
        }
    if not gerando and not erro:
        _iniciar_pdf_unificado(data_real, pasta, pedido)
        return {
            "ok": True,
            "pronto": False,
            "gerando": True,
            "progresso": 5,
            "job": chave,
            "msg": "Preparando o PDF.",
        }
    if erro and not gerando:
        with PDF_JOB["lock"]:
            if PDF_JOB.get("chave") == chave:
                PDF_JOB["erro"] = ""
        return {"ok": False, "pronto": False, "gerando": False, "erro": erro, "job": chave}
    andamento = ler_progresso(trabalho)
    return {
        "ok": True,
        "pronto": False,
        "gerando": True,
        "progresso": int(andamento.get("progresso") or 8),
        "job": chave,
        "msg": str(andamento.get("msg") or "Montando o PDF da frota com os mapas."),
    }


def _garantir_pdf_unificado(data: str) -> Path:
    settings = load_settings()
    pasta = _pasta_unificado(settings.output_dir, data)
    if not pasta:
        raise FileNotFoundError("Monte o relatório único deste dia antes de baixar o PDF.")
    alvo = pasta / "relatorio-rotas.pdf"
    if _pdf_atualizado(pasta):
        return alvo
    _rodar_rastreado([sys.executable, "-u", "-m", "inlog_agent", "pdf", "--data", pasta.name, "--unificado"])
    if not alvo.is_file():
        raise RuntimeError("Falha ao gerar o PDF da frota.")
    return alvo


def _garantir_gpkg_inlog(data: str) -> Path:
    settings = load_settings()
    pasta = _pasta_rotas(settings.output_dir, data)
    if not pasta or not (pasta / "rotas.json").is_file():
        raise FileNotFoundError("Não há extração Inlog deste dia. Extraia as rotas antes de exportar o GPKG.")
    alvo = pasta / "inlog-rotas.gpkg"
    fonte = pasta / "rotas.json"
    if alvo.is_file() and alvo.stat().st_mtime >= fonte.stat().st_mtime:
        return alvo
    from .export_gpkg import gerar_gpkg_inlog

    return gerar_gpkg_inlog(pasta)


def _garantir_pdf_segtrack(data: str) -> Path:
    settings = load_settings()
    pasta = _pasta_segtrack(settings.output_dir, data)
    if not pasta:
        raise FileNotFoundError("Não há extração Segtrack deste dia. Extraia os percursos antes de baixar o PDF.")
    alvo = pasta / "relatorio-rotas.pdf"
    mapa = pasta / "mapa.html"
    diario = pasta / "relatorio-diario.json"
    rotas = pasta / "rotas.json"
    if not mapa.is_file() or not diario.is_file():
        raise FileNotFoundError("Faltam o mapa ou o relatório Segtrack deste dia.")
    if not rotas.is_file():
        raise FileNotFoundError("Falta o arquivo de rotas Segtrack deste dia.")
    if alvo.is_file():
        fontes = [mapa, diario, rotas]
        mais_novo = max(arq.stat().st_mtime for arq in fontes)
        if alvo.stat().st_mtime >= mais_novo:
            return alvo
    from .export import reconstruir_mapa

    reconstruir_mapa(pasta)
    _rodar_rastreado([sys.executable, "-u", "-m", "inlog_agent", "pdf", "--data", data, "--segtrack"])
    if not alvo.is_file():
        raise RuntimeError("Falha ao gerar o PDF Segtrack.")
    return alvo


def _garantir_gpkg_segtrack(data: str) -> Path:
    settings = load_settings()
    pasta = _pasta_segtrack(settings.output_dir, data)
    if not pasta or not (pasta / "rotas.json").is_file():
        raise FileNotFoundError("Não há extração Segtrack deste dia. Extraia os percursos antes de exportar o GPKG.")
    alvo = pasta / "segtrack-rotas.gpkg"
    fonte = pasta / "rotas.json"
    if alvo.is_file() and alvo.stat().st_mtime >= fonte.stat().st_mtime:
        return alvo
    from .export_gpkg import gerar_gpkg_segtrack

    return gerar_gpkg_segtrack(pasta)


def _garantir_gpkg_argos(data: str) -> Path:
    settings = load_settings()
    pasta = _pasta_argos(settings.output_dir, data)
    if not pasta or not (pasta / "rotas.json").is_file():
        raise FileNotFoundError("Não há extração Argos deste dia. Extraia os percursos antes de exportar o GPKG.")
    alvo = pasta / "argos-rotas.gpkg"
    fonte = pasta / "rotas.json"
    if alvo.is_file() and alvo.stat().st_mtime >= fonte.stat().st_mtime:
        return alvo
    from .export_gpkg import gerar_gpkg_argos

    return gerar_gpkg_argos(pasta)


def _enviar_chatbot_apos_unificado(data: str | None, payload: dict[str, Any]) -> None:
    try:
        from .chatbot import (
            carregar_config,
            enviar_pdf_dia,
            enviar_texto_grupo,
            _erro_whatsapp,
        )

        cfg = carregar_config()
        with CHATBOT_ENVIO["lock"]:
            pend_grupo = str(CHATBOT_ENVIO.get("grupo") or "").strip()
            pend_dia = str(CHATBOT_ENVIO.get("dia") or "").strip()
            forcar_pend = bool(CHATBOT_ENVIO.get("forcar"))
            CHATBOT_ENVIO["grupo"] = ""
            CHATBOT_ENVIO["dia"] = ""
            CHATBOT_ENVIO["forcar"] = False
        grupo = str(payload.get("grupoJid") or payload.get("grupo_jid") or "").strip()
        destino = grupo or pend_grupo or str(cfg.get("grupo_jid") or "").strip()
        token = str(cfg.get("token") or "").strip()
        dia = str(data or payload.get("data") or pend_dia or "").strip()
        pediu = forcar_pend or bool(payload.get("enviar_chatbot")) or bool(grupo)
        auto = bool(cfg.get("enviar_ao_gerar")) and bool(destino) and bool(token)
        if not pediu and not auto:
            return
        if not destino or "@g.us" not in destino:
            JOB.append("Chatbot: escolha o grupo na lista e clique em Salvar grupo.")
            return
        if not token:
            JOB.append("Chatbot: clique em Conectar WhatsApp e escaneie o QR.")
            return
        JOB.append("Enviando no grupo: aviso agora e o PDF (capa, frota, grade e mapas) em seguida...")
        try:
            enviar_texto_grupo(
                destino,
                "Sala tecnica: relatorio pronto. O PDF (capa, frota, grade e mapas) segue agora.",
            )
            JOB.append("Aviso enviado no grupo. Montando o PDF...")
            registrar_retorno_chatbot("Aviso enviado no grupo. Montando o PDF...")
        except Exception as exc:
            aviso = _erro_whatsapp(str(exc))
            JOB.append(f"Chatbot (aviso): {aviso}")
            registrar_retorno_chatbot(aviso, erro=True)
        r = enviar_pdf_dia(dia or None, grupo_jid=destino, forcar_pdf=True)
        msg = r.get("msg") or "PDF enviado ao WhatsApp."
        JOB.append(msg)
        registrar_retorno_chatbot(msg)
    except Exception as exc:
        from .chatbot import _erro_whatsapp

        aviso = _erro_whatsapp(str(exc))
        JOB.append(f"Chatbot: {aviso}")
        registrar_retorno_chatbot(aviso, erro=True)


def _executar_inspecao_job(payload: dict[str, Any]) -> None:
    from .config import aplicar_periodo, load_settings
    from .unificado import atualizar_gps_ciclo, inspecionar_rota

    codigo = str(payload.get("codigo") or "").strip().upper()
    data = str(payload.get("data") or "").strip()
    hora_inicio = str(payload.get("hora_inicio") or "06:00").strip() or "06:00"
    hora_fim = str(payload.get("hora_fim") or "05:59").strip() or "05:59"
    data_fim = str(payload.get("data_fim") or "").strip() or None
    if not codigo:
        raise RuntimeError("Informe o código da rota.")
    if not data:
        raise RuntimeError("Informe a data do ciclo.")
    dia = datetime.strptime(data, "%Y-%m-%d").date()
    fim = datetime.strptime(data_fim, "%Y-%m-%d").date() if data_fim else None
    settings = aplicar_periodo(
        load_settings(),
        data=dia,
        hora_inicio=hora_inicio,
        hora_fim=hora_fim,
        data_fim=fim,
    )
    if payload.get("extrair", True):
        JOB.append(f"Consultando pulsos para {codigo}...")
        atualizar_gps_ciclo(settings, on_progress=JOB.append, forcar=False)
    JOB.append(f"Montando a trajetória dentro de {codigo}...")
    gerado = inspecionar_rota(
        settings.output_dir,
        codigo,
        dia,
        hora_inicio,
        hora_fim,
        fim,
        gerar_pdf=bool(payload.get("pdf")),
    )
    JOB.resultado = gerado
    JOB.append(
        f"{gerado.get('codigo')}: {gerado.get('percentual') or 0}% · "
        f"{gerado.get('n_caminhoes') or 0} caminhão(ões) no polígono."
    )


def _tentar_disparo_2h() -> None:
    from .chatbot import (
        carregar_config,
        ciclo_hoje,
        marcar_ultimo_disparo,
        passou_intervalo,
        status_sessao,
        _esconder_servidor,
    )

    cfg = carregar_config()
    if not cfg.get("disparo_2h") or not cfg.get("token") or not cfg.get("grupo_jid") or not cfg.get("sessao_ativa"):
        return
    if not passou_intervalo(cfg):
        return
    with JOB.lock:
        if JOB.rodando:
            return
    try:
        if not status_sessao().get("logado"):
            return
    except Exception:
        return
    dia, data_fim = ciclo_hoje()
    try:
        grupo = str(cfg.get("grupo_jid") or "")
        marcar_envio_chatbot(grupo, dia)
        iniciar_job(
            "unificado",
            {
                "data": dia,
                "data_fim": data_fim,
                "hora_inicio": "06:00",
                "hora_fim": "05:59",
                "extrair": True,
                "enviar_chatbot": True,
                "grupo_jid": grupo,
            },
        )
        marcar_ultimo_disparo()
        JOB.append("Disparo automático: gerando o relatório do dia, o PDF e enviando no grupo.")
    except RuntimeError:
        return
    except Exception as exc:
        JOB.append(f"Disparo automático: {_esconder_servidor(str(exc))}")


def _loop_disparo_2h() -> None:
    while True:
        try:
            _tentar_disparo_2h()
        except Exception:
            pass
        time.sleep(30)


def iniciar_disparo_automatico() -> None:
    t = threading.Thread(target=_loop_disparo_2h, daemon=True, name="chatbot-disparo-2h")
    t.start()


def iniciar_job(nome: str, payload: dict[str, Any]) -> dict[str, Any]:
    with JOB.lock:
        if JOB.rodando:
            raise RuntimeError("Já existe uma execução em andamento.")
        JOB.reset(nome)
    threading.Thread(target=_executar_job, args=(nome, payload), daemon=True).start()
    return {"ok": True, "acao": nome}


def _origem_permitida(origem: str) -> bool:
    normal = (origem or "").strip().rstrip("/")
    if not normal:
        return False
    liberadas = {
        item.strip().rstrip("/")
        for item in os.getenv("CORS_ORIGIN", "").split(",")
        if item.strip()
    }
    if normal in liberadas:
        return True
    host = urlparse(normal).hostname or ""
    if host in {"localhost", "127.0.0.1"}:
        return True
    previews = os.getenv("ALLOW_VERCEL_PREVIEWS", "true").strip().lower()
    return previews in {"1", "true", "yes", "sim"} and host.endswith(".vercel.app")


class Handler(BaseHTTPRequestHandler):
    server_version = "InlogAgent/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        return

    def _cors(self) -> None:
        origem = (self.headers.get("Origin") or "").strip()
        if not origem or not _origem_permitida(origem):
            return
        self.send_header("Access-Control-Allow-Origin", origem.rstrip("/"))
        self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers",
            "Content-Type, X-Chatbot-Secret, X-Webhook-Secret, token, If-None-Match",
        )
        self.send_header("Access-Control-Expose-Headers", "ETag")

    def _json(self, status: int, payload: Any, extra: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._corpo(status, body, "application/json; charset=utf-8", extra)

    def _corpo(
        self,
        status: int,
        body: bytes,
        content_type: str,
        extra: dict[str, str] | None = None,
    ) -> None:
        try:
            self.send_response(status)
            self._cors()
            for chave, valor in (extra or {}).items():
                self.send_header(chave, valor)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            if body:
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return

    def _mapa_vivo(self, path: str, qs: dict[str, list[str]]) -> None:
        from .live_mapa import atualizacoes, detalhe_rota, geometria_bytes, trilhas

        data = (qs.get("data") or [None])[0]
        turno = (qs.get("turno") or ["diurno"])[0]
        sem_cache = {"Cache-Control": "no-store"}
        try:
            if path.endswith("/geometry"):
                body, etag = geometria_bytes(data, turno)
                pedido = (self.headers.get("If-None-Match") or "").strip()
                if pedido == etag:
                    self._corpo(304, b"", "application/json; charset=utf-8", {"ETag": etag, "Cache-Control": "private, max-age=86400"})
                    return
                self._corpo(
                    200,
                    body,
                    "application/geo+json; charset=utf-8",
                    {"ETag": etag, "Cache-Control": "private, max-age=86400"},
                )
                return
            if path.endswith("/tracks"):
                self._json(200, trilhas(data, turno), sem_cache)
                return
            if path.endswith("/track-updates"):
                self._json(200, atualizacoes(data, turno, (qs.get("since") or [None])[0]), sem_cache)
                return
            self._json(200, detalhe_rota(data, turno, (qs.get("rota") or [None])[0]), sem_cache)
        except ValueError as exc:
            self._json(400, {"ok": False, "erro": str(exc)}, sem_cache)
        except Exception as exc:
            self._json(500, {"ok": False, "erro": str(exc)}, sem_cache)

    def _bytes(
        self,
        status: int,
        content: bytes,
        content_type: str,
        download: str | None = None,
    ) -> None:
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        if download:
            self.send_header("Content-Disposition", f'attachment; filename="{download}"')
        self.end_headers()
        try:
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            return

    def do_OPTIONS(self) -> None:
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)

        if path == "/api/health":
            self._json(200, {"ok": True})
            return
        if path == "/api/diagnostico":
            from .live_db import diagnostico

            try:
                self._json(200, diagnostico())
            except Exception as exc:
                self._json(200, {"sqlite": "erro", "erro": str(exc), "persistent_disk": "erro"})
            return
        if path == "/config.js":
            self._bytes(200, b'window.SAGTRACK_API = "";\n', "application/javascript; charset=utf-8")
            return
        if path == "/" or path == "/index.html":
            html = (WEB_DIR / "index.html").read_bytes()
            self._bytes(200, html, "text/html; charset=utf-8")
            return
        if path == "/api/status":
            try:
                qs = parse_qs(parsed.query)
                data = (qs.get("data") or [None])[0]
                data_argos = (qs.get("argos") or [None])[0]
                data_segtrack = (qs.get("segtrack") or [None])[0]
                self._json(200, status_payload(data or None, data_argos or None, data_segtrack or None))
            except Exception as exc:
                self._json(500, {"erro": str(exc)})
            return
        if path == "/api/live-report":
            from .live_report import ler_estado

            qs = parse_qs(parsed.query)
            try:
                settings = load_settings()
                job = JOB.snapshot()
                self._json(
                    200,
                    ler_estado(
                        settings.output_dir,
                        (qs.get("data") or [None])[0],
                        (qs.get("turno") or ["diurno"])[0],
                        job_rodando=bool(job.get("rodando") and job.get("nome") == "unificado"),
                    ),
                )
            except ValueError as exc:
                self._json(400, {"ok": False, "erro": str(exc), "falha": str(exc)})
            except Exception as exc:
                self._json(
                    200,
                    {
                        "ok": False,
                        "vazio": True,
                        "falha": str(exc),
                        "aviso": "Falha na atualização.",
                        "rotas": [],
                        "resumo": {},
                    },
                )
            return
        if path in {
            "/api/live-report/geometry",
            "/api/live-report/tracks",
            "/api/live-report/track-updates",
            "/api/live-report/route-detail",
        }:
            self._mapa_vivo(path, parse_qs(parsed.query))
            return
        if path == "/api/chatbot-config":
            from .chatbot import publico_config, _esconder_servidor

            try:
                self._json(200, publico_config())
            except Exception as exc:
                self._json(
                    200,
                    {
                        "configurado": False,
                        "conectado": False,
                        "logado": False,
                        "sessao_ativa": False,
                        "qr": "",
                        "grupos": [],
                        "grupo_jid": "",
                        "enviar_ao_gerar": True,
                        "disparo_2h": False,
                        "proximo_rotulo": "",
                        "pacote": "",
                        "msg": _esconder_servidor(str(exc)),
                    },
                )
            return
        if path == "/api/pdf":
            try:
                data = (parse_qs(parsed.query).get("data") or [None])[0]
                if not data:
                    self._json(400, {"erro": "Informe a data do relatório."})
                    return
                pdf = _garantir_pdf(data)
                self._bytes(
                    200,
                    pdf.read_bytes(),
                    "application/pdf",
                    download=f"inlog-rotas-{data}.pdf",
                )
            except Exception as exc:
                self._json(500, {"erro": str(exc)})
            return
        if path == "/api/gpkg":
            try:
                data = (parse_qs(parsed.query).get("data") or [None])[0]
                if not data:
                    self._json(400, {"erro": "Informe a data do GeoPackage Inlog."})
                    return
                gpkg = _garantir_gpkg_inlog(data)
                self._bytes(
                    200,
                    gpkg.read_bytes(),
                    "application/geopackage+sqlite3",
                    download=f"inlog-rotas-{data}.gpkg",
                )
            except Exception as exc:
                self._json(500, {"erro": str(exc)})
            return
        if path == "/api/gpkg-argos":
            try:
                data = (parse_qs(parsed.query).get("data") or [None])[0]
                if not data:
                    self._json(400, {"erro": "Informe a data do GeoPackage Argos."})
                    return
                gpkg = _garantir_gpkg_argos(data)
                self._bytes(
                    200,
                    gpkg.read_bytes(),
                    "application/geopackage+sqlite3",
                    download=f"argos-rotas-{data}.gpkg",
                )
            except Exception as exc:
                self._json(500, {"erro": str(exc)})
            return
        if path == "/api/gpkg-segtrack":
            try:
                data = (parse_qs(parsed.query).get("data") or [None])[0]
                if not data:
                    self._json(400, {"erro": "Informe a data do GeoPackage Segtrack."})
                    return
                gpkg = _garantir_gpkg_segtrack(data)
                self._bytes(
                    200,
                    gpkg.read_bytes(),
                    "application/geopackage+sqlite3",
                    download=f"segtrack-rotas-{data}.gpkg",
                )
            except Exception as exc:
                self._json(500, {"erro": str(exc)})
            return
        if path == "/api/pdf-segtrack":
            try:
                data = (parse_qs(parsed.query).get("data") or [None])[0]
                if not data:
                    self._json(400, {"erro": "Informe a data do relatório Segtrack."})
                    return
                pdf = _garantir_pdf_segtrack(data)
                self._bytes(
                    200,
                    pdf.read_bytes(),
                    "application/pdf",
                    download=f"segtrack-rotas-{data}.pdf",
                )
            except Exception as exc:
                self._json(500, {"erro": str(exc)})
            return
        if path == "/api/word-unificado":
            try:
                data = (parse_qs(parsed.query).get("data") or [None])[0]
                if not data:
                    self._json(400, {"erro": "Informe a data do relatório único."})
                    return
                info = _status_word_unificado(data)
                self._json(200 if info.get("ok", True) else 500, info)
            except Exception as exc:
                self._json(500, {"erro": str(exc)})
            return
        if path == "/api/pdf-unificado":
            try:
                data = (parse_qs(parsed.query).get("data") or [None])[0]
                if not data:
                    self._json(400, {"erro": "Informe a data do relatório único."})
                    return
                turno = (parse_qs(parsed.query).get("turno") or [""])[0]
                info = _status_pdf_unificado(data, turno)
                self._json(200 if info.get("ok", True) else 500, info)
            except Exception as exc:
                self._json(500, {"erro": str(exc)})
            return
        if path == "/api/pdf-argos":
            try:
                data = (parse_qs(parsed.query).get("data") or [None])[0]
                if not data:
                    self._json(400, {"erro": "Informe a data do relatório Argos."})
                    return
                pdf = _garantir_pdf_argos(data)
                self._bytes(
                    200,
                    pdf.read_bytes(),
                    "application/pdf",
                    download=f"argos-rotas-{data}.pdf",
                )
            except Exception as exc:
                self._json(500, {"erro": str(exc)})
            return
        if path == "/api/rotas-oficiais":
            try:
                from .mapa_oficial import listar_rotas_oficiais

                qs = parse_qs(parsed.query)
                data = (qs.get("data") or [""])[0].strip()
                dia = datetime.strptime(data, "%Y-%m-%d").date() if data else None
                rotas = listar_rotas_oficiais(dia)
                self._json(
                    200,
                    {
                        "data": data or None,
                        "rotas": rotas,
                    },
                )
            except ValueError as exc:
                self._json(400, {"erro": str(exc)})
            except Exception as exc:
                self._json(500, {"erro": str(exc)})
            return
        if path == "/api/inspecionar-rota":
            try:
                from .unificado import inspecionar_rota

                qs = parse_qs(parsed.query)
                codigo = (qs.get("codigo") or [""])[0].strip()
                data = (qs.get("data") or [""])[0].strip()
                hora_inicio = (qs.get("hora_inicio") or ["06:00"])[0].strip() or "06:00"
                hora_fim = (qs.get("hora_fim") or ["05:59"])[0].strip() or "05:59"
                data_fim = (qs.get("data_fim") or [""])[0].strip() or None
                gerar_pdf = (qs.get("pdf") or ["1"])[0].strip().lower() not in {"0", "false", "nao", "não"}
                if not codigo:
                    self._json(400, {"erro": "Informe o código da rota, por exemplo 16AD."})
                    return
                if not data:
                    self._json(400, {"erro": "Informe a data do ciclo."})
                    return
                dia = datetime.strptime(data, "%Y-%m-%d").date()
                fim = datetime.strptime(data_fim, "%Y-%m-%d").date() if data_fim else None
                gerado = inspecionar_rota(
                    load_settings().output_dir,
                    codigo,
                    dia,
                    hora_inicio,
                    hora_fim,
                    fim,
                    gerar_pdf=gerar_pdf,
                )
                self._json(200, gerado)
            except ValueError as exc:
                self._json(400, {"erro": str(exc)})
            except Exception as exc:
                self._json(500, {"erro": str(exc)})
            return
        if path.startswith("/arquivos/"):
            settings = load_settings()
            relativo = path.removeprefix("/arquivos/")
            base = settings.output_dir.resolve()
            alvo = (base / Path(relativo)).resolve()
            if not alvo.is_relative_to(base) or not alvo.is_file():
                self._json(404, {"erro": "Arquivo não encontrado."})
                return
            tipo = mimetypes.guess_type(alvo.name)[0] or "application/octet-stream"
            inspecao = alvo.suffix.lower() == ".pdf" and alvo.name.lower().startswith("inspecao-")
            baixar = None if inspecao else (alvo.name if alvo.suffix.lower() in {".docx", ".pdf", ".xlsx", ".gpkg", ".zip"} else None)
            self._bytes(200, alvo.read_bytes(), tipo, download=baixar)
            return
        self._json(404, {"erro": "Rota não encontrada."})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8") or "{}")
        except json.JSONDecodeError:
            self._json(400, {"erro": "JSON inválido."})
            return

        acao = path.removeprefix("/api/")
        if acao == "parar":
            self._json(200, cancelar_execucao())
            return
        if path == "/api/live-report/sync":
            from .live_report import pedir_atualizacao

            corpo = payload if isinstance(payload, dict) else {}
            try:
                settings = load_settings()
                job = JOB.snapshot()
                self._json(
                    200,
                    pedir_atualizacao(
                        settings.output_dir,
                        str(corpo.get("data") or ""),
                        str(corpo.get("turno") or "diurno"),
                        job_rodando=bool(job.get("rodando") and job.get("nome") == "unificado"),
                    ),
                )
            except ValueError as exc:
                self._json(400, {"ok": False, "erro": str(exc), "falha": str(exc)})
            except Exception as exc:
                self._json(
                    200,
                    {
                        "ok": False,
                        "vazio": True,
                        "falha": str(exc),
                        "aviso": "Falha na atualização.",
                        "rotas": [],
                        "resumo": {},
                    },
                )
            return
        if path == "/api/chatbot-config":
            from .chatbot import salvar_config, publico_config, _esconder_servidor

            try:
                body = payload if isinstance(payload, dict) else {}
                salvar_config(
                    {
                        "grupo_jid": str(body.get("grupo_jid") or body.get("grupoJid") or "").strip(),
                        "enviar_ao_gerar": bool(body.get("enviar_ao_gerar", True)),
                        "disparo_2h": bool(body.get("disparo_2h")),
                    }
                )
                self._json(200, publico_config())
            except Exception as exc:
                self._json(400, {"erro": _esconder_servidor(str(exc))})
            return
        if path == "/api/chatbot-conectar":
            from .chatbot import conectar_whatsapp, _esconder_servidor

            try:
                self._json(200, conectar_whatsapp())
            except Exception as exc:
                self._json(400, {"ok": False, "erro": _esconder_servidor(str(exc))})
            return
        if path == "/api/chatbot-desconectar":
            from .chatbot import desconectar_whatsapp, _esconder_servidor

            try:
                self._json(200, desconectar_whatsapp())
            except Exception as exc:
                self._json(400, {"ok": False, "erro": _esconder_servidor(str(exc))})
            return
        if path == "/api/chatbot-enviar":
            from .chatbot import (
                carregar_config,
                ciclo_hoje,
                salvar_config,
                enviar_texto_grupo,
                _esconder_servidor,
            )

            try:
                body = payload if isinstance(payload, dict) else {}
                cfg = carregar_config()
                grupo = str(body.get("grupo_jid") or body.get("grupoJid") or cfg.get("grupo_jid") or "").strip()
                if not grupo or "@g.us" not in grupo:
                    raise RuntimeError("Escolha o grupo do WhatsApp que vai receber o relatório.")
                if not cfg.get("token"):
                    raise RuntimeError("Clique em Conectar WhatsApp e escaneie o QR.")
                dia = str(body.get("data") or "").strip()
                if not dia:
                    dia, data_fim = ciclo_hoje()
                else:
                    data_fim = str(body.get("data_fim") or "").strip()
                    if not data_fim or data_fim == dia:
                        data_fim = (datetime.strptime(dia, "%Y-%m-%d").date() + timedelta(days=1)).isoformat()
                hora_inicio = str(body.get("hora_inicio") or "06:00").strip() or "06:00"
                hora_fim = str(body.get("hora_fim") or "05:59").strip() or "05:59"
                salvar_config({"grupo_jid": grupo, "sessao_ativa": True})
                marcar_envio_chatbot(grupo, dia)
                print(f"Chatbot: pedido recebido. Gerando relatorio {dia} e PDF no grupo.", flush=True)

                def avisar() -> None:
                    try:
                        partes_aviso = dia.split("-")
                        rotulo_aviso = (
                            f"{partes_aviso[2]}/{partes_aviso[1]}/{partes_aviso[0]}"
                            if len(partes_aviso) == 3
                            else dia
                        )
                        enviar_texto_grupo(
                            grupo,
                            f"Sala tecnica: gerando o relatorio de {rotulo_aviso}. "
                            "O PDF (capa, frota, grade e mapas) chega em seguida.",
                        )
                        registrar_retorno_chatbot(
                            "Aviso enviado no grupo. O painel está gerando o relatório. O PDF sai quando terminar."
                        )
                        JOB.append("Chatbot: aviso enviado no grupo.")
                    except Exception as exc:
                        aviso = _esconder_servidor(str(exc))
                        registrar_retorno_chatbot(aviso, erro=True)
                        JOB.append(f"Chatbot (aviso): {aviso}")

                threading.Thread(target=avisar, name="chatbot-aviso", daemon=True).start()
                try:
                    job = iniciar_job(
                        "unificado",
                        {
                            "data": dia,
                            "data_fim": data_fim,
                            "hora_inicio": hora_inicio,
                            "hora_fim": hora_fim,
                            "extrair": True,
                            "enviar_chatbot": True,
                            "grupo_jid": grupo,
                        },
                    )
                    msg = (
                        "Pedido recebido. O aviso vai para o grupo agora e o PDF quando o relatório terminar. "
                        "Acompanhe esta mensagem: ela muda para enviado ou para o erro."
                    )
                except RuntimeError as exc:
                    if "andamento" in str(exc).lower():
                        msg = (
                            "Pedido recebido. Já havia um relatório em andamento; "
                            "quando ele terminar, o PDF vai para o grupo. A mensagem aqui atualiza."
                        )
                        job = {"ok": True, "acao": "unificado"}
                    else:
                        raise
                self._json(202, {**job, "ok": True, "msg": msg})
            except RuntimeError as exc:
                codigo = 409 if "andamento" in str(exc).lower() else 400
                self._json(codigo, {"ok": False, "erro": _esconder_servidor(str(exc))})
            except Exception as exc:
                self._json(400, {"ok": False, "erro": _esconder_servidor(str(exc))})
            return
        if path == "/api/chatbot/gerar":
            from .chatbot import carregar_config

            cfg = carregar_config()
            recebido = (self.headers.get("X-Chatbot-Secret") or self.headers.get("X-Webhook-Secret") or self.headers.get("token") or "").strip()
            esperado = str(cfg.get("token") or "")
            if not esperado or not recebido or not hmac.compare_digest(recebido, esperado):
                self._json(401, {"ok": False, "erro": "Token da API inválido."})
                return
            corpo = payload if isinstance(payload, dict) else {}
            corpo = {
                **corpo,
                "extrair": True,
                "enviar_chatbot": True,
                "grupoJid": corpo.get("grupoJid") or corpo.get("grupo_jid") or cfg.get("grupo_jid") or "",
            }
            try:
                self._json(202, {**iniciar_job("unificado", corpo), "ok": True})
            except Exception as exc:
                self._json(409, {"ok": False, "erro": str(exc)})
            return
        if path == "/api/inspecionar":
            corpo = payload if isinstance(payload, dict) else {}
            if not str(corpo.get("codigo") or "").strip():
                self._json(400, {"erro": "Informe o código da rota."})
                return
            try:
                self._json(202, iniciar_job("inspecionar", corpo))
            except Exception as exc:
                self._json(409, {"erro": str(exc)})
            return
        if acao not in {
            "rotas",
            "argos-rotas",
            "segtrack-rotas",
            "unificado",
        }:
            self._json(404, {"erro": "Ação inválida."})
            return
        try:
            self._json(202, iniciar_job(acao, payload if isinstance(payload, dict) else {}))
        except Exception as exc:
            self._json(409, {"erro": str(exc)})


class SalaHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = False


def _porta_do_ambiente(explicita: int | None) -> int:
    if explicita:
        return explicita
    bruto = (os.getenv("PORT") or os.getenv("PORTA") or "").strip()
    try:
        return int(bruto) if bruto else 5889
    except ValueError:
        return 5889


def servir(porta: int | None = None, host: str | None = None) -> None:
    # O glibc do Linux abre várias arenas e estoura os 512 MB do Render.
    os.environ.setdefault("MALLOC_ARENA_MAX", "2")
    try:
        settings = load_settings()
        usada = porta or settings.porta or _porta_do_ambiente(None)
    except Exception:
        usada = _porta_do_ambiente(porta)
    padrao_host = "0.0.0.0" if os.getenv("PORT") else "127.0.0.1"
    usado_host = (host or os.getenv("HOST") or padrao_host).strip() or padrao_host
    try:
        httpd = SalaHTTPServer((usado_host, usada), Handler)
    except OSError as exc:
        print(f"A porta {usada} já está em uso.")
        print(f"Se o painel já abriu, use http://{usado_host}:{usada}")
        print(exc, flush=True)
        return
    print(f"Sala tecnica em http://{usado_host}:{usada}", flush=True)
    from .live_db import DISCO, no_render

    if no_render() and DISCO.is_dir():
        from .live_db import provar_disco

        prova = provar_disco()
        print(f"Disco /var/sagtrack: escrita {prova}. Banco /var/sagtrack/sagtrack_live.db", flush=True)
        saida = (os.getenv("OUTPUT_DIR") or "")
        if "/opt/render/project" in saida or not saida:
            print(
                "PDF, Word e GPKG continuam no disco temporário do código e podem sumir no deploy.",
                flush=True,
            )
    iniciar_disparo_automatico()
    from .live_sync import iniciar_loop
    import inlog_agent.live_mapa  # shapely não é seguro se duas abas importarem ao mesmo tempo

    iniciar_loop()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nServidor encerrado.")
    except Exception:
        import traceback

        traceback.print_exc()
        print("\nO servidor parou.", flush=True)
    finally:
        httpd.server_close()
