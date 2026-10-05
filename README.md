# Sala técnica — API

Serviço Python publicado no Render. O painel estático fica no repositório do front, na Vercel.

```bash
pip install -r requirements.txt
python -m inlog_agent servir
```

O Render define `PORT` e `HOST`. As coletas ficam em `OUTPUT_DIR` (`/var/sagtrack/output`). As chaves do Fulltrack e do Wuzapi permanecem nas variáveis de ambiente do serviço.

Saúde: `GET /api/health`
