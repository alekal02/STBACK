"""Comandos do grupo de WhatsApp, na mesma Wuzapi usada pelo Medzup."""

from __future__ import annotations

import json
import re
import secrets
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .config import Settings, dia_operacional, load_settings

_VISTOS: set[str] = set()
_DIAS: dict[tuple[str, str], date] = {}
_LOCK = threading.Lock()
_GERANDO = threading.Lock()
_CONFIG_LOCK = threading.Lock()
_NOME_USUARIO = "sagtrack"
_WEBHOOK_URL = ""
_ULTIMO_CONNECT = 0.0
_MANTER = True
_GRUPO_PADRAO = "120363429863984066@g.us"
_NOME_PADRAO = "Eu"
def _caminho_config() -> Path:
    from .config import pasta_dados

    return pasta_dados() / "whatsapp.json"


def _norm(texto: str) -> str:
    base = unicodedata.normalize("NFD", texto or "")
    sem = "".join(c for c in base if unicodedata.category(c) != "Mn")
    return re.sub(r"\s+", " ", sem).strip().upper()


def _comando_simples(cmd: str) -> tuple[str, str] | None:
    if cmd in {"MENU", "AJUDA", "COMANDOS", "OI", "OLA", "INICIO"}:
        return "menu", ""
    if cmd in {"FROTA", "RELATORIO", "RELATORIO FROTA", "RELATORIO DA FROTA"}:
        return "frota", ""
    rota = re.fullmatch(r"(?:RELATORIO\s+)?ROTA\s+(.+)", cmd)
    if rota:
        return "rota", rota.group(1).strip()
    placa = re.fullmatch(r"(?:RELATORIO\s+)?(?:PLACA|CAMINHAO)\s+(.+)", cmd)
    if placa:
        return "placa", placa.group(1).strip()
    if re.fullmatch(r"\d{2}[A-Z]{2}", cmd):
        return "rota", cmd
    if re.fullmatch(r"[A-Z]{3}-?\d[A-Z0-9]\d{2}", cmd):
        return "placa", cmd
    return None


def _extrair_data(cmd: str, hoje: date) -> tuple[date | None, str]:
    marcado = re.fullmatch(
        r"DIA\s+(\d{1,2})(?:[/-](\d{1,2})(?:[/-](\d{4}))?)?(?:\s+(.*))?",
        cmd,
    )
    livre = None if marcado else re.fullmatch(
        r"(\d{1,2})[/-](\d{1,2})(?:[/-](\d{4}))?(?:\s+(.*))?",
        cmd,
    )
    alvo = marcado or livre
    if alvo is None:
        return None, cmd
    dia_n = int(alvo.group(1))
    mes_n = int(alvo.group(2) or hoje.month)
    ano_n = int(alvo.group(3) or hoje.year)
    try:
        escolhida = date(ano_n, mes_n, dia_n)
    except ValueError as exc:
        raise ValueError("data") from exc
    return escolhida, (alvo.group(4) or "").strip()


def comando_grupo(texto: str, hoje: date | None = None) -> tuple[str, str, date | None] | None:
    """Lê o pedido do grupo. A data, quando vem, é o dia em que o turno começa às 06:00."""
    hoje = hoje or date.today()
    cmd = _norm(texto)
    try:
        dia, resto = _extrair_data(cmd, hoje)
    except ValueError:
        return "erro", "Essa data não existe. Use *dia 26* ou *26/09*.", None
    if dia is not None and not resto:
        return "dia", "", dia
    pedido = _comando_simples(resto or cmd)
    if pedido is None:
        if dia is not None:
            return "erro", "Depois da data, peça *frota*, a rota ou a placa.", dia
        return None
    return pedido[0], pedido[1], dia


def _rotulo_turno(dia: date) -> str:
    fim = dia + timedelta(days=1)
    return f"{dia.strftime('%d/%m/%Y')} das 06:00 até {fim.strftime('%d/%m/%Y')} 05:59"


def _config_arquivo() -> dict[str, Any]:
    if not _caminho_config().is_file():
        return {}
    try:
        dado = json.loads(_caminho_config().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return dado if isinstance(dado, dict) else {}


def _caminho_grupo() -> Path:
    return _caminho_config().with_name("grupo.json")


def _ler_backup() -> tuple[str, str]:
    caminho = _caminho_grupo()
    if not caminho.is_file():
        return "", ""
    try:
        dado = json.loads(caminho.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "", ""
    if not isinstance(dado, dict):
        return "", ""
    return str(dado.get("group_jid") or "").strip(), str(dado.get("group_nome") or "").strip()


def _escrever_backup(jid: str, nome: str) -> None:
    if not jid.endswith("@g.us"):
        return
    caminho = _caminho_grupo()
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(
        json.dumps({"group_jid": jid, "group_nome": nome}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _jid_gravado() -> tuple[str, str]:
    atual = _config_arquivo()
    jid = str(atual.get("group_jid") or "").strip()
    nome = str(atual.get("group_nome") or "").strip()
    if jid:
        return jid, nome
    return _ler_backup()


def grupo_configurado(settings: Settings | None = None) -> str:
    settings = settings or load_settings()
    jid, nome = _jid_gravado()
    if not jid:
        jid = str(settings.whatsapp_group_jid or "").strip()
    if not jid:
        jid, nome = _GRUPO_PADRAO, nome or _NOME_PADRAO
    if jid and not str(_config_arquivo().get("group_jid") or "").strip():
        salvar_configuracao(jid, group_nome=nome or None)
    return jid


def permitidos(settings: Settings | None = None) -> list[str]:
    settings = settings or load_settings()
    arquivo = _config_arquivo().get("allowed_jids")
    if isinstance(arquivo, list) and arquivo:
        return [str(item).strip() for item in arquivo if str(item).strip()]
    return list(settings.whatsapp_allowed_jids)


def grupo_nome() -> str:
    nome = str(_config_arquivo().get("group_nome") or "").strip()
    if nome:
        return nome
    _, backup = _ler_backup()
    return backup


def _gravar(atual: dict[str, Any]) -> None:
    jid = str(atual.get("group_jid") or "").strip()
    if not jid:
        jid, nome_backup = _ler_backup()
        if jid:
            atual["group_jid"] = jid
            atual["travado"] = True
            if not str(atual.get("group_nome") or "").strip() and nome_backup:
                atual["group_nome"] = nome_backup
    caminho = _caminho_config()
    caminho.parent.mkdir(parents=True, exist_ok=True)
    caminho.write_text(json.dumps(atual, ensure_ascii=False, indent=2), encoding="utf-8")
    gravado = str(atual.get("group_jid") or "").strip()
    if gravado:
        _escrever_backup(gravado, str(atual.get("group_nome") or "").strip())


def salvar_configuracao(
    group_jid: str,
    allowed_jids: list[str] | None = None,
    group_nome: str | None = None,
) -> dict[str, Any]:
    with _CONFIG_LOCK:
        atual = _config_arquivo()
        novo = (group_jid or "").strip() or str(atual.get("group_jid") or "").strip()
        atual["group_jid"] = novo
        atual["travado"] = bool(novo)
        if group_nome is not None and group_nome.strip():
            atual["group_nome"] = group_nome.strip()
        if allowed_jids is not None:
            atual["allowed_jids"] = [item.strip() for item in allowed_jids if item.strip()]
        _gravar(atual)
        return dict(atual)


def _url(settings: Settings, caminho: str) -> str:
    return settings.wuzapi_url.rstrip("/") + caminho


def _json(pedido: urllib.request.Request, timeout: int = 20) -> Any:
    with urllib.request.urlopen(pedido, timeout=timeout) as resp:
        bruto = resp.read().decode("utf-8")
    if not bruto:
        return {}
    return json.loads(bruto)


def _pedido(
    settings: Settings,
    caminho: str,
    payload: dict[str, Any] | None = None,
    *,
    admin: bool = False,
    timeout: int = 20,
) -> Any:
    dados = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if admin:
        headers["Authorization"] = settings.wuzapi_admin_token
    else:
        headers["token"] = settings.wuzapi_token
    metodo = "POST" if payload is not None else "GET"
    pedido = urllib.request.Request(_url(settings, caminho), data=dados, headers=headers, method=metodo)
    return _json(pedido, timeout)


def _limpar(valor: Any) -> Any:
    if isinstance(valor, dict):
        return {
            chave: _limpar(item)
            for chave, item in valor.items()
            if "token" not in chave.lower() and chave.lower() not in {"publickey", "hmac_key"}
        }
    if isinstance(valor, list):
        return [_limpar(item) for item in valor]
    return valor


def _usuarios(settings: Settings) -> list[dict[str, Any]]:
    lista = _pedido(settings, "/admin/users", admin=True)
    dados = (lista or {}).get("data") if isinstance(lista, dict) else lista
    return [item for item in dados or [] if isinstance(item, dict)]


def _por_nome(usuarios: list[dict[str, Any]]) -> dict[str, Any] | None:
    for item in usuarios:
        nome = str(item.get("name") or item.get("Name") or "").strip().lower()
        if nome == _NOME_USUARIO:
            return item
    return None


def _gravar_token(token: str) -> None:
    with _CONFIG_LOCK:
        atual = _config_arquivo()
        atual["wuzapi_token"] = token
        _gravar(atual)


def garantir_token(settings: Settings) -> Settings:
    """Usa o usuário sagtrack já existente. Só cria token novo quando esse usuário não existe."""
    from dataclasses import replace

    if not settings.wuzapi_admin_token:
        return settings
    try:
        usuarios = _usuarios(settings)
    except Exception:
        return settings
    achado = _por_nome(usuarios)
    if achado is not None:
        token = str(achado.get("token") or achado.get("Token") or "").strip()
        if token and token != settings.wuzapi_admin_token:
            return replace(settings, wuzapi_token=token)
        return settings
    token = "sagtrack_" + secrets.token_hex(8)
    try:
        _pedido(
            settings,
            "/admin/users",
            {
                "name": _NOME_USUARIO,
                "token": token,
                "webhook": _webhook_para(settings),
                "events": "Message",
            },
            admin=True,
        )
    except urllib.error.HTTPError as exc:
        if exc.code != 409:
            return settings
    try:
        de_novo = _usuarios(settings)
    except Exception:
        return replace(settings, wuzapi_token=token)
    criado = _por_nome(de_novo)
    real = str((criado or {}).get("token") or (criado or {}).get("Token") or token).strip() or token
    _gravar_token(real)
    return replace(settings, wuzapi_token=real)


def _webhook_para(settings: Settings) -> str:
    """A Wuzapi pública não pode receber o endereço local desta máquina."""
    url = settings.webhook_url.strip()
    wuzapi_local = any(item in settings.wuzapi_url for item in ("127.0.0.1", "localhost"))
    webhook_local = any(item in url for item in ("127.0.0.1", "localhost", "host.docker.internal"))
    if not wuzapi_local and webhook_local:
        return "https://sagtrack-backend.onrender.com/api/webhooks/whatsapp"
    return url


def _aplicar_webhook(settings: Settings) -> None:
    global _WEBHOOK_URL
    webhook = _webhook_para(settings)
    if not webhook or webhook == _WEBHOOK_URL:
        return
    for corpo in (
        {"webhookURL": webhook, "events": ["Message"]},
        {"webhook": webhook, "events": "Message"},
    ):
        try:
            _pedido(settings, "/webhook", corpo)
            _WEBHOOK_URL = webhook
            return
        except Exception:
            continue


def preparar(settings: Settings | None = None, *, webhook: bool = False) -> tuple[Settings, str]:
    settings = settings or load_settings()
    try:
        settings = garantir_token(settings)
    except Exception as exc:
        return settings, f"Wuzapi indisponível: {exc}"
    if not settings.wuzapi_token:
        return settings, "A sessão da Segtrack ainda não tem token na Wuzapi."
    if webhook:
        _aplicar_webhook(settings)
    return settings, ""


def _ler_sessao(settings: Settings) -> tuple[dict[str, Any], str]:
    try:
        corpo = _pedido(settings, "/session/status")
    except Exception as exc:
        return {}, str(exc)
    dados = corpo.get("data") if isinstance(corpo, dict) else corpo
    return dados if isinstance(dados, dict) else {}, ""


def _logado(dados: dict[str, Any]) -> bool:
    return bool(dados.get("loggedIn") or dados.get("LoggedIn"))


def _socket(dados: dict[str, Any]) -> bool:
    return bool(dados.get("connected") or dados.get("Connected"))


def _religar_socket(settings: Settings, *, forcar: bool = False) -> None:
    """Religa o celular já pareado. Não gera QR e não repete a tentativa o tempo todo."""
    global _ULTIMO_CONNECT
    agora = time.monotonic()
    if not forcar and agora - _ULTIMO_CONNECT < 180:
        return
    _ULTIMO_CONNECT = agora
    _pedido(settings, "/session/connect", {"Subscribe": ["Message"], "Immediate": False})


def _resposta(settings: Settings, dados: dict[str, Any], erro: str = "") -> dict[str, Any]:
    logado = _logado(dados)
    grupo = grupo_configurado(settings)
    corpo: dict[str, Any] = {
        "configurado": bool(settings.wuzapi_token),
        "pareado": logado,
        "conectado": logado and _MANTER,
        "socket": _socket(dados),
        "sessao": _limpar(dados),
        "grupo": grupo,
        "grupo_nome": grupo_nome(),
        "travado": bool(grupo),
    }
    if erro and not logado:
        corpo["erro"] = erro
    return corpo


def status(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or load_settings()
    settings, aviso = preparar(settings)
    if aviso and not settings.wuzapi_token:
        grupo = grupo_configurado(settings)
        return {"configurado": False, "erro": aviso, "grupo": grupo, "grupo_nome": grupo_nome(), "travado": bool(grupo)}
    dados, erro = _ler_sessao(settings)
    return _resposta(settings, dados, aviso or erro)


def conectar(settings: Settings | None = None) -> dict[str, Any]:
    global _MANTER
    settings = settings or load_settings()
    settings, aviso = preparar(settings, webhook=True)
    if aviso and not settings.wuzapi_token:
        return {"erro": aviso}
    _MANTER = True
    dados, erro = _ler_sessao(settings)
    if _logado(dados):
        if not _socket(dados):
            try:
                _religar_socket(settings, forcar=True)
            except Exception as exc:
                return {"erro": str(exc), **_resposta(settings, dados)}
        return status(settings)
    try:
        _pedido(settings, "/session/connect", {"Subscribe": ["Message"], "Immediate": False})
    except Exception as exc:
        return {"erro": str(exc)}
    return status(settings)


def desconectar(settings: Settings | None = None) -> dict[str, Any]:
    global _MANTER
    settings = settings or load_settings()
    settings, aviso = preparar(settings)
    if aviso and not settings.wuzapi_token:
        return {"erro": aviso}
    _MANTER = False
    try:
        _pedido(settings, "/session/disconnect", {})
    except Exception as exc:
        return {"erro": str(exc)}
    return status(settings)


def qr(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or load_settings()
    settings, aviso = preparar(settings)
    if aviso and not settings.wuzapi_token:
        return {"erro": aviso}
    dados, erro = _ler_sessao(settings)
    if _logado(dados):
        return _resposta(settings, dados)
    if erro:
        return {"erro": erro, "conectado": False, "pareado": False, "grupo": grupo_configurado(settings)}
    imagem = str(dados.get("qrcode") or dados.get("QRCode") or "")
    if imagem.startswith("data:image"):
        return {"QRCode": imagem, "conectado": False, "pareado": False}
    try:
        corpo = _pedido(settings, "/session/qr")
    except urllib.error.HTTPError as exc:
        if exc.code not in {400, 404, 500}:
            return {"erro": str(exc)}
        try:
            _pedido(settings, "/session/connect", {"Subscribe": ["Message"], "Immediate": False})
            corpo = _pedido(settings, "/session/qr")
        except Exception as falha:
            return {"erro": str(falha)}
    except Exception as exc:
        return {"erro": str(exc)}
    dados = corpo.get("data") if isinstance(corpo, dict) else corpo
    if not isinstance(dados, dict):
        return {"qr": dados}
    return _limpar(dados)


def manter() -> None:
    """Mantém o celular já pareado. Não pede QR de novo e não troca o grupo."""
    try:
        settings, aviso = preparar(load_settings(), webhook=True)
        if aviso:
            print(f"WhatsApp: {aviso}", flush=True)
        else:
            print(f"WhatsApp pronto. Grupo travado {grupo_configurado(settings)}.", flush=True)
    except Exception as exc:
        print(f"WhatsApp: {exc}", flush=True)
    while True:
        time.sleep(60)
        if not _MANTER:
            continue
        try:
            settings, aviso = preparar(load_settings())
            if aviso or not settings.wuzapi_token:
                continue
            dados, erro = _ler_sessao(settings)
            if erro or not _logado(dados) or _socket(dados):
                continue
            _religar_socket(settings)
            print("WhatsApp: religando o celular já pareado", flush=True)
        except Exception as exc:
            print(f"WhatsApp: {exc}", flush=True)


def grupos(settings: Settings | None = None) -> list[dict[str, Any]]:
    settings = settings or load_settings()
    settings, aviso = preparar(settings)
    if aviso:
        raise RuntimeError(aviso)
    corpo = _pedido(settings, "/group/list")
    dados = corpo.get("data") if isinstance(corpo, dict) else {}
    lista = []
    if isinstance(dados, dict):
        lista = dados.get("Groups") or dados.get("groups") or []
    elif isinstance(dados, list):
        lista = dados
    saida = []
    for grupo in lista:
        if not isinstance(grupo, dict):
            continue
        jid = str(grupo.get("JID") or grupo.get("jid") or "")
        if not jid.endswith("@g.us"):
            continue
        saida.append(
            {
                "jid": jid,
                "nome": str(grupo.get("Name") or grupo.get("name") or "Grupo sem nome"),
            }
        )
    saida.sort(key=lambda item: item["nome"].lower())
    return saida


def _enviar(settings: Settings, caminho: str, payload: dict[str, Any], timeout: int = 90) -> None:
    _pedido(settings, caminho, payload, timeout=timeout)


def enviar_texto(settings: Settings, destino: str, texto: str) -> None:
    _enviar(
        settings,
        "/chat/send/text",
        {"Phone": destino, "Body": texto},
    )


def enviar_pdf(settings: Settings, destino: str, arquivo: bytes, nome: str) -> None:
    import base64

    _enviar(
        settings,
        "/chat/send/document",
        {
            "Phone": destino,
            "Document": "data:application/pdf;base64," + base64.b64encode(arquivo).decode("ascii"),
            "FileName": nome,
        },
        timeout=180,
    )


def _menu() -> str:
    return (
        "*Relatórios Segtrack*\n"
        "O turno vai das *06:00* até *05:59* do dia seguinte.\n"
        "Antes do relatório, diga o dia: *dia 26* (mês atual) ou *26/09*.\n\n"
        "*dia 26* — marca o turno; depois peça o relatório\n"
        "*dia 26 frota* ou *26/09 frota* — PDF da frota nesse turno\n"
        "*04AD* ou *rota 04AD* — como está a rota e qual caminhão coletou\n"
        "*SDG-1J76* ou *placa SDG-1J76* — trajeto do caminhão\n\n"
        "_Digite o comando neste grupo._"
    )


def mapear_mensagem(body: Any) -> dict[str, str] | None:
    if isinstance(body, dict) and body.get("jsonData"):
        bruto = body.get("jsonData")
        if isinstance(bruto, str):
            try:
                body = json.loads(bruto)
            except json.JSONDecodeError:
                return None
        elif isinstance(bruto, dict):
            body = bruto
    if not isinstance(body, dict) or body.get("type") != "Message":
        return None
    evento = body.get("event") if isinstance(body.get("event"), dict) else {}
    info = evento.get("Info") if isinstance(evento.get("Info"), dict) else {}
    msg = evento.get("Message") if isinstance(evento.get("Message"), dict) else {}
    if not info or not msg:
        return None
    grupo = str(info.get("Chat") or info.get("RemoteJid") or "")
    if not grupo.endswith("@g.us"):
        return None
    texto = (
        msg.get("conversation")
        or (msg.get("extendedTextMessage") or {}).get("text")
        or ((msg.get("listResponseMessage") or {}).get("singleSelectReply") or {}).get("selectedRowId")
        or (msg.get("buttonsResponseMessage") or {}).get("selectedButtonId")
    )
    if not texto:
        return None
    participante = str(info.get("Sender") or info.get("Participant") or "")
    participante = re.sub(r":\d+@", "@", participante)
    return {
        "id": str(info.get("ID") or info.get("Id") or ""),
        "grupo": grupo,
        "participante": participante,
        "nome": str(info.get("PushName") or ""),
        "texto": str(texto),
    }


def _garantir_coleta(settings: Settings, dia: date) -> None:
    if (settings.output_dir / dia.isoformat() / "resumo.json").is_file():
        return
    from .config import aplicar_periodo
    from .motor import executar

    executar(aplicar_periodo(settings, dia))


def _fontes_do_dia(pasta: Path) -> list[Path]:
    return [pasta / "frota.json", pasta / "resumo.json", pasta / "setores.json"]


def _cache_pdf(pasta: Path, chave: str) -> Path | None:
    destino = pasta / "cache" / chave
    if not destino.is_file():
        return None
    selo = destino.stat().st_mtime
    fontes = [item for item in _fontes_do_dia(pasta) if item.is_file()]
    if not fontes or any(item.stat().st_mtime > selo for item in fontes):
        return None
    return destino


def _guardar_cache(pasta: Path, chave: str, arquivo: bytes) -> None:
    destino = pasta / "cache" / chave
    destino.parent.mkdir(parents=True, exist_ok=True)
    destino.write_bytes(arquivo)


def _texto_frota(settings: Settings, dia: date) -> str:
    from .pdfmapa import _ler, _rotulo_andamento
    from .relatorios import _fmt_pct, balanco_frota

    pasta = settings.output_dir / dia.isoformat()
    if not (pasta / "resumo.json").is_file():
        return ""
    balanco = balanco_frota(_ler(pasta, "setores.json") or [], _ler(pasta, "frota.json") or [])
    feitas = sum(1 for row in balanco if row["situacao"] == "Feita")
    parciais = sum(1 for row in balanco if row["situacao"] == "Parcial")
    nao = sum(1 for row in balanco if row["situacao"] == "Não feita")
    linhas = [
        f"*Frota {dia.strftime('%d/%m/%Y')}*",
        f"{len(balanco)} programadas · {feitas} feitas · {parciais} parciais · {nao} não feitas.",
        "Feita só a partir de 90%. De 45% a 89% é parcial.",
        "",
    ]
    for row in balanco:
        if int(row.get("paradas") or 0) <= 0:
            continue
        linhas.append(
            f"{row['codigo']} {row['nome']} · {_fmt_pct(row['percentual'])} · {_rotulo_andamento(row)}"
        )
    vazias = [str(row["codigo"]) for row in balanco if int(row.get("paradas") or 0) <= 0]
    if vazias:
        linhas.append("")
        linhas.append("Sem coleta: " + ", ".join(vazias))
    linhas.append("")
    linhas.append("O PDF com os mapas segue agora.")
    return "\n".join(linhas)[:3900]


def _texto_consulta(settings: Settings, tipo: str, consulta: str, dia: date) -> str:
    from .relatorios import montar

    if not (settings.output_dir / dia.isoformat() / "resumo.json").is_file():
        return ""
    rel = montar(settings.output_dir, dia, tipo, consulta)
    linhas = [str(item) for item in (rel.get("texto") or []) if str(item).strip()]
    if not linhas:
        return ""
    linhas.append("O PDF segue agora.")
    return "\n".join(linhas[:12])[:3900]


def _gerar(settings: Settings, tipo: str, consulta: str, dia: date) -> tuple[bytes, str]:
    from .pdfmapa import pdf_frota, pdf_placa, pdf_rota

    _garantir_coleta(settings, dia)
    pasta = settings.output_dir / dia.isoformat()
    chave = f"{tipo}-{(consulta or '').strip().upper()}.pdf"
    guardado = _cache_pdf(pasta, chave)
    if guardado is not None:
        if tipo == "frota":
            return guardado.read_bytes(), f"frota-{dia.isoformat()}.pdf"
        return guardado.read_bytes(), f"{tipo}-{consulta}-{dia.isoformat()}.pdf"
    if tipo == "frota":
        arquivo, nome = pdf_frota(settings.output_dir, dia)
    elif tipo == "rota":
        arquivo, nome = pdf_rota(settings.output_dir, dia, consulta)
    else:
        arquivo, nome = pdf_placa(settings.output_dir, dia, consulta)
    _guardar_cache(pasta, chave, arquivo)
    return arquivo, nome


def processar(body: Any, settings: Settings | None = None) -> None:
    settings = settings or load_settings()
    settings, aviso = preparar(settings)
    if aviso:
        print(f"WhatsApp: {aviso}", flush=True)
        return
    mensagem = mapear_mensagem(body)
    if mensagem is None:
        return
    grupo = grupo_configurado(settings)
    if not grupo or mensagem["grupo"] != grupo:
        print(
            f"WhatsApp: pedido ignorado. travado={grupo or 'nenhum'} origem={mensagem['grupo']}",
            flush=True,
        )
        return
    liberados = permitidos(settings)
    if liberados and mensagem["participante"] not in liberados:
        return
    pedido = comando_grupo(mensagem["texto"])
    if pedido is None:
        return
    with _LOCK:
        if mensagem["id"] and mensagem["id"] in _VISTOS:
            return
        if mensagem["id"]:
            _VISTOS.add(mensagem["id"])
            if len(_VISTOS) > 400:
                _VISTOS.clear()
                _VISTOS.add(mensagem["id"])
    tipo, consulta, dia_pedido = pedido
    print(f"WhatsApp: {tipo} {consulta}".strip(), flush=True)
    try:
        if tipo == "menu":
            enviar_texto(settings, mensagem["grupo"], _menu())
            return
        if tipo == "erro":
            enviar_texto(settings, mensagem["grupo"], consulta)
            return
        chave = (mensagem["grupo"], mensagem["participante"])
        if tipo == "dia" and dia_pedido is not None:
            _DIAS[chave] = dia_pedido
            enviar_texto(
                settings,
                mensagem["grupo"],
                f"Turno de {_rotulo_turno(dia_pedido)}. Agora peça *frota*, a rota ou a placa.",
            )
            return
        dia = dia_pedido or _DIAS.get(chave) or dia_operacional()
        if dia_pedido is not None:
            _DIAS[chave] = dia_pedido
        if not _GERANDO.acquire(blocking=False):
            enviar_texto(
                settings,
                mensagem["grupo"],
                "Ainda estou montando o PDF anterior. O resumo dele já foi enviado neste grupo.",
            )
            return
        try:
            resumo = (
                _texto_frota(settings, dia)
                if tipo == "frota"
                else _texto_consulta(settings, tipo, consulta, dia)
            )
            if resumo:
                enviar_texto(settings, mensagem["grupo"], resumo)
            else:
                enviar_texto(
                    settings,
                    mensagem["grupo"],
                    f"Montando o PDF de {_rotulo_turno(dia)}. Já envio neste grupo.",
                )
            arquivo, nome = _gerar(settings, tipo, consulta, dia)
            print(f"WhatsApp: enviando {nome} ({len(arquivo)} bytes)", flush=True)
            enviar_pdf(settings, mensagem["grupo"], arquivo, nome)
            print(f"WhatsApp: {nome} enviado", flush=True)
        finally:
            _GERANDO.release()
    except Exception as exc:
        try:
            enviar_texto(settings, mensagem["grupo"], f"Não consegui enviar o relatório: {exc}")
        except Exception:
            print(f"WhatsApp: {exc}", flush=True)


def agendar(body: Any) -> None:
    threading.Thread(target=processar, args=(body,), daemon=True).start()
