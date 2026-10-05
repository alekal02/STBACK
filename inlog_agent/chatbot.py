from __future__ import annotations

import base64
import json
import os
import re
import secrets
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    from zoneinfo import ZoneInfo

    TZ_BRT = ZoneInfo("America/Sao_Paulo")
except Exception:
    TZ_BRT = timezone(timedelta(hours=-3))

INTERVALO_DISPARO = timedelta(hours=2)

from dotenv import load_dotenv

from .config import ROOT

ARQUIVO = ROOT / "data" / "chatbot.json"
NOME_INSTANCIA = "Sala tecnica"
_URL_INTERNA = "http://24.199.86.195:8080"


def _nome_pacote() -> str:
    try:
        from . import config as cfg

        fn = getattr(cfg, "rotulo_pacote", None)
        if callable(fn):
            return str(fn() or "")
    except Exception:
        pass
    return ""


def _env_load() -> None:
    load_dotenv(ROOT / ".env", override=False)


def url_wuzapi() -> str:
    _env_load()
    bruto = (os.getenv("WUZAPI_URL") or _URL_INTERNA).strip()
    return normalizar_url(bruto)


def admin_token() -> str:
    _env_load()
    return (os.getenv("WUZAPI_ADMIN_TOKEN") or "1234ABCD").strip()


def normalizar_url(url: str) -> str:
    texto = (url or "").strip().rstrip("/")
    if not texto:
        return ""
    if not texto.startswith("http://") and not texto.startswith("https://"):
        texto = "http://" + texto
    return texto.rstrip("/")


def _esconder_servidor(texto: str) -> str:
    return re.sub(r"https?://[^\s/'\"<>]+", "servidor do chatbot", str(texto or ""), flags=re.I)


def _erro_whatsapp(texto: str) -> str:
    bruto = _esconder_servidor(texto)
    t = bruto.lower()
    if "not participating" in t or "not a participant" in t or "não está nesse grupo" in t:
        return (
            "O WhatsApp que escaneou o QR não faz parte desse grupo. "
            "Entre no grupo com esse mesmo número, clique de novo em Conectar WhatsApp "
            "e escolha o grupo na lista."
        )
    if "no session" in t or "not logged" in t or "not connected" in t:
        return "O WhatsApp desconectou. Clique em Conectar WhatsApp e escaneie o QR de novo."
    if "file too" in t or "too large" in t or "max file" in t or "document too" in t:
        return "O PDF ficou grande demais para o WhatsApp. O relatório foi gerado na Sala técnica; baixe pelo painel."
    return bruto


def carregar_config() -> dict[str, Any]:
    _env_load()
    base = {
        "token": os.getenv("WUZAPI_TOKEN", "").strip(),
        "grupo_jid": os.getenv("CHATBOT_GRUPO_JID", "").strip(),
        "enviar_ao_gerar": True,
        "disparo_2h": False,
        "ultimo_disparo": "",
        "sessao_ativa": False,
    }
    if ARQUIVO.is_file():
        try:
            salvo = json.loads(ARQUIVO.read_text(encoding="utf-8"))
            if isinstance(salvo, dict):
                if salvo.get("token"):
                    base["token"] = str(salvo["token"]).strip()
                if "grupo_jid" in salvo:
                    base["grupo_jid"] = str(salvo.get("grupo_jid") or "").strip()
                if "enviar_ao_gerar" in salvo:
                    base["enviar_ao_gerar"] = bool(salvo["enviar_ao_gerar"])
                if "disparo_2h" in salvo:
                    base["disparo_2h"] = bool(salvo["disparo_2h"])
                if salvo.get("ultimo_disparo"):
                    base["ultimo_disparo"] = str(salvo["ultimo_disparo"]).strip()
                if "sessao_ativa" in salvo:
                    base["sessao_ativa"] = bool(salvo.get("sessao_ativa"))
        except Exception:
            pass
    return base


def salvar_config(payload: dict[str, Any] | None = None) -> dict[str, Any]:
    atual = carregar_config()
    extra = payload or {}
    token = str(extra.get("token") if extra.get("token") is not None else atual.get("token") or "").strip()
    if extra.get("grupo_jid") is not None or extra.get("grupoJid") is not None:
        novo_grupo = str(extra.get("grupo_jid") or extra.get("grupoJid") or "").strip()
        grupo = novo_grupo if novo_grupo or extra.get("limpar_grupo") else str(atual.get("grupo_jid") or "").strip()
    else:
        grupo = str(atual.get("grupo_jid") or "").strip()
    if "enviar_ao_gerar" in extra:
        auto = bool(extra.get("enviar_ao_gerar"))
    else:
        auto = bool(atual.get("enviar_ao_gerar", True))
    if "disparo_2h" in extra:
        disparo = bool(extra.get("disparo_2h"))
    else:
        disparo = bool(atual.get("disparo_2h"))
    if "ultimo_disparo" in extra:
        ultimo = str(extra.get("ultimo_disparo") or "").strip()
    elif disparo and not atual.get("disparo_2h"):
        ultimo = ""
    else:
        ultimo = str(atual.get("ultimo_disparo") or "").strip()
    if "sessao_ativa" in extra:
        sessao = bool(extra.get("sessao_ativa"))
    else:
        sessao = bool(atual.get("sessao_ativa"))
    saida = {
        "token": token,
        "grupo_jid": grupo,
        "enviar_ao_gerar": auto,
        "disparo_2h": disparo,
        "ultimo_disparo": ultimo,
        "sessao_ativa": sessao,
    }
    ARQUIVO.parent.mkdir(parents=True, exist_ok=True)
    ARQUIVO.write_text(json.dumps(saida, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return saida


def agora_brt() -> datetime:
    return datetime.now(TZ_BRT)


def ciclo_hoje() -> tuple[str, str]:
    hoje = agora_brt().date()
    return hoje.isoformat(), (hoje + timedelta(days=1)).isoformat()


def _parse_iso(texto: str) -> datetime | None:
    bruto = (texto or "").strip()
    if not bruto:
        return None
    try:
        dt = datetime.fromisoformat(bruto)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ_BRT)
    return dt.astimezone(TZ_BRT)


def proximo_disparo_dt(cfg: dict[str, Any] | None = None) -> datetime | None:
    dados = cfg or carregar_config()
    if not dados.get("disparo_2h"):
        return None
    ultimo = _parse_iso(str(dados.get("ultimo_disparo") or ""))
    if ultimo is None:
        return agora_brt()
    return ultimo + INTERVALO_DISPARO


def passou_intervalo(cfg: dict[str, Any] | None = None) -> bool:
    dados = cfg or carregar_config()
    proximo = proximo_disparo_dt(dados)
    if proximo is None:
        return False
    return agora_brt() >= proximo


def marcar_ultimo_disparo() -> str:
    marca = agora_brt().isoformat(timespec="seconds")
    salvar_config({"ultimo_disparo": marca})
    return marca


def campos_disparo(cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    dados = cfg or carregar_config()
    proximo = proximo_disparo_dt(dados)
    rotulo = ""
    if dados.get("disparo_2h") and proximo is not None:
        if not dados.get("ultimo_disparo"):
            rotulo = "Disparo ligado. Em instantes gera o relatório do dia e envia no grupo. Depois, a cada 2 horas."
        else:
            rotulo = f"Disparo ligado. Próximo: gera o relatório e envia às {proximo.strftime('%H:%M')}."
    return {
        "disparo_2h": bool(dados.get("disparo_2h")),
        "proximo_disparo": proximo.isoformat(timespec="minutes") if proximo else "",
        "proximo_rotulo": rotulo,
        "ultimo_disparo": str(dados.get("ultimo_disparo") or ""),
    }


def _unwrap(data: Any) -> Any:
    if isinstance(data, dict) and "success" in data and "data" in data:
        return data["data"]
    return data


def _http(
    method: str,
    caminho: str,
    *,
    body: dict[str, Any] | None = None,
    admin: bool = False,
    token: str | None = None,
    timeout: int = 30,
) -> Any:
    headers = {"Content-Type": "application/json"}
    if admin:
        headers["Authorization"] = admin_token()
    else:
        uso = (token or carregar_config().get("token") or "").strip()
        if not uso:
            raise RuntimeError("O WhatsApp ainda não tem token. Clique em Conectar WhatsApp.")
        headers["token"] = uso
    dados = None if body is None else json.dumps(body).encode("utf-8")
    req = Request(url_wuzapi() + caminho, data=dados, method=method, headers=headers)
    try:
        with urlopen(req, timeout=timeout) as resp:
            bruto = resp.read().decode("utf-8") or "{}"
            try:
                return json.loads(bruto)
            except json.JSONDecodeError:
                return {"raw": bruto}
    except HTTPError as exc:
        detalhe = ""
        try:
            detalhe = exc.read().decode("utf-8", errors="replace")
            parsed = json.loads(detalhe)
            erro = parsed.get("error") or parsed.get("erro") or parsed.get("message")
            if erro:
                raise RuntimeError(_erro_whatsapp(str(erro))) from exc
            if parsed.get("success") is True:
                return parsed
        except RuntimeError:
            raise
        except Exception:
            pass
        raise RuntimeError(_erro_whatsapp(f"Chatbot recusou ({exc.code}). {detalhe[:180]}".strip())) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(
            "Não alcancei o WhatsApp desta máquina. Confira a internet: "
            "o painel precisa falar com a API do chatbot para devolver o relatório no grupo."
        ) from exc


def _usuarios_admin() -> list[dict[str, Any]]:
    raw = _unwrap(_http("GET", "/admin/users", admin=True))
    if isinstance(raw, list):
        return [u for u in raw if isinstance(u, dict)]
    if isinstance(raw, dict):
        for chave in ("users", "Users"):
            if isinstance(raw.get(chave), list):
                return [u for u in raw[chave] if isinstance(u, dict)]
    return []


def _nome_sala(nome: str) -> bool:
    n = (nome or "").lower().replace("é", "e")
    return n in {"sala tecnica", "inlogrobo", "sistemma", "relatorios coleta"}


def garantir_token() -> str:
    cfg = carregar_config()
    atual = str(cfg.get("token") or "").strip()
    if atual:
        return atual
    for user in _usuarios_admin():
        if _nome_sala(str(user.get("name") or "")) and user.get("token"):
            token = str(user["token"]).strip()
            salvar_config({"token": token})
            return token
    token = "sala_tecnica_" + secrets.token_hex(16)
    try:
        _http(
            "POST",
            "/admin/users",
            body={"name": NOME_INSTANCIA, "token": token, "events": "Message,Connected,QR"},
            admin=True,
        )
    except RuntimeError as exc:
        if "409" not in str(exc) and "exist" not in str(exc).lower() and "already" not in str(exc).lower():
            raise
    salvar_config({"token": token})
    return token


def _flag_bool(raw: dict[str, Any], *chaves: str) -> bool | None:
    for chave in chaves:
        if chave not in raw:
            continue
        valor = raw.get(chave)
        if isinstance(valor, bool):
            return valor
        if isinstance(valor, (int, float)):
            return bool(valor)
        texto = str(valor or "").strip().lower()
        if texto in {"true", "1", "yes", "sim"}:
            return True
        if texto in {"false", "0", "no", "nao", "não", ""}:
            return False
    return None


def _jid_whatsapp(raw: dict[str, Any]) -> str:
    for chave in ("jid", "JID", "Jid"):
        texto = str(raw.get(chave) or "").strip()
        baixo = texto.lower()
        if "@s.whatsapp.net" in baixo or baixo.endswith("@lid"):
            return texto
    return ""


def status_sessao(token: str | None = None) -> dict[str, Any]:
    raw = _unwrap(_http("GET", "/session/status", token=token, timeout=15)) or {}
    if not isinstance(raw, dict):
        raw = {}
    jid = _jid_whatsapp(raw)
    logged = _flag_bool(raw, "loggedIn", "LoggedIn", "loggedin")
    if logged is False:
        logado = False
        jid = ""
    elif logged is True:
        logado = True
    else:
        logado = bool(jid)
    return {
        "conectado": bool(_flag_bool(raw, "connected", "Connected") or False),
        "logado": logado,
        "jid": jid,
        "qr": _qr_data_uri(raw.get("qrcode") or raw.get("QRCode") or raw.get("qrCode") or ""),
    }


def _qr_data_uri(qr: Any) -> str:
    texto = str(qr or "").strip()
    if not texto:
        return ""
    if texto.startswith("data:"):
        return texto
    return "data:image/png;base64," + texto.replace("\n", "")


def obter_qr(token: str | None = None) -> str:
    try:
        raw = _unwrap(_http("GET", "/session/qr", token=token, timeout=15)) or {}
        if isinstance(raw, dict):
            return _qr_data_uri(raw.get("QRCode") or raw.get("qrcode") or raw.get("qrCode") or raw.get("qr") or "")
        return _qr_data_uri(raw)
    except Exception:
        return status_sessao(token).get("qr") or ""


def _encerrar_sessao_wuzapi(token: str) -> None:
    if not token:
        return
    try:
        _http("POST", "/session/logout", body={}, token=token, timeout=25)
    except RuntimeError as exc:
        msg = str(exc).lower()
        if not any(
            trecho in msg
            for trecho in ("no session", "not logged", "not connected", "logged out")
        ):
            raise
    except Exception:
        pass
    try:
        _http("POST", "/session/disconnect", body={}, token=token, timeout=20)
    except Exception:
        pass
    time.sleep(0.4)


def conectar_whatsapp() -> dict[str, Any]:
    cfg = carregar_config()
    ja_ativa = bool(cfg.get("sessao_ativa"))
    token = garantir_token()
    try:
        st = status_sessao(token)
    except Exception:
        st = {"conectado": False, "logado": False, "jid": "", "qr": ""}
    if ja_ativa and st.get("logado"):
        salvar_config({"sessao_ativa": True})
        grupos = listar_grupos(token)
        cfg = carregar_config()
        return {
            "ok": True,
            "conectado": True,
            "logado": True,
            "sessao_ativa": True,
            "qr": "",
            "grupos": grupos,
            "grupo_jid": cfg.get("grupo_jid") or "",
            "enviar_ao_gerar": bool(cfg.get("enviar_ao_gerar", True)),
            **campos_disparo(cfg),
            "msg": "WhatsApp conectado. Escolha o grupo para receber o relatório.",
        }
    if st.get("logado") or st.get("conectado"):
        _encerrar_sessao_wuzapi(token)
        st = {"conectado": False, "logado": False, "jid": "", "qr": ""}
    salvar_config({"sessao_ativa": True})
    try:
        _http(
            "POST",
            "/session/connect",
            body={"Subscribe": ["Message", "Connected", "QR"], "Immediate": True},
            token=token,
            timeout=40,
        )
    except RuntimeError as exc:
        msg = str(exc).lower()
        if "already connected" not in msg and "ja conectad" not in msg and "já conectad" not in msg:
            raise
    time.sleep(1.2)
    st = status_sessao(token)
    qr = ""
    if not st.get("logado"):
        qr = obter_qr(token) or st.get("qr") or ""
    grupos: list[dict[str, Any]] = []
    if st.get("logado"):
        grupos = listar_grupos(token)
    cfg = carregar_config()
    return {
        "ok": True,
        "conectado": bool(st.get("logado")),
        "logado": bool(st.get("logado")),
        "sessao_ativa": True,
        "qr": qr,
        "grupos": grupos,
        "grupo_jid": cfg.get("grupo_jid") or "",
        "enviar_ao_gerar": bool(cfg.get("enviar_ao_gerar", True)),
        **campos_disparo(cfg),
        "msg": (
            "WhatsApp conectado. Escolha o grupo para receber o relatório."
            if st.get("logado")
            else "Escaneie o QR no WhatsApp (Aparelhos conectados)."
        ),
    }


def desconectar_whatsapp() -> dict[str, Any]:
    token = str(carregar_config().get("token") or "").strip()
    _encerrar_sessao_wuzapi(token)
    salvar_config({"grupo_jid": "", "ultimo_disparo": "", "sessao_ativa": False, "limpar_grupo": True})
    estado = estado_publico()
    estado["ok"] = True
    estado["logado"] = False
    estado["conectado"] = False
    estado["sessao_ativa"] = False
    estado["qr"] = ""
    estado["grupos"] = []
    estado["grupo_jid"] = ""
    estado["msg"] = "WhatsApp desconectado. A sessão antiga foi apagada. Clique em Conectar WhatsApp para gerar o QR."
    return estado


def listar_grupos(token: str | None = None) -> list[dict[str, Any]]:
    raw = _unwrap(_http("GET", "/group/list", token=token, timeout=25)) or {}
    groups = []
    if isinstance(raw, dict):
        groups = raw.get("Groups") or raw.get("groups") or []
    elif isinstance(raw, list):
        groups = raw
    saida = []
    for item in groups:
        if not isinstance(item, dict):
            continue
        jid = str(item.get("JID") or item.get("jid") or item.get("Id") or item.get("id") or "")
        if "@g.us" not in jid:
            continue
        nome = str(item.get("Name") or item.get("name") or item.get("Subject") or item.get("subject") or jid)
        saida.append({"jid": jid, "nome": nome})
    saida.sort(key=lambda g: g["nome"].lower())
    return saida


def estado_publico() -> dict[str, Any]:
    cfg = carregar_config()
    token = str(cfg.get("token") or "")
    base = {
        "configurado": bool(token),
        "conectado": False,
        "logado": False,
        "qr": "",
        "grupos": [],
        "grupo_jid": cfg.get("grupo_jid") or "",
        "enviar_ao_gerar": bool(cfg.get("enviar_ao_gerar", True)),
        "sessao_ativa": False,
        "pacote": _nome_pacote(),
        **campos_disparo(cfg),
        "msg": "Clique em Conectar WhatsApp para gerar o QR.",
    }
    if not token or not cfg.get("sessao_ativa"):
        return base
    try:
        st = status_sessao(token)
    except Exception as exc:
        base["msg"] = _erro_whatsapp(str(exc))
        return base
    logado = bool(st.get("logado"))
    base.update({"conectado": logado, "logado": logado, "sessao_ativa": True})
    if logado:
        try:
            base["grupos"] = listar_grupos(token)
        except Exception:
            base["grupos"] = []
        extra = str(base.get("proximo_rotulo") or "")
        if extra:
            base["msg"] = extra
        else:
            base["msg"] = "WhatsApp conectado. Escolha o grupo."
    else:
        base["qr"] = obter_qr(token) or st.get("qr") or ""
        if base["qr"]:
            base["msg"] = "Escaneie o QR no WhatsApp (Aparelhos conectados)."
        else:
            base["msg"] = "Clique em Conectar WhatsApp para gerar o QR."
    return base


def _pdf_do_dia(dia: str) -> Path | None:
    from .config import load_settings

    pasta = load_settings().output_dir / "unificado" / dia
    alvo = pasta / "relatorio-rotas.pdf"
    return alvo if alvo.is_file() else None


def garantir_pdf_relatorio(dia: str, *, forcar: bool = False) -> Path:
    """Gera o mesmo PDF da Sala técnica (capa, frota, grade e mapas)."""
    from .config import load_settings
    from .pdf import gerar_pdf_dia

    pasta = load_settings().output_dir / "unificado" / dia
    diario = pasta / "relatorio-diario.json"
    mapa = pasta / "mapa.html"
    if not diario.is_file() or not mapa.is_file():
        raise RuntimeError(
            "Gere o relatório do dia na Sala técnica (Gerar relatório) antes de enviar o PDF."
        )
    alvo = pasta / "relatorio-rotas.pdf"
    fontes = [diario, mapa, pasta / "rotas.json"]
    existentes = [arq for arq in fontes if arq.is_file()]
    if (
        not forcar
        and alvo.is_file()
        and alvo.stat().st_size > 20_000
        and existentes
        and alvo.stat().st_mtime >= max(arq.stat().st_mtime for arq in existentes)
    ):
        return alvo
    return gerar_pdf_dia(pasta)


def _legenda(dia: str) -> str:
    from .config import load_settings

    pasta = load_settings().output_dir / "unificado" / dia
    feitas = parciais = apoios = nao = "?"
    rel = pasta / "relatorio.json"
    if rel.is_file():
        try:
            dados = json.loads(rel.read_text(encoding="utf-8"))
            feitas = dados.get("feitas", feitas)
            parciais = dados.get("parciais", parciais)
            apoios = dados.get("apoios", apoios)
            nao = dados.get("nao_feitas", nao)
        except Exception:
            pass
    partes = dia.split("-")
    rotulo = f"{partes[2]}/{partes[1]}/{partes[0]}" if len(partes) == 3 else dia
    return (
        f"📍 Relatório GPS · {rotulo}\n"
        f"Concluídas: {feitas} · Parciais: {parciais} · Apoios: {apoios} · Não feitas: {nao}\n"
        "Cobertura dos rastreadores (Inlog + Argos + Segtrack). Não é prova de coleta."
    )


def enviar_texto_grupo(destino: str, texto: str) -> None:
    alvo = (destino or "").strip()
    if not alvo or "@g.us" not in alvo:
        raise RuntimeError("Escolha o grupo do WhatsApp que vai receber o relatório.")
    garantir_token()
    msg = (texto or "").strip()
    if not msg:
        return
    ultimo = None
    for body in (
        {"Phone": alvo, "Body": msg},
        {"Phone": alvo, "Text": msg},
    ):
        try:
            _http("POST", "/chat/send/text", body=body, timeout=12)
            return
        except RuntimeError as exc:
            ultimo = exc
            continue
    raise RuntimeError(_erro_whatsapp(str(ultimo) if ultimo else "Falha ao enviar texto no grupo."))


def _enviar_documento(destino: str, pdf: Path, data: str) -> None:
    b64 = base64.b64encode(pdf.read_bytes()).decode("ascii")
    nome = f"relatorio-rotas-{data}.pdf"
    caption = _legenda(data)
    tentativas = (
        {
            "Phone": destino,
            "FileName": nome,
            "Document": f"data:application/pdf;base64,{b64}",
            "MimeType": "application/pdf",
            "Caption": caption,
        },
        {
            "Phone": destino,
            "FileName": nome,
            "Document": b64,
            "Caption": caption,
        },
    )
    ultimo = None
    for body in tentativas:
        try:
            _http("POST", "/chat/send/document", body=body, timeout=180)
            return
        except RuntimeError as exc:
            ultimo = exc
            continue
    raise RuntimeError(_erro_whatsapp(str(ultimo) if ultimo else "Falha ao enviar o PDF no grupo."))


def enviar_pdf_dia(dia: str | None, *, grupo_jid: str | None = None, forcar_pdf: bool = True) -> dict[str, Any]:
    data = (dia or "").strip() or date.today().isoformat()
    pdf = garantir_pdf_relatorio(data, forcar=forcar_pdf)
    cfg = carregar_config()
    destino = (grupo_jid or cfg.get("grupo_jid") or "").strip()
    if not destino:
        raise RuntimeError("Escolha o grupo do WhatsApp que vai receber o relatório.")
    if "@g.us" not in destino:
        raise RuntimeError("O destino salvo não é um grupo do WhatsApp. Escolha o grupo de novo na lista.")
    garantir_token()
    _enviar_documento(destino, pdf, data)
    return {"ok": True, "msg": "PDF do relatório (o mesmo da Sala técnica) enviado no grupo.", "enviados": 1}


def publico_config() -> dict[str, Any]:
    return estado_publico()
